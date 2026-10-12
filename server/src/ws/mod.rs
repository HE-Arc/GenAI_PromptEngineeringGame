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
use tokio::sync::mpsc;

pub mod protocol;

use crate::ws::protocol::{WsIn, WsOut};
use crate::admin_session;
use crate::auth::{validate, validate_ws, Auth};
use crate::db::{answers, rooms};
use crate::hub::{get_hub, RoomHub};
use crate::router::AppState;

#[derive(Deserialize)]
pub struct WsParams {
    #[serde(default)]
    pub token: String,
}

const CLOSE_BAD_TOKEN: u16 = 4403;

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
    let hub = get_hub(&state.hubs, code).await;
    if !hub.try_claim(round.id, player.id).await {
        return Err(AnswerReject {
            code: "inflight",
            message: "answer already streaming",
        });
    }
    Ok(AnswerJob {
        hub,
        round_id: round.id,
        player_id: player.id,
        player_name: player.name,
        target: round.target,
        prompt: prompt.to_string(),
    })
}

// Fire-and-forget by design: the task owns pool/hub/llm clones, so dropping
// the socket never cancels it and a reconnecting player sees done via seq bump.
fn spawn_answer(state: AppState, job: AnswerJob) {
    let player = job.player_name.clone();
    job.hub.publish_event(WsOut::AnswerScoring {
        player: player.clone(),
    });
    tokio::spawn(async move {
        let (tx, mut rx) = mpsc::unbounded_channel::<String>();
        let llm = state.llm.clone();
        let prompt = job.prompt.clone();
        let stream_done = tokio::spawn(async move {
            llm.stream_into(prompt, tx).await;
        });
        let mut full = String::new();
        while let Some(delta) = rx.recv().await {
            full.push_str(&delta);
            job.hub.publish_event(WsOut::AnswerChunk {
                player: player.clone(),
                delta,
            });
        }
        let _ = stream_done.await;
        let verdict = state.llm.score(&job.prompt, &full, &job.target).await;
        // One transaction, one INSERT: concurrent streams never contend on a
        // shared row, and busy_timeout absorbs the burst instead of locking.
        let persisted = async {
            let mut dbtx = state.pool.begin().await?;
            sqlx::query(
                "INSERT INTO answer (round_id, player_id, prompt, output, score) VALUES (?, ?, ?, ?, ?)",
            )
            .bind(job.round_id)
            .bind(job.player_id)
            .bind(&job.prompt)
            .bind(&full)
            .bind(verdict.score)
            .execute(&mut *dbtx)
            .await?;
            dbtx.commit().await?;
            Ok::<_, sqlx::Error>(())
        }
        .await;
        job.hub.release(job.round_id, job.player_id).await;
        if persisted.is_err() {
            job.hub.publish_event(WsOut::error("internal", "answer persist failed"));
            return;
        }
        let seq = job.hub.next_seq().await;
        // Snapshot rebuild needs the room code; recover it from the round row.
        let room_code: Option<String> =
            sqlx::query_scalar("SELECT room_code FROM round WHERE id = ?")
                .bind(job.round_id)
                .fetch_optional(&state.pool)
                .await
                .unwrap_or(None);
        if let Some(rc) = room_code {
            // Presence override keeps the live count across the rebuild.
            if let Ok(fresh) =
                rooms::room_snapshot(&state.pool, &rc, Some(job.hub.presence())).await
            {
                job.hub.publish_snapshot(seq, fresh).await;
            }
        }
        job.hub.publish_event(WsOut::AnswerDone {
            player,
            score: verdict.score,
            feedback: verdict.feedback,
        });
    });
}

// Owns the hub-side presence slot for one connected player socket. Drop
// fires the decrement in a detached task: socket teardown runs inside the
// recv loop where awaiting the snapshot rebuild would only hold the socket
// open longer, and the pool/hub clones make the task self-sufficient.
struct PresenceGuard {
    hub: Arc<RoomHub>,
    pool: sqlx::SqlitePool,
    code: String,
}

impl Drop for PresenceGuard {
    fn drop(&mut self) {
        let hub = Arc::clone(&self.hub);
        let pool = self.pool.clone();
        let code = self.code.clone();
        tokio::spawn(async move {
            let _ = hub.refresh_presence(&pool, &code, -1).await;
        });
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
    let answer_token: Option<String> =
        match validate(&state.pool, &code, &query_token).await {
            Ok(Auth::Player(_)) => Some(query_token.clone()),
            _ => None,
        };
    let hub = get_hub(&state.hubs, &code).await;
    let (mut sender, mut receiver) = ws.split();
    // Single writer task: the recv loop and the hub fan-out both push WsOut
    // here, so frames never interleave on the socket.
    let (tx_out, mut rx_out) = mpsc::unbounded_channel::<WsOut>();
    tokio::spawn(async move {
        while let Some(msg) = rx_out.recv().await {
            let text = serde_json::to_string(&msg).unwrap_or_else(|_| r#"{"type":"error"}"#.into());
            if sender.send(Message::Text(text.into())).await.is_err() {
                return;
            }
        }
    });
    // Live answer frames reach every subscriber, including late joiners, via
    // the hub events channel rather than per-connection DB polls.
    let mut erx = hub.subscribe_events().await;
    let fwd_tx = tx_out.clone();
    let fwd_hub = Arc::clone(&hub);
    let fwd_code = code.clone();
    let fwd_pool = state.pool.clone();
    tokio::spawn(async move {
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
                        if let Ok(fresh) =
                            rooms::room_snapshot(&fwd_pool, &fwd_code, Some(fwd_hub.presence()))
                                .await
                        {
                            let next = fwd_hub.next_seq().await;
                            fwd_hub.publish_snapshot(next, fresh.clone()).await;
                            if fwd_tx.send(WsOut::Snapshot { seq: next, state: fresh }).is_err() {
                                return;
                            }
                        }
                    } else if fwd_tx.send(WsOut::Snapshot { seq, state: snap }).is_err() {
                        return;
                    }
                }
                Err(tokio::sync::broadcast::error::RecvError::Closed) => return,
            }
        }
    });
    // Held from the first successful player hello until this socket drops;
    // admins and cookie connections never count, so theirs stays None.
    let mut present: Option<PresenceGuard> = None;
    while let Some(msg) = receiver.next().await {
        let msg = match msg {
            Ok(m) => m,
            Err(_) => return,
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
                let (seq, snap) = hub.cached().await;
                if snap.is_null() {
                    // First subscriber builds the snapshot once; later ones clone it.
                    match rooms::room_snapshot(&state.pool, &code, None).await {
                        Ok(fresh) => {
                            let next = hub.next_seq().await;
                            hub.publish_snapshot(next, fresh.clone()).await;
                            send_out(&tx_out, WsOut::Snapshot { seq: next, state: fresh });
                        }
                        Err(_) => {
                            send_out(&tx_out, WsOut::error("unknown_room", "unknown room"));
                        }
                    }
                } else {
                    send_out(&tx_out, WsOut::Snapshot { seq, state: snap });
                }
                // First player hello registers presence and republishes the
                // snapshot so the admin counter moves without another write.
                if present.is_none()
                    && matches!(validate(&state.pool, &code, &token).await, Ok(Auth::Player(_)))
                {
                    if let Some((pseq, pstate)) = hub
                        .refresh_presence(&state.pool, &code, 1)
                        .await
                    {
                        send_out(&tx_out, WsOut::Snapshot { seq: pseq, state: pstate });
                        present = Some(PresenceGuard {
                            hub: Arc::clone(&hub),
                            pool: state.pool.clone(),
                            code: code.clone(),
                        });
                    }
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
}
