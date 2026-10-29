pub mod admin_session;
pub mod auth;
pub mod db;
pub mod game;
pub mod hub;
pub mod llm;
pub mod rest;
pub mod router;
pub mod ws;

// Structured logging for the whole process. Level comes from RUST_LOG
// (default: info for this crate, warn for chatty dependencies); LOG_FORMAT=json
// switches to one JSON object per line for log aggregators (k8s). Safe to call
// twice: a second init is ignored, which keeps tests that boot the app happy.
pub fn init_logging() {
    use tracing_subscriber::{fmt, EnvFilter};
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("info,sqlx=warn,hyper=warn,reqwest=warn,tower_http=warn"));
    let json = std::env::var("LOG_FORMAT").is_ok_and(|v| v.eq_ignore_ascii_case("json"));
    let builder = fmt().with_env_filter(filter).with_target(true);
    let _ = if json { builder.json().try_init() } else { builder.try_init() };
}

// Rate-limited WARN for hot paths: at most one line per 5s per process, so an
// LLM outage logs the fact instead of one line per failed answer.
pub fn log_warn(msg: &str) {
    use std::sync::atomic::{AtomicU64, Ordering};
    static LAST_MS: AtomicU64 = AtomicU64::new(0);
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0);
    // Rare double-log under a race is harmless; the bound that matters is
    // one line per 5s in the common case.
    if now.saturating_sub(LAST_MS.load(Ordering::Relaxed)) >= 5000 {
        LAST_MS.store(now, Ordering::Relaxed);
        tracing::warn!("{msg}");
    }
}
