use axum::{
    extract::{Path, State},
    http::{header, HeaderMap, StatusCode},
    response::IntoResponse,
    Json,
};
use serde::Deserialize;
use serde_json::json;
use sqlx::SqlitePool;

use crate::admin_session;
use crate::db::{answers, rooms};
use crate::game;
use crate::hub::get_hub;
use crate::router::AppState;

// One snapshot build per mutation: writers call this once after the DB
// write, subscribers clone the cache instead of re-querying.
async fn touch(state: &AppState, code: &str) {
    let hub = get_hub(&state.hubs, code).await;
    let _ = hub.republish(&state.pool, code).await;
}

async fn online(state: &AppState, code: &str) -> std::collections::HashSet<i64> {
    get_hub(&state.hubs, code).await.online()
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

// Global admin (create/list/login): the session cookie, with the
// password bearer kept so existing integrations keep working.
fn global_admin(headers: &HeaderMap) -> bool {
    admin_session::request_is_admin(headers) || bearer(headers) == admin_password()
}

async fn require_room_admin(
    pool: &SqlitePool,
    headers: &HeaderMap,
    code: &str,
) -> Result<rooms::Room, (StatusCode, Json<serde_json::Value>)> {
    let room = rooms::get_room(pool, code).await.map_err(|e| unknown_room(e).unwrap())?;
    // Session cookie first: the console never holds the per-room key,
    // which only ever travelled as a WS query token before this.
    if admin_session::request_is_admin(headers) {
        return Ok(room);
    }
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
    // Reprise : même pseudo dans la même pièce alors que la partie a commencé —
    // le joueur retrouve son jeton, comme avec /room/XXXX (hello WS).
    if room.phase != "waiting" {
        let existing: Option<rooms::Player> = sqlx::query_as(
            "SELECT id, room_code, token, name FROM player WHERE room_code = ? AND name = ?",
        )
        .bind(&code)
        .bind(&name)
        .fetch_optional(&state.pool)
        .await
        .unwrap_or(None);
        if let Some(p) = existing {
            touch(&state, &code).await;
            return (StatusCode::OK, Json(json!({"token": p.token, "name": p.name}))).into_response();
        }
        // Django refuse les arrivées une fois le lobby fermé ; même 409 ici.
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
    // The reveal index the player was looking at. Optional for old clients;
    // when sent, a rating for a prompt the room has moved past is refused
    // instead of landing on the next prompt nobody has read yet.
    #[serde(default)]
    pub index: Option<i64>,
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
    if body.index.is_some_and(|i| i != round.revealed) {
        return err(StatusCode::CONFLICT, "not the current prompt").into_response();
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
    game::maybe_advance_phase(&state.pool, &code, &online(&state, &code).await).await;
    touch(&state, &code).await;
    (StatusCode::OK, Json(json!({"ok": true}))).into_response()
}

#[derive(Deserialize, Default)]
pub struct ReadyBody {
    #[serde(default)]
    pub token: String,
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
    let step = match game::load(&state.pool, &code).await {
        Ok(g) => game::current_step(&state.pool, &g).await,
        Err(_) => None,
    };
    let Some(step) = step else {
        return err(StatusCode::CONFLICT, "nothing to discuss").into_response();
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
    game::maybe_advance_by_votes(&state.pool, &code, &online(&state, &code).await).await;
    touch(&state, &code).await;
    let ready = game::ready_for(&state, &code, player.id).await;
    (StatusCode::OK, Json(json!({"ok": true, "ready": ready}))).into_response()
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
    // Capped: the bank is one quiz long, but a corrupt room must not hand
    // an unbounded array to every phone that opens it.
    let questions: Vec<(i64, String, String)> =
        sqlx::query_as("SELECT \"order\", prompt, choices FROM quiz_question WHERE room_code = ? ORDER BY \"order\" LIMIT 100")
            .bind(&code)
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
    // NULL deadline is no window, not an open one; same clock as the snapshot.
    let open = game::load(&state.pool, &code).await.is_ok_and(|g| g.quiz_open());
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
    game::maybe_close_quiz_window(&state.pool, &code, &online(&state, &code).await).await;
    touch(&state, &code).await;
    (
        StatusCode::OK,
        Json(json!({"correct": is_correct, "correct_index": correct, "explanation": explanation, "score": score})),
    )
        .into_response()
}

#[derive(Deserialize, Default)]
pub struct LoginBody {
    #[serde(default)]
    pub password: String,
}

// Mirrors Django admin_login: same 403 body on a wrong password, and a
// signed HttpOnly cookie instead of Django's signed-cookie session.
// The cookie also authorises the WS upgrade (Cookie header), which is
// how the console subscribes without ever holding a per-room key.
pub async fn admin_login(Json(body): Json<LoginBody>) -> impl IntoResponse {
    if body.password != admin_password() {
        return err(StatusCode::FORBIDDEN, "bad password").into_response();
    }
    let mut headers = HeaderMap::new();
    if let Ok(v) = admin_session::set_cookie_header().parse() {
        headers.insert(header::SET_COOKIE, v);
    }
    (StatusCode::OK, headers, Json(json!({"ok": true}))).into_response()
}

// Mirrors Django admin_list_rooms payload keys; ordered live-first so
// the console and this list rank rooms the same way.
pub async fn admin_list(State(state): State<AppState>, headers: HeaderMap) -> impl IntoResponse {
    if !global_admin(&headers) {
        return err(StatusCode::FORBIDDEN, "admin only").into_response();
    }
    // Same sweep as the background task, so the console never lists a room
    // the next minute would delete.
    let plan = game::sweep_plan(&state, state.boot).await;
    game::delete_rooms(&state, &plan.dead).await;
    // One grouped read instead of two COUNTs per room: the old loop was 2N+1
    // round-trips and stalled the console behind every classroom at once.
    // Capped: the console pages live rooms, it never needs all of history.
    let rows: Vec<(String, String, i64, i64, i64, i64, i64, i64)> = sqlx::query_as(
        "SELECT room.code, room.phase, room.current, room.tutorial, room.anonymous,
                COALESCE(room.auto_mode, 0),
                COUNT(DISTINCT round.id), COUNT(DISTINCT player.id)
         FROM room
         LEFT JOIN round ON round.room_code = room.code
         LEFT JOIN player ON player.room_code = room.code
         GROUP BY room.code ORDER BY room.code LIMIT 500",
    )
    .fetch_all(&state.pool)
    .await
    .unwrap_or_default();
    let mut out = Vec::with_capacity(rows.len());
    for (code, phase, current, tutorial, anonymous, auto_mode, total, players) in rows.iter().filter(|r| !plan.hide.contains(&r.0)) {
        // Live socket count from the hub; rooms never touched by a WS
        // connection read 0, which is also the truth for them.
        let online = if let Some(hub) = state.hubs.read().await.get(code) {
            hub.presence()
        } else {
            0
        };
        out.push(json!({
            "code": code, "phase": phase, "current": current, "total": total,
            "online_count": online, "players": players,
            "tutorial": *tutorial != 0, "anonymous": *anonymous != 0,
            "auto_mode": *auto_mode != 0,
        }));
    }
    // Live phases first, lobby next, finished last: same ranking the
    // Django list applies, so the console order survives the cutover.
    out.sort_by(|a, b| {
        let rank = |p: &str| match p {
            "quiz" | "playing" | "rating" | "summary" => 0,
            "waiting" => 1,
            "done" => 2,
            _ => 3,
        };
        let ra = rank(a["phase"].as_str().unwrap_or(""));
        let rb = rank(b["phase"].as_str().unwrap_or(""));
        ra.cmp(&rb)
            .then(a["code"].as_str().cmp(&b["code"].as_str()))
    });
    (StatusCode::OK, Json(json!({"rooms": out}))).into_response()
}

pub async fn admin_create(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Option<Json<serde_json::Value>>,
) -> impl IntoResponse {
    if !global_admin(&headers) {
        return err(StatusCode::FORBIDDEN, "admin only").into_response();
    }
    let data = body.map(|b| b.0).unwrap_or(json!({}));
    // 4-char code from the Django alphabet (no O/I); retry on collision.
    let alphabet: Vec<char> = "ABCDEFGHJKLMNPQRSTUVWXYZ".chars().collect();
    let mut code = String::new();
    for _ in 0..50 {
        // OS randomness first so codes are not guessable from the clock; the
        // counter mix keeps colliding processes apart on the same tick.
        let mut seed = [0u8; 16];
        let _ = getrandom::fill(&mut seed);
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let mut x = u128::from_le_bytes(seed).wrapping_add(now).wrapping_add(rand_bit());
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
    let flag = |key: &str| data.get(key).and_then(|v| v.as_bool()).unwrap_or(true);
    let auto_mode = data.get("auto_mode").and_then(|v| v.as_bool()).unwrap_or(false);
    let quiz_seconds = game::clamp_seconds(data.get("quiz_seconds"), game::QUIZ_SECONDS_DEFAULT, game::QUIZ_SECONDS_RANGE);
    let _ = sqlx::query(
        "UPDATE room SET tutorial = ?, anonymous = ?, auto_mode = ?, round_seconds = ?, rate_seconds = ?, quiz_seconds = ? WHERE code = ?",
    )
    .bind(flag("tutorial") as i64)
    .bind(flag("anonymous") as i64)
    .bind(auto_mode as i64)
    .bind(game::clamp_seconds(data.get("round_seconds"), game::ROUND_SECONDS, game::ROUND_SECONDS_RANGE))
    .bind(game::clamp_seconds(data.get("rate_seconds"), game::RATE_SECONDS, game::RATE_SECONDS_RANGE))
    .bind(quiz_seconds)
    .bind(&code)
    .execute(&state.pool)
    .await;
    if let Err(e) = game::create_bank(&state.pool, &code).await {
        return err(StatusCode::INTERNAL_SERVER_ERROR, &e.to_string()).into_response();
    }
    touch(&state, &code).await;
    (StatusCode::OK, Json(json!({"code": room.code, "auto_mode": auto_mode, "quiz_seconds": quiz_seconds}))).into_response()
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
    let _ = room;
    match game::start_game(&state.pool, &code).await {
        Ok(true) => {}
        Ok(false) => return err(StatusCode::CONFLICT, "already started").into_response(),
        Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, &e.to_string()).into_response(),
    }
    touch(&state, &code).await;
    let phase = game::load(&state.pool, &code).await.map(|g| g.phase).unwrap_or_default();
    (StatusCode::OK, Json(json!({"ok": true, "total": game::ROUNDS_PER_GAME, "phase": phase}))).into_response()
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
    // Pendant la notation, le bouton attend la fin du vote : passer au prompt
    // suivant en pleine notation fausserait le dépouillement. La reprise
    // manuelle reste possible une fois le résumé affiché.
    if room.phase == "rating" {
        return err(StatusCode::CONFLICT, "vote en cours").into_response();
    }
    // Rating an unanswered round has no meaning, so the button refuses; the
    // clock path still closes such a round into a spoken summary.
    if room.phase == "playing" && !game::round_has_answers(&state.pool, &code).await {
        return err(
            StatusCode::CONFLICT,
            "Notation impossible : aucun prompt envoyé pour cette manche.",
        )
        .into_response();
    }
    if let Err(e) = game::advance(&state.pool, &code).await {
        return err(StatusCode::CONFLICT, &e).into_response();
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
    if let Err(e) = require_room_admin(&state.pool, &headers, &code).await {
        return e.into_response();
    }
    if rooms::get_room(&state.pool, &code).await.is_err() {
        return err(StatusCode::NOT_FOUND, "unknown room").into_response();
    }
    // Idempotent wipe: players and tokens survive, rounds/attempts/votes go.
    let _ = sqlx::query("DELETE FROM rating WHERE answer_id IN (SELECT answer.id FROM answer JOIN round ON round.id = answer.round_id WHERE round.room_code = ?)")
        .bind(&code).execute(&state.pool).await;
    let _ = sqlx::query("DELETE FROM answer WHERE round_id IN (SELECT id FROM round WHERE room_code = ?)")
        .bind(&code).execute(&state.pool).await;
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
    get_hub(&state.hubs, &code).await.mark_seen();
    touch(&state, &code).await;
    (StatusCode::OK, Json(json!({"ok": true, "phase": "waiting"}))).into_response()
}

// Efface une partie terminée et tout ce qu'elle contient. Même contrôle que
// les autres routes admin : session de la console ou clé de la pièce.
pub async fn admin_delete(
    State(state): State<AppState>,
    Path(code): Path<String>,
    headers: HeaderMap,
) -> impl IntoResponse {
    match require_room_admin(&state.pool, &headers, &code).await {
        Ok(room) => {
            if room.phase != "done" {
                return err(StatusCode::CONFLICT, "partie non terminée").into_response();
            }
        }
        Err(e) => return e.into_response(),
    }
    match rooms::delete_room(&state.pool, &code).await {
        Ok(true) => {}
        Ok(false) => match rooms::get_room(&state.pool, &code).await {
            Err(_) => return err(StatusCode::NOT_FOUND, "unknown room").into_response(),
            Ok(room) if room.phase != "done" => {
                return err(StatusCode::CONFLICT, "partie non terminée").into_response()
            }
            _ => return err(StatusCode::NOT_FOUND, "unknown room").into_response(),
        },
        Err(e) => return err(StatusCode::INTERNAL_SERVER_ERROR, &e.to_string()).into_response(),
    }
    state.hubs.write().await.remove(&code);
    (StatusCode::OK, Json(json!({"ok": true}))).into_response()
}
