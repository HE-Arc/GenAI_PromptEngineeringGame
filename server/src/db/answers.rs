use sqlx::SqlitePool;

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct Answer {
    pub id: i64,
    pub round_id: i64,
    pub player_id: i64,
    pub prompt: String,
    pub output: String,
    pub score: i64,
}

// Single INSERT at stream end: no per-chunk UPDATE, so concurrent streams
// never contend on the same row.
pub async fn insert_answer(
    pool: &SqlitePool,
    round_id: i64,
    player_id: i64,
    prompt: &str,
    output: &str,
    score: i64,
) -> Result<Answer, sqlx::Error> {
    sqlx::query(
        "INSERT INTO answer (round_id, player_id, prompt, output, score) VALUES (?, ?, ?, ?, ?)",
    )
    .bind(round_id)
    .bind(player_id)
    .bind(prompt)
    .bind(output)
    .bind(score)
    .execute(pool)
    .await?;
    sqlx::query_as::<_, Answer>(
        "SELECT id, round_id, player_id, prompt, output, score FROM answer WHERE round_id = ? AND player_id = ?",
    )
    .bind(round_id)
    .bind(player_id)
    .fetch_one(pool)
    .await
}

pub async fn insert_rating(
    pool: &SqlitePool,
    answer_id: i64,
    rater_id: i64,
    score: i64,
) -> Result<i64, sqlx::Error> {
    // Self-rating rejected here too: the CHECK below cannot see across the
    // Answer join, so the view stays the primary guard.
    let author: Option<i64> =
        sqlx::query_scalar("SELECT player_id FROM answer WHERE id = ?")
            .bind(answer_id)
            .fetch_optional(pool)
            .await?;
    if author == Some(rater_id) {
        return Err(sqlx::Error::Protocol("a player cannot rate their own prompt".into()));
    }
    // A second tap changes the note: one row per rater per prompt, last tap wins.
    let id = sqlx::query(
        "INSERT INTO rating (answer_id, rater_id, score) VALUES (?, ?, ?)
         ON CONFLICT (answer_id, rater_id) DO UPDATE SET score = excluded.score",
    )
        .bind(answer_id)
        .bind(rater_id)
        .bind(score)
        .execute(pool)
        .await?
        .last_insert_rowid();
    Ok(id)
}

pub async fn add_ready(
    pool: &SqlitePool,
    room_code: &str,
    player_id: i64,
    step: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query("INSERT OR IGNORE INTO ready (room_code, player_id, step) VALUES (?, ?, ?)")
        .bind(room_code)
        .bind(player_id)
        .bind(step)
        .execute(pool)
        .await?;
    Ok(())
}

pub async fn create_quiz_question(
    pool: &SqlitePool,
    room_code: &str,
    order: i64,
    prompt: &str,
    choices_json: &str,
    correct: i64,
    explanation: &str,
) -> Result<i64, sqlx::Error> {
    let id = sqlx::query(
        "INSERT INTO quiz_question (room_code, \"order\", prompt, choices, correct, explanation)
         VALUES (?, ?, ?, ?, ?, ?)",
    )
    .bind(room_code)
    .bind(order)
    .bind(prompt)
    .bind(choices_json)
    .bind(correct)
    .bind(explanation)
    .execute(pool)
    .await?
    .last_insert_rowid();
    Ok(id)
}

pub async fn insert_quiz_attempt(
    pool: &SqlitePool,
    player_id: i64,
    question_id: i64,
    choice: i64,
    correct: bool,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO quiz_attempt (player_id, question_id, choice, correct) VALUES (?, ?, ?, ?)",
    )
    .bind(player_id)
    .bind(question_id)
    .bind(choice)
    .bind(if correct { 1 } else { 0 })
    .execute(pool)
    .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::{rooms, test_pool};

    #[tokio::test]
    async fn rating_unique_answer_rater() {
        let pool = test_pool().await;
        let room = rooms::create_room(&pool, "CD34").await.unwrap();
        let round = rooms::current_round(&pool, &room.code)
            .await
            .unwrap()
            .expect("order-0 round");
        let author = rooms::add_player(&pool, &room.code, "ana").await.unwrap();
        let rater = rooms::add_player(&pool, &room.code, "bob").await.unwrap();
        let answer = insert_answer(&pool, round.id, author.id, "p", "out", 7)
            .await
            .unwrap();
        insert_rating(&pool, answer.id, rater.id, 8).await.unwrap();
        // UNIQUE(answer_id, rater_id): a second note replaces the first.
        insert_rating(&pool, answer.id, rater.id, 3).await.unwrap();
        let scores: Vec<i64> = sqlx::query_scalar("SELECT score FROM rating WHERE answer_id = ?")
            .bind(answer.id)
            .fetch_all(&pool)
            .await
            .unwrap();
        assert_eq!(scores, [3]);
    }
}
