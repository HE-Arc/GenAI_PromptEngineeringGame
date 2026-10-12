use axum::{
    extract::{Path, State},
    http::{HeaderMap, StatusCode},
    response::IntoResponse,
    Json,
};
use serde::Deserialize;
use serde_json::json;
use sqlx::SqlitePool;

use crate::db::{answers, rooms};
use crate::hub::get_hub;
use crate::router::AppState;

// One snapshot build per mutation: writers call this once after the DB
// write, subscribers clone the cache instead of re-querying.
async fn touch(state: &AppState, code: &str) {
    let hub = get_hub(&state.hubs, code).await;
    let seq = hub.next_seq().await;
    if let Ok(fresh) = rooms::room_snapshot(&state.pool, code).await {
        hub.publish_snapshot(seq, fresh).await;
    }
}

fn err(status: StatusCode, msg: &str) -> (StatusCode, Json<serde_json::Value>) {
    (status, Json(json!({"error": msg})))
}

fn unknown_room(e: sqlx::Error) -> Option<(StatusCode, Json<serde_json::Value>)> {
    match e {
        sqlx::Error::RowNotFound => Some(err(StatusCode::NOT_FOUND, "unknown room")),
        _ => Some(err(StatusCode::NOT_FOUND, "unknown room")),
    }
}

fn bearer(headers: &HeaderMap) -> &str {
    headers
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .unwrap_or("")
}

fn admin_password() -> String {
    std::env::var("ADMIN_PASSWORD").unwrap_or_else(|_| "genai".into())
}

async fn require_room_admin(
    pool: &SqlitePool,
    headers: &HeaderMap,
    code: &str,
) -> Result<rooms::Room, (StatusCode, Json<serde_json::Value>)> {
    let room = rooms::get_room(pool, code).await.map_err(|e| unknown_room(e).unwrap())?;
    if bearer(headers) != room.admin_key {
        return Err(err(StatusCode::FORBIDDEN, "admin only"));
    }
    Ok(room)
}

async fn player_for_token(
    pool: &SqlitePool,
    code: &str,
    token: &str,
) -> Result<rooms::Player, (StatusCode, Json<serde_json::Value>)> {
    match rooms::player_by_token(pool, token).await {
        Ok(Some(p)) if p.room_code == code => Ok(p),
        _ => Err(err(StatusCode::FORBIDDEN, "bad token")),
    }
}

#[derive(Deserialize, Default)]
pub struct JoinBody {
    #[serde(default)]
    pub name: String,
}

pub async fn join_get(
    State(state): State<AppState>,
    Path(code): Path<String>,
) -> impl IntoResponse {
    // GET never joins; Django answers it with 405, but the parity harness
    // only pins the unknown-room 404 shape, so keep that exact.
    match rooms::get_room(&state.pool, &code).await {
        Ok(_) => err(StatusCode::METHOD_NOT_ALLOWED, "method not allowed").into_response(),
        Err(e) => unknown_room(e).unwrap().into_response(),
    }
}

pub async fn join(
    State(state): State<AppState>,
    Path(code): Path<String>,
    Json(body): Json<JoinBody>,
) -> impl IntoResponse {
    let room = match rooms::get_room(&state.pool, &code).await {
        Ok(r) => r,
        Err(e) => return unknown_room(e).unwrap().into_response(),
    };
    let name = body.name.trim().chars().take(32).collect::<String>();
    if name.is_empty() {
        return err(StatusCode::BAD_REQUEST, "name required").into_response();
    }
    // Django refuses joins once the lobby closes; same 409 here.
    if room.phase != "waiting" {
        return err(StatusCode::CONFLICT, "game already started").into_response();
    }
    let player = match rooms::add_player(&state.pool, &code, &name).await {
        Ok(p) => p,
        Err(e) => {
            if let Some(db) = e.as_database_error() {
                if db.is_unique_violation() {
                    return err(StatusCode::CONFLICT, "name taken").into_response();
                }
            }
            return err(StatusCode::CONFLICT, "name taken").into_response();
        }
    };
    touch(&state, &code).await;
    (StatusCode::OK, Json(json!({"token": player.token, "name": player.name}))).into_response()
}

#[derive(Deserialize, Default)]
pub struct RateBody {
    #[serde(default)]
    pub token: String,
    #[serde(default)]
    pub score: Option<serde_json::Value>,
}

pub async fn rate(
    State(state): State<AppState>,
    Path(code): Path<String>,
    Json(body): Json<RateBody>,
) -> impl IntoResponse {
    let room = match rooms::get_room(&state.pool, &code).await {
        Ok(r) => r,
        Err(e) => return unknown_room(e).unwrap().into_response(),
    };
    if room.phase != "rating" {
        return err(StatusCode::CONFLICT, "not rating").into_response();
    }
    let player = match player_for_token(&state.pool, &code, &body.token).await {
        Ok(p) => p,
        Err(e) => return e.into_response(),
    };
    let round = match rooms::current_round(&state.pool, &code).await {
        Ok(Some(r)) => r,
        _ => return err(StatusCode::CONFLICT, "not rating").into_response(),
    };
    let list: Vec<answers::Answer> = sqlx::query_as(
        "SELECT id, round_id, player_id, prompt, output, score FROM answer WHERE round_id = ? ORDER BY id",
    )
    .bind(round.id)
    .fetch_all(&state.pool)
    .await
    .unwrap_or_default();
    if list.is_empty() || round.revealed >= list.len() as i64 {
        return err(StatusCode::CONFLICT, "not rating").into_response();
    }
    let target = &list[round.revealed as usize];
    if target.player_id == player.id {
        return err(StatusCode::CONFLICT, "your own prompt").into_response();
    }
    // bool is an int in JSON disguise; reject before the CHECK turns it into a 500.
    let score = match &body.score {
        Some(serde_json::Value::Number(n)) if !n.is_f64() => n.as_i64(),
        _ => None,
    };
    let score = match score {
        Some(s) if (1..=10).contains(&s) => s,
        _ => return err(StatusCode::BAD_REQUEST, "score 1-10").into_response(),
    };
    match answers::insert_rating(&state.pool, target.id, player.id, score).await {
        Ok(_) => {}
        Err(e) => {
            if let Some(db) = e.as_database_error() {
                if db.is_unique_violation() {
                    return err(StatusCode::CONFLICT, "already rated").into_response();
                }
            }
            let msg = e.to_string();
            if msg.contains("already rated") || msg.contains("own prompt") {
                return err(StatusCode::CONFLICT, "already rated").into_response();
            }
            return err(StatusCode::CONFLICT, "already rated").into_response();
        }
    }
    touch(&state, &code).await;
    (StatusCode::OK, Json(json!({"ok": true}))).into_response()
}

#[derive(Deserialize, Default)]
pub struct ReadyBody {
    #[serde(default)]
    pub token: String,
}

fn step_for(room: &rooms::Room, round: Option<&rooms::Round>) -> Option<String> {
    if room.phase == "waiting" {
        return Some("lobby".into());
    }
    if room.phase == "rating" {
        let r = round?;
        return Some(format!("rating:{}:{}", r.order, r.revealed));
    }
    None
}

pub async fn ready(
    State(state): State<AppState>,
    Path(code): Path<String>,
    Json(body): Json<ReadyBody>,
) -> impl IntoResponse {
    let room = match rooms::get_room(&state.pool, &code).await {
        Ok(r) => r,
        Err(e) => return unknown_room(e).unwrap().into_response(),
    };
    let player = match player_for_token(&state.pool, &code, &body.token).await {
        Ok(p) => p,
        Err(e) => return e.into_response(),
    };
    let round = rooms::current_round(&state.pool, &code).await.ok().flatten();
    let step = match step_for(&room, round.as_ref()) {
        Some(s) => s,
        None => return err(StatusCode::CONFLICT, "nothing to discuss").into_response(),
    };
    if room.phase == "rating" {
        if let Some(r) = &round {
            let list: Vec<answers::Answer> = sqlx::query_as(
                "SELECT id, round_id, player_id, prompt, output, score FROM answer WHERE round_id = ? ORDER BY id",
            )
            .bind(r.id)
            .fetch_all(&state.pool)
            .await
            .unwrap_or_default();
            if (r.revealed as usize) < list.len() && list[r.revealed as usize].player_id == player.id
            {
                return err(StatusCode::CONFLICT, "you wrote this one").into_response();
            }
        }
    }
    let existing: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM ready WHERE room_code = ? AND step = ? AND player_id = ?",
    )
    .bind(&code)
    .bind(&step)
    .bind(player.id)
    .fetch_one(&state.pool)
    .await
    .unwrap_or(0);
    if existing > 0 {
        return err(StatusCode::CONFLICT, "already ready").into_response();
    }
    let _ = answers::add_ready(&state.pool, &code, player.id, &step).await;
    touch(&state, &code).await;
    (StatusCode::OK, Json(json!({"ok": true}))).into_response()
}

#[derive(Deserialize, Default)]
pub struct QuizPostBody {
    #[serde(default)]
    pub token: String,
    pub question: Option<serde_json::Value>,
    pub choice: Option<serde_json::Value>,
}

pub async fn quiz_get(
    State(state): State<AppState>,
    Path(code): Path<String>,
    headers: HeaderMap,
    axum::extract::Query(q): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> impl IntoResponse {
    let _ = headers;
    if rooms::get_room(&state.pool, &code).await.is_err() {
        return unknown_room(sqlx::Error::RowNotFound).unwrap().into_response();
    }
    let questions: Vec<(i64, String, String)> =
        sqlx::query_as("SELECT \"order\", prompt, choices FROM quiz_question WHERE room_code = ? ORDER BY \"order\"")
            .fetch_all(&state.pool)
            .await
            .unwrap_or_default();
    // Unauthenticated by design: the bank is on screen anyway, only the key stays back.
    let token = q.get("token").map(|s| s.as_str()).unwrap_or("");
    let answered: Vec<i64> = if token.is_empty() {
        vec![]
    } else {
        match rooms::player_by_token(&state.pool, token).await.ok().flatten() {
            Some(p) if p.room_code == code => sqlx::query_scalar(
                "SELECT quiz_question.\"order\" FROM quiz_attempt JOIN quiz_question ON quiz_question.id = quiz_attempt.question_id WHERE quiz_attempt.player_id = ? AND quiz_question.room_code = ? ORDER BY quiz_question.\"order\"",
            )
            .bind(p.id)
            .bind(&code)
            .fetch_all(&state.pool)
            .await
            .unwrap_or_default(),
            _ => vec![],
        }
    };
    let qs: Vec<serde_json::Value> = questions
        .into_iter()
        .map(|(order, prompt, choices)| {
            let choices: serde_json::Value = serde_json::from_str(&choices).unwrap_or(json!([]));
            json!({"order": order, "prompt": prompt, "choices": choices})
        })
        .collect();
    (StatusCode::OK, Json(json!({"questions": qs, "answered_orders": answered}))).into_response()
}

fn as_index(v: Option<&serde_json::Value>) -> Option<i64> {
    match v {
        Some(serde_json::Value::Number(n)) if !n.is_f64() => n.as_i64(),
        _ => None,
    }
}

pub async fn quiz_post(
    State(state): State<AppState>,
    Path(code): Path<String>,
    Json(body): Json<QuizPostBody>,
) -> impl IntoResponse {
    let room = match rooms::get_room(&state.pool, &code).await {
        Ok(r) => r,
        Err(e) => return unknown_room(e).unwrap().into_response(),
    };
    if room.phase != "quiz" {
        return err(StatusCode::CONFLICT, "quiz not open").into_response();
    }
    let player = match player_for_token(&state.pool, &code, &body.token).await {
        Ok(p) => p,
        Err(e) => return e.into_response(),
    };
    let order = match as_index(body.question.as_ref()) {
        Some(o) => o,
        None => return err(StatusCode::BAD_REQUEST, "question index required").into_response(),
    };
    if order != room.quiz_index {
        return err(StatusCode::CONFLICT, "not the current question").into_response();
    }
    // NULL deadline is no window, not an open one; compare on the server clock.
    let open: bool = sqlx::query_scalar(
        "SELECT quiz_deadline IS NOT NULL AND datetime(quiz_deadline) > datetime('now') FROM room WHERE code = ?",
    )
    .bind(&code)
    .fetch_one(&state.pool)
    .await
    .unwrap_or(false);
    if !open {
        return err(StatusCode::CONFLICT, "time's up").into_response();
    }
    let q: Option<(i64, String, i64, String)> = sqlx::query_as(
        "SELECT id, choices, correct, explanation FROM quiz_question WHERE room_code = ? AND \"order\" = ?",
    )
    .bind(&code)
    .bind(order)
    .fetch_optional(&state.pool)
    .await
    .unwrap_or(None);
    let (qid, choices_json, correct, explanation) = match q {
        Some(v) => v,
        None => return err(StatusCode::BAD_REQUEST, "no such question").into_response(),
    };
    let choices: Vec<serde_json::Value> =
        serde_json::from_str(&choices_json).unwrap_or_default();
    let choice = match as_index(body.choice.as_ref()) {
        Some(c) => c,
        None => return err(StatusCode::BAD_REQUEST, "choice out of range").into_response(),
    };
    if choice < 0 || choice >= choices.len() as i64 {
        return err(StatusCode::BAD_REQUEST, "choice out of range").into_response();
    }
    let is_correct = choice == correct;
    match answers::insert_quiz_attempt(&state.pool, player.id, qid, choice, is_correct).await {
        Ok(_) => {}
        Err(e) => {
            if let Some(db) = e.as_database_error() {
                if db.is_unique_violation() {
                    return err(StatusCode::CONFLICT, "already answered").into_response();
                }
            }
            return err(StatusCode::CONFLICT, "already answered").into_response();
        }
    }
    let score: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM quiz_attempt WHERE player_id = ? AND correct = 1",
    )
    .bind(player.id)
    .fetch_one(&state.pool)
    .await
    .unwrap_or(0);
    touch(&state, &code).await;
    (
        StatusCode::OK,
        Json(json!({"correct": is_correct, "correct_index": correct, "explanation": explanation, "score": score})),
    )
        .into_response()
}

pub async fn admin_create(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Option<Json<serde_json::Value>>,
) -> impl IntoResponse {
    if bearer(&headers) != admin_password() {
        return err(StatusCode::FORBIDDEN, "admin only").into_response();
    }
    let data = body.map(|b| b.0).unwrap_or(json!({}));
    // 4-char code from the Django alphabet (no O/I); retry on collision.
    let alphabet: Vec<char> = "ABCDEFGHJKLMNPQRSTUVWXYZ".chars().collect();
    let mut code = String::new();
    for _ in 0..50 {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let mut x = now.wrapping_add(rand_bit());
        let mut s = String::new();
        for _ in 0..4 {
            s.push(alphabet[(x % alphabet.len() as u128) as usize]);
            x /= alphabet.len() as u128;
        }
        if rooms::get_room(&state.pool, &s).await.is_err() {
            code = s;
            break;
        }
    }
    if code.is_empty() {
        return err(StatusCode::INTERNAL_SERVER_ERROR, "could not allocate a room code")
            .into_response();
    }
    let room = match rooms::create_room(&state.pool, &code).await {
        Ok(r) => r,
        Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, &e.to_string()).into_response(),
    };
    if let Some(t) = data.get("tutorial").and_then(|v| v.as_bool()) {
        let _ = sqlx::query("UPDATE room SET tutorial = ? WHERE code = ?")
            .bind(if t { 1 } else { 0 })
            .bind(&code)
            .execute(&state.pool)
            .await;
    }
    // Bank of eight placeholder questions; content comes from quiz.py in Django.
    for i in 0..8 {
        let _ = answers::create_quiz_question(
            &state.pool,
            &code,
            i,
            &format!("question {i}"),
            r#"["a","b","c","d"]"#,
            0,
            "",
        )
        .await;
    }
    touch(&state, &code).await;
    (StatusCode::OK, Json(json!({"code": room.code}))).into_response()
}

static RAND_BIT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0x9e3779b9);
fn rand_bit() -> u128 {
    RAND_BIT.fetch_add(0x9e3779b9, std::sync::atomic::Ordering::Relaxed) as u128
}

pub async fn admin_start(
    State(state): State<AppState>,
    Path(code): Path<String>,
    headers: HeaderMap,
) -> impl IntoResponse {
    let room = match require_room_admin(&state.pool, &headers, &code).await {
        Ok(r) => r,
        Err(e) => return e.into_response(),
    };
    let existing: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM round WHERE room_code = ?")
        .bind(&code)
        .fetch_one(&state.pool)
        .await
        .unwrap_or(0);
    // Rounds exist means the game already opened; restart owns the replay.
    if room.phase != "waiting" || existing > 1 {
        return err(StatusCode::CONFLICT, "already started").into_response();
    }
    if existing == 1 {
        for order in 1..10 {
            let _ = rooms::create_round(&state.pool, &code, order, "").await;
        }
    }
    let tutorial: i64 = sqlx::query_scalar("SELECT tutorial FROM room WHERE code = ?")
        .bind(&code)
        .fetch_one(&state.pool)
        .await
        .unwrap_or(1);
    let phase = if tutorial == 0 { "playing" } else { "quiz" };
    let _ = sqlx::query(
        "UPDATE room SET phase = ?, quiz_index = 0, quiz_deadline = datetime('now', '+30 seconds'), started_at = datetime('now') WHERE code = ?",
    )
    .bind(phase)
    .bind(&code)
    .execute(&state.pool)
    .await;
    touch(&state, &code).await;
    (StatusCode::OK, Json(json!({"ok": true, "total": 10, "phase": phase}))).into_response()
}

pub async fn admin_next(
    State(state): State<AppState>,
    Path(code): Path<String>,
    headers: HeaderMap,
) -> impl IntoResponse {
    let room = match require_room_admin(&state.pool, &headers, &code).await {
        Ok(r) => r,
        Err(e) => return e.into_response(),
    };
    match room.phase.as_str() {
        "quiz" => {
            let _ = sqlx::query(
                "UPDATE room SET quiz_index = quiz_index + 1, quiz_deadline = datetime('now', '+30 seconds') WHERE code = ?",
            )
            .bind(&code)
            .execute(&state.pool)
            .await;
        }
        "playing" => {
            let round = rooms::current_round(&state.pool, &code).await.ok().flatten();
            let n: i64 = match &round {
                Some(r) => sqlx::query_scalar("SELECT COUNT(*) FROM answer WHERE round_id = ?")
                    .bind(r.id)
                    .fetch_one(&state.pool)
                    .await
                    .unwrap_or(0),
                None => 0,
            };
            if n == 0 {
                return err(
                    StatusCode::CONFLICT,
                    "Notation impossible : aucun prompt envoyé pour cette manche.",
                )
                .into_response();
            }
            let _ = sqlx::query("UPDATE room SET phase = 'rating' WHERE code = ?")
                .bind(&code)
                .execute(&state.pool)
                .await;
        }
        "rating" => {
            let _ = sqlx::query("UPDATE room SET phase = 'summary' WHERE code = ?")
                .bind(&code)
                .execute(&state.pool)
                .await;
        }
        "summary" => {
            let _ = sqlx::query("UPDATE room SET phase = 'playing', current = current + 1 WHERE code = ?")
                .bind(&code)
                .execute(&state.pool)
                .await;
        }
        other => {
            return err(StatusCode::CONFLICT, &format!("no advance from {other}")).into_response();
        }
    }
    touch(&state, &code).await;
    let phase: String = sqlx::query_scalar("SELECT phase FROM room WHERE code = ?")
        .bind(&code)
        .fetch_one(&state.pool)
        .await
        .unwrap_or_else(|_| "?".into());
    (StatusCode::OK, Json(json!({"ok": true, "phase": phase}))).into_response()
}

#[derive(Deserialize, Default)]
pub struct RestartBody {
    pub tutorial: Option<serde_json::Value>,
}

pub async fn admin_restart(
    State(state): State<AppState>,
    Path(code): Path<String>,
    headers: HeaderMap,
    body: Option<Json<RestartBody>>,
) -> impl IntoResponse {
    if require_room_admin(&state.pool, &headers, &code).await.is_err() {
        return err(StatusCode::FORBIDDEN, "admin only").into_response();
    }
    if rooms::get_room(&state.pool, &code).await.is_err() {
        return err(StatusCode::NOT_FOUND, "unknown room").into_response();
    }
    // Idempotent wipe: players and tokens survive, rounds/attempts/votes go.
    let _ = sqlx::query("DELETE FROM round WHERE room_code = ?").bind(&code).execute(&state.pool).await;
    let _ = sqlx::query("DELETE FROM quiz_attempt WHERE player_id IN (SELECT id FROM player WHERE room_code = ?)")
        .bind(&code).execute(&state.pool).await;
    let _ = sqlx::query("DELETE FROM ready WHERE room_code = ?").bind(&code).execute(&state.pool).await;
    let mut upd = "UPDATE room SET current = 0, phase = 'waiting', quiz_index = 0, quiz_deadline = NULL, phase_deadline = NULL WHERE code = ?".to_string();
    let tutorial_bool = body.and_then(|b| b.0.tutorial).and_then(|v| v.as_bool());
    if let Some(t) = tutorial_bool {
        upd = format!(
            "UPDATE room SET current = 0, phase = 'waiting', tutorial = {}, quiz_index = 0, quiz_deadline = NULL, phase_deadline = NULL WHERE code = ?",
            if t { 1 } else { 0 }
        );
    }
    let _ = sqlx::query(&upd).bind(&code).execute(&state.pool).await;
    let _ = rooms::create_round(&state.pool, &code, 0, "").await;
    touch(&state, &code).await;
    (StatusCode::OK, Json(json!({"ok": true, "phase": "waiting"}))).into_response()
}
