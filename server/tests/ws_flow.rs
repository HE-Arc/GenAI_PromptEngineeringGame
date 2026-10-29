use futures_util::{SinkExt, StreamExt};
use server::db::rooms;
use server::router::{build_router, AppState};
use server::ws::protocol::WsOut;
use sqlx::SqlitePool;
use sqlx::sqlite::SqlitePoolOptions;
use tokio_tungstenite::tungstenite::Message;

// Live loopback server: axum-test has no WS client, so bind an ephemeral
// port and speak real WebSocket bytes via tokio-tungstenite.
async fn spawn_app() -> (String, SqlitePool) {
    let pool = SqlitePoolOptions::new()
        .max_connections(1)
        .connect("sqlite::memory:")
        .await
        .expect("test pool");
    sqlx::migrate!("./migrations").run(&pool).await.expect("migrate");
    let app = build_router(AppState::new(pool.clone()));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        axum::serve(listener, app).await.expect("serve");
    });
    (format!("ws://127.0.0.1:{port}"), pool)
}

async fn recv_json(
    ws: &mut tokio_tungstenite::WebSocketStream<
        tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
    >,
) -> WsOut {
    let msg = ws.next().await.expect("frame").expect("no ws error");
    let text = match msg {
        Message::Text(t) => t.to_string(),
        other => panic!("expected text, got {other:?}"),
    };
    serde_json::from_str(&text).expect("WsOut json")
}

#[tokio::test]
async fn ws_answer_start_streams_chunks_then_done_and_accepts_retry() {
    std::env::remove_var("OPENAI_API_KEY");
    let (base, pool) = spawn_app().await;
    rooms::create_room(&pool, "CD34").await.unwrap();
    let player = rooms::add_player(&pool, "CD34", "ana").await.unwrap();
        // The stored target IS the exercice: give the round one, or
        // begin_answer refuses with the missing-exercise notice.
        sqlx::query("UPDATE round SET target = 'resume ce mail' WHERE room_code = 'CD34'")
            .execute(&pool)
            .await
            .unwrap();
        // Prompts are only taken in the writing phase.
        sqlx::query("UPDATE room SET phase = 'playing' WHERE code = 'CD34'")
            .execute(&pool)
            .await
            .unwrap();
    let url = format!("{base}/api/rooms/CD34/ws?token={}", player.token);

    let (mut ws, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    let hello = serde_json::json!({"type": "hello", "token": player.token}).to_string();
    ws.send(Message::Text(hello.into())).await.unwrap();
    let snap = recv_json(&mut ws).await;
    assert!(matches!(snap, WsOut::Snapshot { .. }));
    // Player hello also publishes a presence snapshot (online_count 1);
    // drain it before the answer frames are asserted.
    let presence = recv_json(&mut ws).await;
    assert!(matches!(presence, WsOut::Snapshot { .. }));

    ws.send(Message::Text(r#"{"type":"answer.start","prompt":"first prompt"}"#.into()))
        .await
        .unwrap();
    // scoring + at least one chunk, then exactly one done. Snapshots
    // interleave (presence on hello, persist before done) since they now
    // fan into the events channel like every other frame.
    let mut chunks = 0;
    let mut saw_scoring = false;
    loop {
        match tokio::time::timeout(std::time::Duration::from_secs(10), recv_json(&mut ws)).await {
            Ok(WsOut::AnswerChunk { .. }) => chunks += 1,
            Ok(WsOut::AnswerScoring { .. }) => saw_scoring = true,
            Ok(WsOut::Snapshot { .. }) => {}
            Ok(WsOut::AnswerDone { score, .. }) => {
                assert!(saw_scoring && chunks >= 1, "need scoring + chunks before done");
                assert!(score >= 0.0);
                break;
            }
            Ok(other) => panic!("unexpected frame while streaming: {other:?}"),
            Err(_) => panic!("timed out waiting for answer.done"),
        }
    }
    // Second start over the same socket => a new attempt that replaces the first.
    ws.send(Message::Text(r#"{"type":"answer.start","prompt":"again"}"#.into()))
        .await
        .unwrap();
    loop {
        match tokio::time::timeout(std::time::Duration::from_secs(10), recv_json(&mut ws)).await {
            Ok(WsOut::AnswerDone { .. }) => break,
            Ok(WsOut::Error { code, .. }) => panic!("retry refused: {code}"),
            Ok(_) => continue,
            Err(_) => panic!("timed out waiting for the retry's answer.done"),
        }
    }
    let prompts: Vec<String> = sqlx::query_scalar("SELECT prompt FROM answer")
        .fetch_all(&pool)
        .await
        .unwrap();
    assert_eq!(prompts, ["again"], "the retry replaced the first attempt");
}

#[tokio::test]
async fn ws_admin_cookie_subscribes_without_token() {
    // The console never holds an admin_key: the login cookie alone
    // admits the WS upgrade and hello answers the cached snapshot.
    let (base, pool) = spawn_app().await;
    rooms::create_room(&pool, "AD77").await.unwrap();
    let cookie = format!("genai_admin={}", server::admin_session::issue_value());
    let req = tokio_tungstenite::tungstenite::http::Request::builder()
        .uri(format!("{base}/api/rooms/AD77/ws?token="))
        .header("Cookie", cookie)
        .header("Host", "127.0.0.1")
        .header("Upgrade", "websocket")
        .header("Connection", "Upgrade")
        .header("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ==")
        .header("Sec-WebSocket-Version", "13")
        .body(())
        .unwrap();
    let (mut ws, _) = tokio_tungstenite::connect_async(req).await.unwrap();
    ws.send(Message::Text(r#"{"type":"hello","token":""}"#.into()))
        .await
        .unwrap();
    let msg = recv_json(&mut ws).await;
    assert!(
        matches!(msg, WsOut::Snapshot { .. }),
        "cookie-authed hello must yield snapshot, got {msg:?}"
    );
    // Watching is all a cookie connection may do: answering needs a player row.
    ws.send(Message::Text(r#"{"type":"answer.start","prompt":"x"}"#.into()))
        .await
        .unwrap();
    // First subscriber gets the hello snapshot twice (direct reply + events
    // fan-out): drain duplicates before reading the verdict.
    let err = loop {
        match recv_json(&mut ws).await {
            WsOut::Snapshot { .. } => continue,
            other => break other,
        }
    };
    assert!(
        matches!(&err, WsOut::Error { code, .. } if code == "bad_token"),
        "admin connection must not answer, got {err:?}"
    );
}

#[tokio::test]
async fn ws_hello_returns_snapshot_and_bad_token_closes() {
    let (base, pool) = spawn_app().await;
    rooms::create_room(&pool, "AB12").await.unwrap();
    let player = rooms::add_player(&pool, "AB12", "ana").await.unwrap();
    let url = format!("{base}/api/rooms/AB12/ws?token={}", player.token);

    let (mut ws, _) = tokio_tungstenite::connect_async(&url).await.unwrap();
    let hello = serde_json::json!({"type": "hello", "token": player.token}).to_string();
    ws.send(Message::Text(hello.into())).await.unwrap();
    let msg = recv_json(&mut ws).await;
    assert!(
        matches!(msg, WsOut::Snapshot { .. }),
        "hello must yield snapshot, got {msg:?}"
    );
    // ping keepalive round-trips without touching the DB. The presence
    // snapshot (hello side effect) may arrive before the pong, so drain.
    ws.send(Message::Text(r#"{"type":"ping"}"#.into())).await.unwrap();
    let mut pong = recv_json(&mut ws).await;
    while !matches!(pong, WsOut::Pong) {
        pong = recv_json(&mut ws).await;
    }
    assert!(matches!(pong, WsOut::Pong), "ping must yield pong, got {pong:?}");

    // Bad query token: error bad_token then close 4403, mirroring REST 403.
    let (mut bad, _) = tokio_tungstenite::connect_async(format!("{base}/api/rooms/AB12/ws?token=nope"))
        .await
        .unwrap();
    let err = recv_json(&mut bad).await;
    assert!(
        matches!(&err, WsOut::Error { code, .. } if code == "bad_token"),
        "bad token must yield bad_token error, got {err:?}"
    );
    let close = bad.next().await.expect("close frame").expect("no ws error");
    match close {
        Message::Close(Some(frame)) => assert_eq!(u16::from(frame.code), 4403),
        other => panic!("expected close 4403, got {other:?}"),
    }
}

#[tokio::test]
async fn ws_unknown_room_upgrades_then_refuses_in_band() {
    // A 404 on the handshake is invisible to a browser WebSocket, which then retries for ever;
    // the refusal has to arrive as a frame the client can act on.
    let (base, _pool) = spawn_app().await;
    let (mut ws, _) = tokio_tungstenite::connect_async(format!("{base}/api/rooms/ZZZZ/ws?token=x"))
        .await
        .expect("unknown room must still upgrade");
    let err = recv_json(&mut ws).await;
    assert!(
        matches!(&err, WsOut::Error { code, .. } if code == "unknown_room"),
        "unknown room must yield unknown_room error, got {err:?}"
    );
    let close = ws.next().await.expect("close frame").expect("no ws error");
    match close {
        Message::Close(Some(frame)) => assert_eq!(u16::from(frame.code), 4404),
        other => panic!("expected close 4404, got {other:?}"),
    }
}
