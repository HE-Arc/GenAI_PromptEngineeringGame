use sqlx::SqlitePool;

// The id is 128 random bits: knowing the link is the only access check, the
// same trust a room code gets.
pub async fn insert(pool: &SqlitePool, round_id: i64, mime: &str, data: &[u8]) -> Result<String, sqlx::Error> {
    let mut buf = [0u8; 16];
    getrandom::fill(&mut buf).map_err(|e| sqlx::Error::Protocol(format!("no randomness for image id: {e}")))?;
    let id = crate::admin_session::hex(&buf);
    sqlx::query("INSERT INTO generated_image (id, round_id, mime, data) VALUES (?, ?, ?, ?)")
        .bind(&id)
        .bind(round_id)
        .bind(mime)
        .bind(data)
        .execute(pool)
        .await?;
    Ok(id)
}

pub async fn get(pool: &SqlitePool, id: &str) -> Result<Option<(String, Vec<u8>)>, sqlx::Error> {
    sqlx::query_as("SELECT mime, data FROM generated_image WHERE id = ?")
        .bind(id)
        .fetch_optional(pool)
        .await
}
