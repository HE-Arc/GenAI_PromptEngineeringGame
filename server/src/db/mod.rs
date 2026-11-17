use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous};
use sqlx::SqlitePool;
use std::str::FromStr;
use std::time::Duration;

pub mod answers;
pub mod images;
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
    heal_line_ending_checksums(&pool, &migrator).await;
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

// A migration checked out on Windows carries CRLF line endings, so an image
// built there records another checksum than the LF build from CI for the very
// same SQL, and switching images crash-loops on VersionMismatch. A stored
// checksum that matches the compiled SQL with only its line endings swapped is
// rewritten to the compiled one; any other mismatch is left to fail.
async fn heal_line_ending_checksums(pool: &SqlitePool, migrator: &sqlx::migrate::Migrator) {
    use sha2::{Digest, Sha384};
    for m in migrator.iter() {
        let lf = m.sql.replace("\r\n", "\n");
        let crlf = lf.replace('\n', "\r\n");
        for variant in [lf, crlf] {
            if variant == m.sql {
                continue;
            }
            let other = Sha384::digest(variant.as_bytes());
            let healed = sqlx::query("UPDATE _sqlx_migrations SET checksum = ? WHERE version = ? AND checksum = ?")
                .bind(&m.checksum[..])
                .bind(m.version)
                .bind(&other[..])
                .execute(pool)
                .await;
            // An error is a fresh database with no table yet: nothing to heal.
            if healed.is_ok_and(|r| r.rows_affected() > 0) {
                tracing::warn!(version = m.version, "migrations: checksum differed only by line endings, updated");
            }
        }
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

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha384};

    // A database migrated by a CRLF build must open with the LF build, while a
    // real edit of a migration still fails.
    #[tokio::test]
    async fn checksum_differing_only_by_line_endings_is_healed() {
        let pool = test_pool().await;
        let migrator = sqlx::migrate!("./migrations");
        let first = migrator.iter().next().unwrap();
        let crlf = first.sql.replace("\r\n", "\n").replace('\n', "\r\n");
        let set = |sum: Vec<u8>| {
            let pool = pool.clone();
            async move {
                sqlx::query("UPDATE _sqlx_migrations SET checksum = ? WHERE version = 1")
                    .bind(sum)
                    .execute(&pool)
                    .await
                    .unwrap();
            }
        };

        set(Sha384::digest(crlf.as_bytes()).to_vec()).await;
        assert!(migrator.run(&pool).await.is_err(), "the mismatch is real before healing");
        heal_line_ending_checksums(&pool, &migrator).await;
        migrator.run(&pool).await.expect("healed checksum passes");

        set(Sha384::digest(b"edited").to_vec()).await;
        heal_line_ending_checksums(&pool, &migrator).await;
        assert!(migrator.run(&pool).await.is_err(), "a real edit still fails");
    }
}
