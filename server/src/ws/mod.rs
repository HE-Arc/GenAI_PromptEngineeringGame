use axum::{
    extract::{Path, Query, State, WebSocketUpgrade},
    http::{HeaderMap, StatusCode},
    response::IntoResponse,
    Json,
};
use axum::extract::ws::{Message, WebSocket};
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use serde_json::json;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

pub mod protocol;

use crate::ws::protocol::{WsIn, WsOut};
use crate::admin_session;
use crate::auth::{validate, validate_ws, Auth};
use crate::db::{answers, rooms};
use crate::game;
use crate::hub::{get_hub, RoomHub};
use crate::router::AppState;

#[derive(Deserialize)]
pub struct WsParams {
    #[serde(default)]
    pub token: String,
}

const CLOSE_BAD_TOKEN: u16 = 4403;
// Same bound the composer enforces (`maxLength`), checked again server-side.
const MAX_PROMPT_CHARS: usize = 500;
// Player-facing, so French: shown to the author and as the room notice.
const CRASH_NOTICE: &str = "La partie a planté côté serveur. Préviens l'animateur.";
// Protocol-level pings are answered by the browser's network stack, not by
// page JS, so a backgrounded tab with throttled timers still answers them.
// That makes them the right probe for "is this player still reachable".
const SERVER_PING_EVERY: Duration = Duration::from_secs(15);
// No frame at all (pong, ping, text) for this long means a half-open
// socket: close it so the presence grace period starts counting.
const IDLE_TIMEOUT: Duration = Duration::from_secs(45);

pub struct AnswerReject {
    pub code: &'static str,
    pub message: &'static str,
}

impl std::fmt::Debug for AnswerReject {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "AnswerReject({})", self.code)
    }
}

pub struct AnswerJob {
    hub: Arc<RoomHub>,
    code: String,
    round_id: i64,
    player_id: i64,
    player_name: String,
    target: String,
    prompt: String,
}

// Unknown room rejects the upgrade outright so REST/WS share the 404 shape.
pub async fn ws_handler(
    State(state): State<AppState>,
    Path(code): Path<String>,
    Query(params): Query<WsParams>,
    headers: HeaderMap,
    ws: WebSocketUpgrade,
) -> impl IntoResponse {
    if rooms::get_room(&state.pool, &code).await.is_err() {
        return (StatusCode::NOT_FOUND, Json(json!({"error": "unknown room"}))).into_response();
    }
    let state = state.clone();
    ws.on_upgrade(move |socket| handle_socket(socket, state, code, params.token, headers))
        .into_response()
}

fn send_out(tx: &mpsc::UnboundedSender<WsOut>, msg: WsOut) {
    let _ = tx.send(msg);
}

// Idempotent admission: persisted answer => already_answered (Django 409
// {"error":"already answered"} semantics), live stream => inflight. Claiming
// before spawning closes the race between two concurrent answer.start frames.
pub async fn begin_answer(
    state: &AppState,
    code: &str,
    token: &str,
    prompt: &str,
) -> Result<AnswerJob, AnswerReject> {
    let prompt = prompt.trim();
    let player = match validate(&state.pool, code, token).await {
        Ok(Auth::Player(p)) => p,
        Ok(Auth::Admin) => {
            return Err(AnswerReject {
                code: "bad_token",
                message: "only players can answer",
            })
        }
        Err(_) => {
            return Err(AnswerReject {
                code: "bad_token",
                message: "bad token",
            })
        }
    };
    let round = rooms::current_round(&state.pool, code).await.map_err(|_| AnswerReject {
        code: "bad_phase",
        message: "no current round",
    })?.ok_or(AnswerReject {
        code: "bad_phase",
        message: "no current round",
    })?;
    if answers::find_answer(&state.pool, round.id, player.id)
        .await
        .map_err(|_| AnswerReject {
            code: "internal",
            message: "answer lookup failed",
        })?
        .is_some()
    {
        return Err(AnswerReject {
            code: "already_answered",
            message: "already answered",
        });
    }
    // Checked after already_answered, the more precise refusal. Only the writing phase takes prompts, and only while its clock runs.
    match game::load(&state.pool, code).await {
        Ok(g) if g.phase == "playing" && !g.phase_expired() => {}
        Ok(g) if g.phase == "playing" => {
            return Err(AnswerReject {
                code: "bad_phase",
                message: "Temps écoulé : la manche est fermée.",
            })
        }
        _ => {
            return Err(AnswerReject {
                code: "bad_phase",
                message: "not playing",
            })
        }
    }
    if prompt.is_empty() {
        return Err(AnswerReject {
            code: "bad_request",
            message: "prompt required",
        });
    }
    if prompt.chars().count() > MAX_PROMPT_CHARS {
        return Err(AnswerReject {
            code: "bad_request",
            message: "prompt too long",
        });
    }
    let hub = get_hub(&state.hubs, code).await;
    if !hub.try_claim(round.id, player.id).await {
        return Err(AnswerReject {
            code: "inflight",
            message: "answer already streaming",
        });
    }
    Ok(AnswerJob {
        hub,
        code: code.to_string(),
        round_id: round.id,
        player_id: player.id,
        player_name: player.name,
        target: round.target,
        prompt: prompt.to_string(),
    })
}

async fn insert_answer_row(
    pool: &sqlx::SqlitePool,
    job: &AnswerJob,
    output: &str,
    verdict: &crate::llm::Verdict,
    critique: &crate::llm::Verdict,
) -> Result<(), sqlx::Error> {
    // One INSERT per answer: concurrent streams never contend on a shared
    // row, and busy_timeout absorbs the burst instead of locking.
    sqlx::query(
        "INSERT INTO answer (round_id, player_id, prompt, output, score, comment, quality_llm, quality_comment)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    )
    .bind(job.round_id)
    .bind(job.player_id)
    .bind(&job.prompt)
    .bind(output)
    .bind(verdict.score)
    .bind(&verdict.feedback)
    .bind(critique.score)
    .bind(&critique.feedback)
    .execute(pool)
    .await?;
    Ok(())
}

// Fire-and-forget by design: the task owns pool/hub/llm clones, so dropping
// the socket never cancels it and a reconnecting player sees done via seq bump.
fn spawn_answer(state: AppState, job: AnswerJob) {
    tokio::spawn(async move {
        let player = job.player_name.clone();
        let (tx, mut rx) = mpsc::unbounded_channel::<String>();
        let llm = state.llm.clone();
        let prompt = job.prompt.clone();
        let stream_done = tokio::spawn(async move { llm.stream_into(prompt, tx).await });
        let mut full = String::new();
        while let Some(delta) = rx.recv().await {
            full.push_str(&delta);
            job.hub.publish_event(WsOut::AnswerChunk {
                player: player.clone(),
                delta,
            });
        }
        let streamed = stream_done
            .await
            .unwrap_or_else(|e| Err(format!("play task panicked: {e}")));
        let full = full.trim().to_string();
        let outcome = match streamed {
            Ok(truncated) => {
                job.hub.publish_event(WsOut::AnswerScoring {
                    player: player.clone(),
                });
                let verdict = state.llm.score(&job.prompt, &full, &job.target).await;
                let critique = state.llm.grade_prompt(&job.prompt, &job.target).await;
                insert_answer_row(&state.pool, &job, &full, &verdict, &critique)
                    .await
                    .map(|_| (verdict, truncated))
                    .map_err(|e| e.to_string())
            }
            Err(e) => Err(e),
        };
        match outcome {
            Ok((verdict, truncated)) => {
                job.hub.release(job.round_id, job.player_id).await;
                // The last online player to write opens the rating.
                game::maybe_advance_phase(&state.pool, &job.code, &job.hub.online()).await;
                let _ = job.hub.republish(&state.pool, &job.code).await;
                job.hub.publish_event(WsOut::AnswerDone {
                    player,
                    score: verdict.score,
                    feedback: verdict.feedback,
                    truncated,
                });
            }
            Err(e) => {
                eprintln!("answer failed in room {}: {e}", job.code);
                // A row still lands, scored 0 and marked: without it the
                // round would wait for this player forever, and the summary
                // says plainly what happened.
                let marker = crate::llm::Verdict {
                    score: 0,
                    feedback: CRASH_NOTICE.into(),
                };
                if let Err(e) = insert_answer_row(&state.pool, &job, &full, &marker, &marker).await {
                    eprintln!("fallback answer write failed in room {}: {e}", job.code);
                }
                job.hub.release(job.round_id, job.player_id).await;
                job.hub.note_error(CRASH_NOTICE);
                game::maybe_advance_phase(&state.pool, &job.code, &job.hub.online()).await;
                let _ = job.hub.republish(&state.pool, &job.code).await;
                job.hub.publish_event(WsOut::Error {
                    code: "internal".into(),
                    message: CRASH_NOTICE.into(),
                    player: Some(player),
                });
            }
        }
    });
}

// Owns the hub-side presence slot for one connected player socket. Drop
// only hands the socket back to the hub, which keeps the player online for
// the grace period: every return path of the recv loop releases it.
struct PresenceGuard {
    hub: Arc<RoomHub>,
    pool: sqlx::SqlitePool,
    code: String,
    player_id: i64,
}

impl Drop for PresenceGuard {
    fn drop(&mut self) {
        self.hub
            .player_disconnected(self.pool.clone(), self.code.clone(), self.player_id);
    }
}

// The writer and fan-out tasks hold clones of each other's channels, so
// neither ends on its own when the client leaves; the recv loop's exit
// tears both down instead of waiting for the next broadcast to fail.
struct AbortOnDrop(JoinHandle<()>);

impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        self.0.abort();
    }
}

async fn handle_socket(
    ws: WebSocket,
    state: AppState,
    code: String,
    query_token: String,
    headers: HeaderMap,
) {
    // Query token gates the connection before any hello is read; the
    // admin session cookie is the fallback so the console subscribes
    // with no token in a URL that proxies would log.
    if validate_ws(&state.pool, &code, &query_token, &headers)
        .await
        .is_err()
    {
        let mut ws = ws;
        send_direct(&mut ws, &WsOut::error("bad_token", "bad token")).await;
        let _ = ws
            .send(Message::Close(Some(axum::extract::ws::CloseFrame {
                code: CLOSE_BAD_TOKEN,
                reason: "bad token".into(),
            })))
            .await;
        return;
    }
    // Answer identity is the player query token only: an admin-key or
    // cookie connection may watch but never answers for a player row.
    let (player_id, player_name) = match validate(&state.pool, &code, &query_token).await {
        Ok(Auth::Player(p)) => (Some(p.id), Some(p.name)),
        _ => (None, None),
    };
    let answer_token: Option<String> = player_id.map(|_| query_token.clone());
    let hub = get_hub(&state.hubs, &code).await;
    let (mut sender, mut receiver) = ws.split();
    // Single writer task: the recv loop and the hub fan-out both push WsOut
    // here, so frames never interleave on the socket. It also owns the
    // keepalive ping, the only frame the server sends unprompted.
    let (tx_out, mut rx_out) = mpsc::unbounded_channel::<WsOut>();
    // Snapshots are shared by every socket; the per-viewer flags
    // (`reveal.mine`, `ready.mine`) are resolved here, on the way out.
    let _writer = AbortOnDrop(tokio::spawn(async move {
        let mut ping = tokio::time::interval(SERVER_PING_EVERY);
        ping.tick().await;
        loop {
            let frame = tokio::select! {
                msg = rx_out.recv() => match msg {
                    Some(WsOut::Error { player: Some(p), .. }) if player_name.as_deref() != Some(p.as_str()) => continue,
                    Some(WsOut::Snapshot { seq, state }) => Message::Text(
                        serde_json::to_string(&WsOut::Snapshot {
                            seq,
                            state: game::personalize(&state, player_id),
                        })
                        .unwrap_or_else(|_| r#"{"type":"error"}"#.into())
                        .into(),
                    ),
                    Some(msg) => Message::Text(
                        serde_json::to_string(&msg)
                            .unwrap_or_else(|_| r#"{"type":"error"}"#.into())
                            .into(),
                    ),
                    None => return,
                },
                _ = ping.tick() => Message::Ping(Vec::new().into()),
            };
            if sender.send(frame).await.is_err() {
                return;
            }
        }
    }));
    // Live answer frames reach every subscriber, including late joiners, via
    // the hub events channel rather than per-connection DB polls.
    let mut erx = hub.subscribe_events().await;
    let fwd_tx = tx_out.clone();
    let fwd_hub = Arc::clone(&hub);
    let fwd_code = code.clone();
    let fwd_pool = state.pool.clone();
    let _forwarder = AbortOnDrop(tokio::spawn(async move {
        loop {
            match erx.recv().await {
                Ok(ev) => {
                    if fwd_tx.send(ev).is_err() {
                        return;
                    }
                }
                // Lagged receivers resync from the cached snapshot, never panic.
                Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => {
                    let (seq, snap) = fwd_hub.cached().await;
                    if snap.is_null() {
                        // republish fans the fresh snapshot to this socket too.
                        let _ = fwd_hub.republish(&fwd_pool, &fwd_code).await;
                    } else if fwd_tx.send(WsOut::Snapshot { seq, state: snap }).is_err() {
                        return;
                    }
                }
                Err(tokio::sync::broadcast::error::RecvError::Closed) => return,
            }
        }
    }));
    // Held from the first successful player hello until this socket drops;
    // admins and cookie connections never count, so theirs stays None.
    let mut present: Option<PresenceGuard> = None;
    loop {
        let msg = match tokio::time::timeout(IDLE_TIMEOUT, receiver.next()).await {
            Ok(Some(Ok(m))) => m,
            // Closed, errored or silent past the timeout: all mean gone.
            Ok(Some(Err(_))) | Ok(None) | Err(_) => return,
        };
        let text = match msg {
            Message::Text(t) => t.to_string(),
            Message::Close(_) => return,
            _ => continue,
        };
        let incoming: WsIn = match serde_json::from_str(&text) {
            Ok(v) => v,
            Err(_) => {
                send_out(&tx_out, WsOut::error("bad_request", "unparseable message"));
                continue;
            }
        };
        match incoming {
            WsIn::Hello { token } => {
                // Cookie-authed admins hello with an empty token: the
                // session already admitted them at connect time.
                if validate(&state.pool, &code, &token).await.is_err()
                    && !admin_session::request_is_admin(&headers)
                {
                    send_out(&tx_out, WsOut::error("bad_token", "bad token"));
                    return;
                }
                // Presence first: when it changes, the republished snapshot
                // already carries this player as online, so the reply below
                // never shows them their own lamp off.
                if present.is_none() {
                    if let Some(id) = player_id {
                        hub.player_connected(&state.pool, &code, id).await;
                        present = Some(PresenceGuard {
                            hub: Arc::clone(&hub),
                            pool: state.pool.clone(),
                            code: code.clone(),
                            player_id: id,
                        });
                    }
                }
                let (seq, snap) = hub.cached().await;
                if snap.is_null() {
                    // First subscriber builds the snapshot once; later ones
                    // clone it. republish also fans it to this socket.
                    if hub.republish(&state.pool, &code).await.is_none() {
                        send_out(&tx_out, WsOut::error("unknown_room", "unknown room"));
                    }
                } else {
                    send_out(&tx_out, WsOut::Snapshot { seq, state: snap });
                }
            }
            WsIn::Ping => send_out(&tx_out, WsOut::Pong),
            WsIn::AnswerStart { prompt } => match answer_token.as_deref() {
                Some(t) => match begin_answer(&state, &code, t, &prompt).await {
                    Ok(job) => spawn_answer(state.clone(), job),
                    Err(reject) => send_out(&tx_out, WsOut::error(reject.code, reject.message)),
                },
                None => send_out(&tx_out, WsOut::error("bad_token", "only players can answer")),
            },
        }
    }
}

async fn send_direct(ws: &mut WebSocket, msg: &WsOut) {
    let text = serde_json::to_string(msg).unwrap_or_else(|_| r#"{"type":"error"}"#.into());
    let _ = ws.send(Message::Text(text.into())).await;
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::test_pool;

    // No OPENAI_API_KEY in tests by rule: AppState::new picks the stub.
    #[tokio::test]
    async fn double_answer_start_rejected() {
        std::env::remove_var("OPENAI_API_KEY");
        let pool = test_pool().await;
        let state = AppState::new(pool.clone());
        rooms::create_room(&pool, "EF56").await.unwrap();
        let player = rooms::add_player(&pool, "EF56", "ana").await.unwrap();
        // Prompts are only taken in the writing phase.
        sqlx::query("UPDATE room SET phase = 'playing' WHERE code = 'EF56'")
            .execute(&pool)
            .await
            .unwrap();
        let hub = get_hub(&state.hubs, "EF56").await;
        let mut erx = hub.subscribe_events().await;

        let job = begin_answer(&state, "EF56", &player.token, "first prompt")
            .await
            .expect("first start claims");
        // Concurrent second start while streaming => inflight, not a second task.
        let err = match begin_answer(&state, "EF56", &player.token, "second prompt").await {
            Ok(_) => panic!("second concurrent start must be rejected"),
            Err(e) => e,
        };
        assert_eq!(err.code, "inflight");
        spawn_answer(state.clone(), job);
        // Drain to done; disconnects never cancel so every frame must arrive.
        let mut chunks = 0;
        let done = loop {
            match erx.recv().await.expect("event") {
                WsOut::AnswerChunk { .. } => chunks += 1,
                d @ WsOut::AnswerDone { .. } => break d,
                _ => continue,
            }
        };
        assert!(chunks >= 1, "stub must stream at least one chunk");
        assert!(matches!(done, WsOut::AnswerDone { .. }));
        // Persisted answer => already_answered, the Django 409 over WS.
        let err = match begin_answer(&state, "EF56", &player.token, "third prompt").await {
            Ok(_) => panic!("post-done start must be rejected"),
            Err(e) => e,
        };
        assert_eq!(err.code, "already_answered");
    }

    struct FailingSource;

    impl crate::llm::LlmSource for FailingSource {
        fn stream_into(
            &self,
            _prompt: String,
            _tx: mpsc::UnboundedSender<String>,
        ) -> std::pin::Pin<Box<dyn std::future::Future<Output = crate::llm::StreamResult> + Send + '_>> {
            Box::pin(async { Err("provider down".into()) })
        }
        fn score(
            &self,
            _p: &str,
            _o: &str,
            _t: &str,
        ) -> std::pin::Pin<Box<dyn std::future::Future<Output = crate::llm::Verdict> + Send + '_>> {
            unreachable!("a failed stream is never scored")
        }
        fn grade_prompt(
            &self,
            _p: &str,
            _t: &str,
        ) -> std::pin::Pin<Box<dyn std::future::Future<Output = crate::llm::Verdict> + Send + '_>> {
            unreachable!("a failed stream is never graded")
        }
    }

    #[tokio::test]
    async fn failed_model_call_records_marker_and_notifies_author_only() {
        let pool = test_pool().await;
        let mut state = AppState::new(pool.clone());
        state.llm = Arc::new(FailingSource);
        rooms::create_room(&pool, "FA11").await.unwrap();
        let ana = rooms::add_player(&pool, "FA11", "ana").await.unwrap();
        sqlx::query("UPDATE room SET phase = 'playing' WHERE code = 'FA11'")
            .execute(&pool)
            .await
            .unwrap();
        let hub = get_hub(&state.hubs, "FA11").await;
        let mut erx = hub.subscribe_events().await;

        // Validation runs before any model call.
        for (prompt, msg) in [("   ", "prompt required"), (&*"x".repeat(501), "prompt too long")] {
            match begin_answer(&state, "FA11", &ana.token, prompt).await {
                Err(e) => assert_eq!(e.message, msg),
                Ok(_) => panic!("{msg} must be refused"),
            }
        }

        let job = begin_answer(&state, "FA11", &ana.token, "un prompt").await.unwrap();
        spawn_answer(state.clone(), job);
        let err = loop {
            match erx.recv().await.unwrap() {
                e @ WsOut::Error { .. } => break e,
                _ => continue,
            }
        };
        assert!(matches!(err, WsOut::Error { player: Some(ref p), .. } if p == "ana"));
        // The marker row lands so the round does not wait on ana forever.
        let (score, comment): (i64, String) =
            sqlx::query_as("SELECT score, comment FROM answer WHERE player_id = ?")
                .bind(ana.id)
                .fetch_one(&pool)
                .await
                .unwrap();
        assert_eq!((score, comment.as_str()), (0, CRASH_NOTICE));
        assert_eq!(hub.cached().await.1["error"], CRASH_NOTICE);
    }
}
