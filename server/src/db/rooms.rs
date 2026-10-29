use sqlx::SqlitePool;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct Room {
    pub code: String,
    pub phase: String,
    pub current: i64,
    pub admin_key: String,
    #[sqlx(default)]
    pub tutorial: i64,
    #[sqlx(default)]
    pub quiz_index: i64,
    #[sqlx(default)]
    pub quiz_seconds: i64,
    #[sqlx(default)]
    pub auto_mode: i64,
}

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct Round {
    pub id: i64,
    pub room_code: String,
    pub order: i64,
    pub target: String,
    pub revealed: i64,
}

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct Player {
    pub id: i64,
    pub room_code: String,
    pub token: String,
    pub name: String,
}

static TOKEN_SEQ: AtomicU64 = AtomicU64::new(0);

// Unpredictable bearer: timestamp-derived tokens let anyone who joined once
// guess a later player's token, so randomness carries the entropy and the
// clock/sequence only disambiguate.
fn new_token() -> String {
    let seq = TOKEN_SEQ.fetch_add(1, Ordering::Relaxed);
    let mut buf = [0u8; 16];
    if getrandom::fill(&mut buf).is_ok() {
        return crate::admin_session::hex(&buf) + &format!("{seq:x}");
    }
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("{nanos:x}{seq:x}")
}

fn new_admin_key(code: &str) -> String {
    format!("{code}-{}-admin", new_token())
}

pub async fn create_room(pool: &SqlitePool, code: &str) -> Result<Room, sqlx::Error> {
    let admin_key = new_admin_key(code);
    sqlx::query("INSERT INTO room (code, admin_key) VALUES (?, ?)")
        .bind(code)
        .bind(&admin_key)
        .execute(pool)
        .await?;
    // Order-0 round up front so current_round() resolves before the game starts.
    create_round(pool, code, 0, "").await?;
    get_room(pool, code).await
}

pub async fn get_room(pool: &SqlitePool, code: &str) -> Result<Room, sqlx::Error> {
    sqlx::query_as::<_, Room>("SELECT code, phase, current, admin_key, tutorial, quiz_index, COALESCE(quiz_seconds, 60) AS quiz_seconds, COALESCE(auto_mode, 0) AS auto_mode FROM room WHERE code = ?")
        .bind(code)
        .fetch_one(pool)
        .await
}

// Mirrors Room.current_round(): None while the room has no matching round.
pub async fn current_round(pool: &SqlitePool, code: &str) -> Result<Option<Round>, sqlx::Error> {
    sqlx::query_as::<_, Round>(
        "SELECT round.id, round.room_code, round.\"order\", round.target, round.revealed
         FROM round JOIN room ON room.code = round.room_code
         WHERE room.code = ? AND round.\"order\" = room.current",
    )
    .bind(code)
    .fetch_optional(pool)
    .await
}

pub async fn create_round(
    pool: &SqlitePool,
    room_code: &str,
    order: i64,
    target: &str,
) -> Result<Round, sqlx::Error> {
    sqlx::query("INSERT INTO round (room_code, \"order\", target) VALUES (?, ?, ?)")
        .bind(room_code)
        .bind(order)
        .bind(target)
        .execute(pool)
        .await?;
    sqlx::query_as::<_, Round>(
        "SELECT id, room_code, \"order\", target, revealed FROM round WHERE room_code = ? AND \"order\" = ?",
    )
    .bind(room_code)
    .bind(order)
    .fetch_one(pool)
    .await
}

pub async fn add_player(pool: &SqlitePool, room_code: &str, name: &str) -> Result<Player, sqlx::Error> {
    let token = new_token();
    sqlx::query("INSERT INTO player (room_code, token, name) VALUES (?, ?, ?)")
        .bind(room_code)
        .bind(&token)
        .bind(name)
        .execute(pool)
        .await?;
    sqlx::query_as::<_, Player>(
        "SELECT id, room_code, token, name FROM player WHERE token = ?",
    )
    .bind(&token)
    .fetch_one(pool)
    .await
}

pub async fn player_by_token(pool: &SqlitePool, token: &str) -> Result<Option<Player>, sqlx::Error> {
    sqlx::query_as::<_, Player>(
        "SELECT id, room_code, token, name FROM player WHERE token = ?",
    )
    .bind(token)
    .fetch_optional(pool)
    .await
}

// Efface une partie terminée et tout ce qu'elle contient (manches, réponses,
// votes, joueurs, questions). Réservé à la console, une fois `done`.
// Renvoie `true` si la partie a été effacée, `false` si la garde finale
// n'a rien supprimé (partie absente ou non terminée) : l'appelant
// distingue 404 et 409 en relisant la partie.
pub async fn delete_room(pool: &SqlitePool, code: &str) -> Result<bool, sqlx::Error> {
    let mut tx = pool.begin().await?;
    for sql in [
        "DELETE FROM rating WHERE answer_id IN (SELECT answer.id FROM answer JOIN round ON round.id = answer.round_id WHERE round.room_code = ?)",
        "DELETE FROM answer WHERE round_id IN (SELECT id FROM round WHERE room_code = ?)",
        "DELETE FROM quiz_attempt WHERE question_id IN (SELECT id FROM quiz_question WHERE room_code = ?)",
        "DELETE FROM ready WHERE room_code = ?",
        "DELETE FROM round WHERE room_code = ?",
        "DELETE FROM quiz_question WHERE room_code = ?",
        "DELETE FROM player WHERE room_code = ?",
    ] {
        sqlx::query(sql).bind(code).execute(&mut *tx).await?;
    }
    let guard = sqlx::query("DELETE FROM room WHERE code = ? AND phase = 'done'")
        .bind(code)
        .execute(&mut *tx)
        .await?;
    if guard.rows_affected() != 1 {
        tx.rollback().await?;
        return Ok(false);
    }
    tx.commit().await?;
    Ok(true)
}

// The full room_state lives with the rest of the game rules.
pub async fn room_snapshot(
    pool: &SqlitePool,
    code: &str,
    online: Option<&std::collections::HashSet<i64>>,
) -> Result<serde_json::Value, sqlx::Error> {
    crate::game::room_snapshot(pool, code, online).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::{answers, test_pool};

    #[tokio::test]
    async fn answer_unique_round_player() {
        let pool = test_pool().await;
        let room = create_room(&pool, "AB12").await.unwrap();
        let round = current_round(&pool, &room.code)
            .await
            .unwrap()
            .expect("order-0 round");
        let p = add_player(&pool, &room.code, "ana").await.unwrap();
        answers::insert_answer(&pool, round.id, p.id, "x", "out", 7)
            .await
            .unwrap();
        // UNIQUE(round_id, player_id): one row per player per round, a retry upserts it.
        let dup = answers::insert_answer(&pool, round.id, p.id, "y", "out", 5).await;
        assert!(dup.is_err());
    }
}
