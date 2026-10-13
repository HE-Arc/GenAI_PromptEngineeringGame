use axum::{
    extract::{Request, State},
    http::StatusCode,
    middleware::{self, Next},
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use serde_json::json;
use sqlx::SqlitePool;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{RwLock, Semaphore};

// Shed load before SQLite serializes it: at most this many REST requests run
// at once, each bounded in time and body size. The WS route is excluded — a
// long-lived socket is not a slow request, and holding a permit for its whole
// life would starve REST.
const MAX_CONCURRENT_REST: usize = 256;
const REST_TIMEOUT: Duration = Duration::from_secs(30);
const MAX_BODY_BYTES: usize = 1024 * 1024;

fn rest_slots() -> Arc<Semaphore> {
    static SLOTS: std::sync::OnceLock<Arc<Semaphore>> = std::sync::OnceLock::new();
    Arc::clone(SLOTS.get_or_init(|| Arc::new(Semaphore::new(MAX_CONCURRENT_REST))))
}

async fn limit_concurrency(State(slots): State<Arc<Semaphore>>, req: Request, next: Next) -> impl IntoResponse {
    let Ok(_permit) = slots.try_acquire_owned() else {
        return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": "busy"}))).into_response();
    };
    next.run(req).await
}

async fn rest_timeout(req: Request, next: Next) -> impl IntoResponse {
    match tokio::time::timeout(REST_TIMEOUT, next.run(req)).await {
        Ok(res) => res,
        Err(_) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": "timeout"}))).into_response(),
    }
}

use crate::hub::RoomHub;
use crate::llm::{self, LlmSource};
use crate::rest;
use crate::ws::ws_handler;

#[derive(Clone)]
pub struct AppState {
    pub pool: SqlitePool,
    pub hubs: Arc<RwLock<HashMap<String, Arc<RoomHub>>>>,
    pub llm: Arc<dyn LlmSource>,
    // Process start: rooms with no hub yet count as idle since then.
    pub boot: std::time::Instant,
}

impl AppState {
    pub fn new(pool: SqlitePool) -> Self {
        Self {
            pool,
            hubs: Arc::new(RwLock::new(HashMap::new())),
            llm: llm::from_env(),
            boot: std::time::Instant::now(),
        }
    }
}

async fn healthz() -> impl IntoResponse {
    Json(json!({"ok": true}))
}

pub fn build_router(state: AppState) -> Router {
    let rest = Router::new()
        .route("/healthz", get(healthz))
        .route("/api/admin/login", post(rest::admin_login))
        .route(
            "/api/admin/rooms",
            get(rest::admin_list).post(rest::admin_create),
        )
        .route("/api/admin/rooms/:code/start", post(rest::admin_start))
        .route("/api/admin/rooms/:code/next", post(rest::admin_next))
        .route("/api/admin/rooms/:code/restart", post(rest::admin_restart))
        .route("/api/rooms/:code/join", get(rest::join_get).post(rest::join))
        .route("/api/rooms/:code/rate", post(rest::rate))
        .route("/api/rooms/:code/ready", post(rest::ready))
        .route(
            "/api/rooms/:code/quiz",
            get(rest::quiz_get).post(rest::quiz_post),
        )
        .route_layer(middleware::from_fn_with_state(rest_slots(), limit_concurrency))
        .route_layer(middleware::from_fn(rest_timeout))
        .layer(axum::extract::DefaultBodyLimit::max(MAX_BODY_BYTES))
        .with_state(state.clone());
    Router::new()
        .route("/api/rooms/:code/ws", get(ws_handler))
        .with_state(state)
        .merge(rest)
}

#[cfg(test)]
pub async fn test_router() -> Router {
    use sqlx::sqlite::SqlitePoolOptions;
    let pool = SqlitePoolOptions::new()
        .max_connections(1)
        .connect("sqlite::memory:")
        .await
        .expect("test pool");
    build_router(AppState::new(pool))
}
