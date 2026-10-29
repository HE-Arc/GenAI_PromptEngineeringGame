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
    log_applied_migrations(&pool).await;
    let migrator = sqlx::migrate!("./migrations");
    if let Err(e) = migrator.run(&pool).await {
        tracing::error!(error = %e, "migrations failed");
        log_version_mismatch(&e, &migrator);
        return Err(e.into());
    }
    tracing::info!(compiled = migrator.migrations.len(), "migrations up to date");
    Ok(pool)
}

// Best-effort startup log: lists versions already recorded in the file DB.
// Missing table = fresh database, nothing applied yet. Never fails startup.
async fn log_applied_migrations(pool: &SqlitePool) {
    match sqlx::query("SELECT version, description FROM _sqlx_migrations ORDER BY version")
        .fetch_all(pool)
        .await
    {
        Ok(rows) => {
            let mut versions: Vec<String> = Vec::new();
            for row in &rows {
                let v: i64 = sqlx::Row::try_get(row, "version").unwrap_or(-1);
                let d: String = sqlx::Row::try_get(row, "description").unwrap_or_default();
                versions.push(format!("{v} ({d})"));
            }
            tracing::info!(applied = %versions.join(", "), "migrations: already applied");
        }
        Err(_) => tracing::info!("migrations: fresh database, none applied yet"),
    }
}

// Actionable hint for the stale-volume crash loop: the persisted DB holds a
// _sqlx_migrations checksum that differs from the compiled migration with
// the same version (0001/0002 must never be edited). Never deletes data here.
fn log_version_mismatch(e: &sqlx::migrate::MigrateError, migrator: &sqlx::migrate::Migrator) {
    if let sqlx::migrate::MigrateError::VersionMismatch(version) = e {
        let compiled: Vec<String> = migrator
            .migrations
            .iter()
            .map(|m| format!("{} ({})", m.version, m.description))
            .collect();
        tracing::error!(
            "migrations: VersionMismatch({version}): stored checksum in \
             _sqlx_migrations differs from compiled migration. \
             Stored rows logged above; compiled versions: [{}]. \
             Dev fix (wipes rooms): run `docker compose down -v` to reset dev DB",
            compiled.join(", ")
        );
    }
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
