use axum_test::TestServer;
use futures_util::{SinkExt, StreamExt};
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
    assert!(snap["players"].as_array().unwrap().iter().any(|p| p["name"] == "ana"));
}

#[tokio::test]
async fn admin_list_online_count_follows_ws_connect_and_disconnect() {
    // The admin room list must read the live socket count, not a hardcoded
    // 0: player hello bumps it, socket drop restores 0 without any REST write.
    let (server, pool, state) = server_with_pool().await;
    rooms::create_room(&pool, "PR23").await.unwrap();
    let player = rooms::add_player(&pool, "PR23", "ana").await.unwrap();

    // Bind a real listener: axum-test has no WS client. Reusing the same
    // state means the hubs map (and thus presence) is shared with the REST
    // TestServer above.
    let app = build_router(state);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });

    let login = server
        .post("/api/admin/login")
        .json(&json!({"password": "genai"}))
        .await;
    let cookie = login
        .headers()
        .get("set-cookie")
        .unwrap()
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_owned();
    let list = || async {
        let res = server
            .get("/api/admin/rooms")
            .add_header("cookie", cookie.clone())
            .await;
        res.json::<serde_json::Value>()["rooms"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["code"] == "PR23")
            .unwrap()["online_count"]
            .as_u64()
            .unwrap()
    };
    assert_eq!(list().await, 0, "no sockets yet");

    // Connect + hello as the player: the count must move live.
    let url = format!("ws://127.0.0.1:{port}/api/rooms/PR23/ws?token={}", player.token);
    let (mut ws, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    ws.send(tokio_tungstenite::tungstenite::Message::Text(
        json!({"type": "hello", "token": player.token}).to_string().into(),
    ))
    .await
    .unwrap();
    let mut frames = 0;
    let mut saw_online = false;
    loop {
        match tokio::time::timeout(std::time::Duration::from_secs(5), ws.next()).await {
            Ok(Some(Ok(tokio_tungstenite::tungstenite::Message::Text(t)))) => {
                let v: serde_json::Value = serde_json::from_str(&t.to_string()).unwrap();
                if v["type"] == "snapshot" {
                    frames += 1;
                    if v["state"]["online_count"] == 1 {
                        saw_online = true;
                    }
                }
                if saw_online {
                    break;
                }
            }
            Ok(Some(Ok(_))) => continue,
            Ok(Some(Err(e))) => panic!("ws error: {e}"),
            Ok(None) => panic!("socket closed before hello"),
            Err(_) => panic!("timed out waiting for presence snapshot"),
        }
    }
    assert!(frames >= 1, "hello must yield a snapshot");
    assert!(saw_online, "player hello must bump online_count to 1");
    assert_eq!(list().await, 1, "admin list must read the live count");

    // Disconnect: the decrement task republishes; poll past its spawn gap.
    drop(ws);
    let mut online = 1;
    for _ in 0..100 {
        online = list().await;
        if online == 0 {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert_eq!(online, 0, "socket drop must restore online_count 0");
}

#[tokio::test]
async fn admin_login_ok_sets_signed_cookie() {
    // Django parity: right password opens the session; Axum answers
    // with a signed HttpOnly cookie instead of a server session.
    let server = test_server().await;
    let res = server
        .post("/api/admin/login")
        .json(&json!({"password": "genai"}))
        .await;
    res.assert_status_ok();
    res.assert_text(r#"{"ok":true}"#);
    let set_cookie = res
        .headers()
        .get("set-cookie")
        .expect("login must set the admin cookie")
        .to_str()
        .unwrap()
        .to_owned();
    assert!(set_cookie.starts_with("genai_admin="), "cookie name parity");
    assert!(set_cookie.contains("HttpOnly"), "never readable from page JS");
}

#[tokio::test]
async fn admin_login_wrong_password_403_parity() {
    // Django answers 403 {"error":"bad password"} — Axum must match exactly.
    let server = test_server().await;
    let res = server
        .post("/api/admin/login")
        .json(&json!({"password": "nope"}))
        .await;
    res.assert_status(axum::http::StatusCode::FORBIDDEN);
    res.assert_text(r#"{"error":"bad password"}"#);
}

#[tokio::test]
async fn admin_gated_routes_session_parity() {
    // Every admin route answers 403 {"error":"admin only"} without
    // credentials, and the login cookie opens each of them.
    let server = test_server().await;
    let gated = server.get("/api/admin/rooms").await;
    gated.assert_status(axum::http::StatusCode::FORBIDDEN);
    gated.assert_text(r#"{"error":"admin only"}"#);

    let login = server
        .post("/api/admin/login")
        .json(&json!({"password": "genai"}))
        .await;
    login.assert_status_ok();
    let cookie = login
        .headers()
        .get("set-cookie")
        .unwrap()
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_owned();

    let list = server.get("/api/admin/rooms").add_header("cookie", cookie.clone()).await;
    list.assert_status_ok();

    // The cookie also opens per-room admin writes, like Django's session.
    let created = server
        .post("/api/admin/rooms")
        .add_header("cookie", cookie.clone())
        .json(&json!({}))
        .await;
    created.assert_status_ok();
    let code = created.json::<serde_json::Value>()["code"]
        .as_str()
        .unwrap()
        .to_owned();
    let started = server
        .post(&format!("/api/admin/rooms/{code}/start"))
        .add_header("cookie", cookie)
        .await;
    started.assert_status_ok();

    // A forged cookie stays a 403: the signature is the whole credential.
    let forged = server
        .get("/api/admin/rooms")
        .add_header("cookie", "genai_admin=deadbeef")
        .await;
    forged.assert_status(axum::http::StatusCode::FORBIDDEN);
}
