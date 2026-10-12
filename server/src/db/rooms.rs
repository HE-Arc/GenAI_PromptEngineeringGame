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

// Dependency-free token: nanos plus a sequence so same-millisecond joins
// still differ. Upgraded to a CSPRNG token when join owns issuance.
fn new_token() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let seq = TOKEN_SEQ.fetch_add(1, Ordering::Relaxed);
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
    sqlx::query_as::<_, Room>("SELECT code, phase, current, admin_key, tutorial, quiz_index FROM room WHERE code = ?")
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

// Snapshot keys match the documented Django room_state shape that
// frontend/src/api.ts types as State: the player area reads
// `state.online_count` and per-player objects (`p.online`, `p.name`), so a
// bare name list renders the roster total ("sur 1 dans la pièce") with a
// blank where the count belongs. Scores stay null until settling lands;
// only the fields join-order and one answer lookup can state are filled.
pub async fn room_snapshot(
    pool: &SqlitePool,
    code: &str,
) -> Result<serde_json::Value, sqlx::Error> {
    let room = get_room(pool, code).await?;
    // get_room does not select this column, and widening its SELECT would
    // break its FromRow mapping; a scalar read keeps that query untouched.
    let anonymous: i64 = sqlx::query_scalar("SELECT anonymous FROM room WHERE code = ?")
        .bind(code)
        .fetch_one(pool)
        .await
        .unwrap_or(1);
    let total: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM round WHERE room_code = ?")
        .bind(code)
        .fetch_one(pool)
        .await
        .unwrap_or(0);
    let round = current_round(pool, code).await.ok().flatten();
    let names: Vec<(i64, String)> =
        sqlx::query_as("SELECT id, name FROM player WHERE room_code = ? ORDER BY id")
            .bind(code)
            .fetch_all(pool)
            .await?;
    let answered: Vec<i64> = match &round {
        Some(r) => {
            sqlx::query_scalar("SELECT player_id FROM answer WHERE round_id = ?")
                .bind(r.id)
                .fetch_all(pool)
                .await
                .unwrap_or_default()
        }
        None => Vec::new(),
    };
    // No presence registry on this server: WS connections are anonymous to
    // the DB, so every rostered player reads as on the stream until presence
    // lands. Counting the roster keeps the count and the lamps in agreement.
    let players: Vec<serde_json::Value> = names
        .iter()
        .map(|(id, name)| {
            serde_json::json!({
                "name": name,
                "score": null,
                "round_score": null,
                "answered": answered.contains(id),
                "online": true,
                "quiz_score": 0,
                "quiz_done": false,
                "proximity_total": null,
                "quality_total": null,
            })
        })
        .collect();
    let online_count = players.len();
    let target: Option<String> = round.as_ref().and_then(|r| {
        if r.target.is_empty() {
            None
        } else {
            Some(r.target.clone())
        }
    });
    Ok(serde_json::json!({
        "code": room.code,
        "phase": room.phase,
        "current": room.current,
        "total": total,
        "target": target,
        "target_image": null,
        "anonymous": anonymous != 0,
        "players": players,
        "online_count": online_count,
        "quiz": null,
        "reveal": null,
        "summary": null,
        "ready": null,
        "error": null,
        "timer": null,
    }))
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
        // UNIQUE(round_id, player_id): the already_answered 409 rests on this.
        let dup = answers::insert_answer(&pool, round.id, p.id, "y", "out", 5).await;
        assert!(dup.is_err());
    }
}
