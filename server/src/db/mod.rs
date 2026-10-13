use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous};
use sqlx::SqlitePool;
use std::str::FromStr;
use std::time::Duration;

pub mod answers;
pub mod rooms;

// WAL + NORMAL + 5s busy timeout: concurrent answer bursts wait instead of
// failing with database-is-locked (views.py:1816 measured 8/100).
//
// Pool sized for a write-serialized SQLite file: a few connections absorb
// read bursts while writers queue on the busy timeout instead of starving
// readers. Short lifetimes recycle a connection that a hung LLM task held.
pub async fn connect_pool(url: &str) -> Result<SqlitePool, sqlx::Error> {
    let options = SqliteConnectOptions::from_str(url)?
        .journal_mode(SqliteJournalMode::Wal)
        .synchronous(SqliteSynchronous::Normal)
        .busy_timeout(Duration::from_millis(5000))
        .foreign_keys(true)
        .create_if_missing(true);
    let pool = SqlitePoolOptions::new()
        .min_connections(2)
        .max_connections(num_cpus_max(16))
        .acquire_timeout(Duration::from_secs(5))
        .max_lifetime(Duration::from_secs(600))
        .idle_timeout(Duration::from_secs(120))
        .connect_with(options)
        .await?;
    sqlx::migrate!("./migrations").run(&pool).await?;
    Ok(pool)
}

// One writer at a time per file, so scale with CPUs only up to the point
// where extra connections just queue on the write lock.
fn num_cpus_max(cap: u32) -> u32 {
    std::thread::available_parallelism()
        .map(|n| (n.get() as u32).clamp(4, cap))
        .unwrap_or(8)
}

#[cfg(test)]
pub async fn test_pool() -> SqlitePool {
    let pool = SqlitePoolOptions::new()
        .max_connections(1)
        .connect("sqlite::memory:")
        .await
        .expect("test pool");
    sqlx::migrate!("./migrations")
        .run(&pool)
        .await
        .expect("migrate test db");
    pool
}
