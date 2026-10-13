pub mod admin_session;
pub mod auth;
pub mod db;
pub mod game;
pub mod hub;
pub mod llm;
pub mod rest;
pub mod router;
pub mod ws;

// Rate-limited stderr note for hot paths (no tracing/log dependency by
// design): at most one line per 5s per process, so an LLM outage logs the
// fact instead of one line per failed answer.
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
        eprintln!("{msg}");
    }
}
