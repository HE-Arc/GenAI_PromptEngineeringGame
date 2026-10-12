use axum_test::TestServer;
use serde_json::json;
use server::db::rooms;
use server::hub::get_hub;
use server::router::{build_router, AppState};
use sqlx::sqlite::SqlitePoolOptions;

async fn test_server() -> TestServer {
    let pool = SqlitePoolOptions::new()
        .max_connections(1)
        .connect("sqlite::memory:")
        .await
        .expect("test pool");
    sqlx::migrate!("./migrations").run(&pool).await.expect("migrate");
    TestServer::new(build_router(AppState::new(pool))).unwrap()
}

async fn server_with_pool() -> (TestServer, sqlx::SqlitePool, AppState) {
    let pool = SqlitePoolOptions::new()
        .max_connections(1)
        .connect("sqlite::memory:")
        .await
        .expect("test pool");
    sqlx::migrate!("./migrations").run(&pool).await.expect("migrate");
    let state = AppState::new(pool.clone());
    let server = TestServer::new(build_router(state.clone())).unwrap();
    (server, pool, state)
}

#[tokio::test]
async fn healthz_ok() {
    let server = test_server().await;
    let res = server.get("/healthz").await;
    res.assert_status_ok();
    res.assert_text(r#"{"ok":true}"#);
}

#[tokio::test]
async fn unknown_room_404_parity() {
    // Django returns 404 {"error":"unknown room"} — Axum must match exactly.
    let server = test_server().await;
    let res = server.get("/api/rooms/XXXX/join").await;
    res.assert_status(axum::http::StatusCode::NOT_FOUND);
    res.assert_text(r#"{"error":"unknown room"}"#);
}

#[tokio::test]
async fn join_wrong_phase_409_parity() {
    // Django join refuses non-waiting rooms with 409 — Axum must match.
    let (server, pool, _) = server_with_pool().await;
    rooms::create_room(&pool, "AB12").await.unwrap();
    sqlx::query("UPDATE room SET phase = 'playing' WHERE code = 'AB12'")
        .execute(&pool)
        .await
        .unwrap();
    let res = server
        .post("/api/rooms/AB12/join")
        .json(&json!({"name": "ana"}))
        .await;
    res.assert_status(axum::http::StatusCode::CONFLICT);
    res.assert_text(r#"{"error":"game already started"}"#);
}

#[tokio::test]
async fn join_duplicate_409_parity() {
    // Django answers a taken name with 409 {"error":"name taken"}.
    let (server, pool, _) = server_with_pool().await;
    rooms::create_room(&pool, "CD34").await.unwrap();
    let first = server
        .post("/api/rooms/CD34/join")
        .json(&json!({"name": "ana"}))
        .await;
    first.assert_status_ok();
    let dup = server
        .post("/api/rooms/CD34/join")
        .json(&json!({"name": "ana"}))
        .await;
    dup.assert_status(axum::http::StatusCode::CONFLICT);
    dup.assert_text(r#"{"error":"name taken"}"#);
}

#[tokio::test]
async fn bad_token_403_parity() {
    // Every player endpoint scopes the token to the room: 403 {"error":"bad token"}.
    let (server, pool, _) = server_with_pool().await;
    rooms::create_room(&pool, "EF56").await.unwrap();
    sqlx::query("UPDATE room SET phase = 'waiting' WHERE code = 'EF56'")
        .execute(&pool)
        .await
        .unwrap();
    let res = server
        .post("/api/rooms/EF56/ready")
        .json(&json!({"token": "nope"}))
        .await;
    res.assert_status(axum::http::StatusCode::FORBIDDEN);
    res.assert_text(r#"{"error":"bad token"}"#);

    // Quiz POST is phase-gated too: open the quiz, then the bad token must 403.
    sqlx::query("UPDATE room SET phase = 'quiz', quiz_deadline = datetime('now', '+30 seconds') WHERE code = 'EF56'")
        .execute(&pool)
        .await
        .unwrap();
    let res = server
        .post("/api/rooms/EF56/quiz")
        .json(&json!({"token": "nope", "question": 0, "choice": 0}))
        .await;
    res.assert_status(axum::http::StatusCode::FORBIDDEN);
    res.assert_text(r#"{"error":"bad token"}"#);
}

#[tokio::test]
async fn write_invalidates_snapshot_seq() {
    // Each write bumps seq + rebuilds the snapshot exactly once.
    let (server, pool, state) = server_with_pool().await;
    rooms::create_room(&pool, "GH78").await.unwrap();
    let hub = get_hub(&state.hubs, "GH78").await;
    hub.publish_snapshot(0, json!({"players": []})).await;
    let s0 = hub.cached().await.0;
    let res = server
        .post("/api/rooms/GH78/join")
        .json(&json!({"name": "ana"}))
        .await;
    res.assert_status_ok();
    let (seq, snap) = hub.cached().await;
    assert_eq!(seq, s0 + 1);
    assert!(snap["players"].as_array().unwrap().iter().any(|p| p == "ana"));
}
