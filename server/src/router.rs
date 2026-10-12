use axum::{
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use serde_json::json;
use sqlx::SqlitePool;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;

use crate::hub::RoomHub;
use crate::llm::{self, LlmSource};
use crate::rest;
use crate::ws::ws_handler;

#[derive(Clone)]
pub struct AppState {
    pub pool: SqlitePool,
    pub hubs: Arc<RwLock<HashMap<String, Arc<RoomHub>>>>,
    pub llm: Arc<dyn LlmSource>,
}

impl AppState {
    pub fn new(pool: SqlitePool) -> Self {
        Self {
            pool,
            hubs: Arc::new(RwLock::new(HashMap::new())),
            llm: llm::from_env(),
        }
    }
}

async fn healthz() -> impl IntoResponse {
    Json(json!({"ok": true}))
}

pub fn build_router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/api/admin/rooms", post(rest::admin_create))
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
        .route("/api/rooms/:code/ws", get(ws_handler))
        .with_state(state)
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
