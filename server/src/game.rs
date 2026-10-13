// The game engine: what used to live in Django's views.py/models.py
// (start, the didacticiel window, the reveal walk, settling, the votes and
// the full room_state). REST and WS handlers only validate and call in here,
// so the admin button, a class vote and an expired clock all run the same
// transition code.
//
// Clocks: every deadline is written from the Rust clock in epoch ms and read
// back as epoch ms, so `deadline_ms` / `server_ms` in the snapshot and every
// "is the window open" comparison share one time source.
use serde::Deserialize;
use serde_json::{json, Value};
use sqlx::SqlitePool;
use std::collections::{HashMap, HashSet};
use std::sync::OnceLock;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::hub::RoomHub;
use crate::router::AppState;
use std::sync::Arc;

pub const QUIZ_SECONDS: i64 = 30;
pub const ROUNDS_PER_GAME: usize = 10;
pub const ROUND_SECONDS: i64 = 60;
pub const RATE_SECONDS: i64 = 20;
pub const ROUND_SECONDS_RANGE: (i64, i64) = (10, 600);
pub const RATE_SECONDS_RANGE: (i64, i64) = (5, 300);
pub const LOBBY_STEP: &str = "lobby";
// The critic anchors the peers: the retained quality is the peer mean held
// within ±QUALITY_BAND of the critic's own mark.
const QUALITY_BAND: f64 = 3.0;

// Private block the snapshot carries for per-viewer fields. The cached
// snapshot is shared by every socket, so `mine` cannot be computed at build
// time; `personalize` resolves it per socket and strips this key.
const VIEWER_KEY: &str = "_viewer";

// --- static content -------------------------------------------------------

#[derive(Deserialize)]
pub struct QuizItem {
    pub prompt: String,
    pub choices: Vec<String>,
    pub correct: i64,
    pub explanation: String,
}

#[derive(Deserialize)]
pub struct Target {
    pub text: String,
    pub image: Option<String>,
}

fn quiz_bank() -> &'static [QuizItem] {
    static BANK: OnceLock<Vec<QuizItem>> = OnceLock::new();
    BANK.get_or_init(|| {
        serde_json::from_str(include_str!("../data/quiz.json")).expect("data/quiz.json")
    })
}

fn targets() -> &'static [Target] {
    static TARGETS: OnceLock<Vec<Target>> = OnceLock::new();
    TARGETS.get_or_init(|| {
        serde_json::from_str(include_str!("../data/targets.json")).expect("data/targets.json")
    })
}

// Looked up by text, as Django did: a round stores only its target.
pub fn image_for(target: &str) -> Option<String> {
    targets()
        .iter()
        .find(|t| t.text == target)
        .and_then(|t| t.image.clone())
}

// --- scoring rules (prompts.py) -------------------------------------------

pub fn half_up(x: f64) -> i64 {
    (x + 0.5).floor() as i64
}

pub fn quality(llm: Option<i64>, peers: &[i64]) -> i64 {
    if peers.is_empty() {
        return llm.unwrap_or(0);
    }
    let mean = peers.iter().sum::<i64>() as f64 / peers.len() as f64;
    match llm {
        None => half_up(mean),
        Some(l) => {
            let l = l as f64;
            half_up(mean.clamp(l - QUALITY_BAND, l + QUALITY_BAND))
        }
    }
}

// A majority of the players present, for every vote. The lobby used to ask
// for everybody, which let the first few arrivals start the game as soon as
// they were all ready and lock out the rest of the class. `n` is measured
// when the vote is counted, so a player who joins the lobby meanwhile raises
// the bar and the class still needs its majority.
pub fn threshold(n: usize) -> usize {
    if n == 0 {
        0
    } else {
        n / 2 + 1
    }
}

// --- clock ----------------------------------------------------------------

pub fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

const MS_OF: &str = "CAST(ROUND((julianday({}) - 2440587.5) * 86400000.0) AS INTEGER)";
const TS_FROM_MS: &str = "strftime('%Y-%m-%d %H:%M:%f', ? / 1000.0, 'unixepoch')";

fn ms_of(col: &str) -> String {
    MS_OF.replace("{}", col)
}

// --- room row -------------------------------------------------------------

#[derive(Debug, Clone, sqlx::FromRow)]
pub struct GameRoom {
    pub code: String,
    pub phase: String,
    pub current: i64,
    pub tutorial: i64,
    pub anonymous: i64,
    pub quiz_index: i64,
    pub quiz_deadline_ms: Option<i64>,
    pub phase_deadline_ms: Option<i64>,
    pub round_seconds: i64,
    pub rate_seconds: i64,
}

pub async fn load(pool: &SqlitePool, code: &str) -> Result<GameRoom, sqlx::Error> {
    let sql = format!(
        "SELECT code, phase, current, tutorial, anonymous, quiz_index,
                {} AS quiz_deadline_ms, {} AS phase_deadline_ms, round_seconds, rate_seconds
         FROM room WHERE code = ?",
        ms_of("quiz_deadline"),
        ms_of("phase_deadline"),
    );
    sqlx::query_as::<_, GameRoom>(&sql).bind(code).fetch_one(pool).await
}

impl GameRoom {
    pub fn quiz_open(&self) -> bool {
        matches!(self.quiz_deadline_ms, Some(d) if now_ms() < d)
    }
    pub fn phase_expired(&self) -> bool {
        matches!(self.phase_deadline_ms, Some(d) if now_ms() >= d)
    }
}

#[derive(Debug, Clone, sqlx::FromRow)]
struct AnswerRow {
    id: i64,
    player_id: i64,
    prompt: String,
    output: String,
    score: i64,
    quality_llm: Option<i64>,
    quality_comment: String,
    quality_peers: Option<i64>,
    quality: Option<i64>,
    round_score: Option<f64>,
}

async fn round_answers(pool: &SqlitePool, round_id: i64) -> Vec<AnswerRow> {
    sqlx::query_as::<_, AnswerRow>(
        "SELECT id, player_id, prompt, output, score, quality_llm, quality_comment,
                quality_peers, quality, round_score
         FROM answer WHERE round_id = ? ORDER BY id",
    )
    .bind(round_id)
    .fetch_all(pool)
    .await
    .unwrap_or_default()
}

// --- setup ----------------------------------------------------------------

fn random_u64() -> u64 {
    let mut buf = [0u8; 8];
    // Shuffle only: a failed RNG degrades to a time seed, never a panic.
    if getrandom::fill(&mut buf).is_err() {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0x9e3779b9);
        return nanos ^ (nanos >> 29);
    }
    u64::from_le_bytes(buf)
}

// A permutation of the whole bank: the bank is exactly one quiz long, so the
// only variety between two rooms is the order (Fisher-Yates).
pub async fn create_bank(pool: &SqlitePool, code: &str) -> Result<(), sqlx::Error> {
    let bank = quiz_bank();
    let mut order: Vec<usize> = (0..bank.len()).collect();
    for i in (1..order.len()).rev() {
        order.swap(i, (random_u64() % (i as u64 + 1)) as usize);
    }
    for (pos, &i) in order.iter().enumerate() {
        let q = &bank[i];
        crate::db::answers::create_quiz_question(
            pool,
            code,
            pos as i64,
            &q.prompt,
            &serde_json::to_string(&q.choices).unwrap_or_else(|_| "[]".into()),
            q.correct,
            &q.explanation,
        )
        .await?;
    }
    Ok(())
}

// Django's `int(value)`: a number or a numeric string, anything else is the
// default; then clamped to the allowed range.
pub fn clamp_seconds(v: Option<&Value>, default: i64, (lo, hi): (i64, i64)) -> i64 {
    let n = match v {
        Some(Value::Number(n)) => n.as_i64(),
        Some(Value::String(s)) => s.trim().parse::<i64>().ok(),
        _ => None,
    };
    n.map_or(default, |s| s.clamp(lo, hi))
}

// --- transitions (Room methods in models.py) ------------------------------

// EVERY TRANSITION IS A CLAIM. Ratings, votes, answers and the clock all
// race to move the room, and several requests can see "everyone is done" at
// once. Each write is conditional on the state its caller read (phase,
// round, reveal cursor, question), so exactly one of them moves the room and
// the rest are no-ops instead of skipping prompts or rounds.

// Sets the phase and arms its clock in one write: playing and rating are
// timed, every other phase clears the deadline. Applies only if the room is
// still in the phase and round `room` was read in; true when it moved.
async fn go(pool: &SqlitePool, room: &GameRoom, phase: &str) -> Result<bool, sqlx::Error> {
    let seconds = match phase {
        "playing" => Some(room.round_seconds),
        "rating" => Some(room.rate_seconds),
        _ => None,
    };
    let sql = format!(
        "UPDATE room SET phase = ?, phase_deadline = {TS_FROM_MS} WHERE code = ? AND phase = ? AND current = ?"
    );
    let moved = sqlx::query(&sql)
        .bind(phase)
        .bind(seconds.map(|s| now_ms() + s * 1000))
        .bind(&room.code)
        .bind(&room.phase)
        .bind(room.current)
        .execute(pool)
        .await?
        .rows_affected();
    Ok(moved == 1)
}

// Re-arms the clock of the phase the room is already in.
async fn arm(pool: &SqlitePool, room: &GameRoom, phase: &str) -> Result<bool, sqlx::Error> {
    go(pool, room, phase).await
}

// Opens question `index`, if the room is still on question `from`.
async fn open_quiz(pool: &SqlitePool, code: &str, from: i64, index: i64) -> Result<bool, sqlx::Error> {
    let sql = format!(
        "UPDATE room SET quiz_index = ?, quiz_deadline = {TS_FROM_MS} WHERE code = ? AND quiz_index = ?"
    );
    let moved = sqlx::query(&sql)
        .bind(index)
        .bind(now_ms() + QUIZ_SECONDS * 1000)
        .bind(code)
        .bind(from)
        .execute(pool)
        .await?
        .rows_affected();
    Ok(moved == 1)
}

async fn bank_size(pool: &SqlitePool, code: &str) -> i64 {
    sqlx::query_scalar("SELECT COUNT(*) FROM quiz_question WHERE room_code = ?")
        .bind(code)
        .fetch_one(pool)
        .await
        .unwrap_or(0)
}

// Closing early only moves the deadline into the past: the reveal is derived
// from it, so nothing else has to change.
async fn close_quiz(pool: &SqlitePool, room: &GameRoom) -> Result<(), sqlx::Error> {
    if !room.quiz_open() {
        return Ok(());
    }
    let sql = format!("UPDATE room SET quiz_deadline = {TS_FROM_MS} WHERE code = ?");
    sqlx::query(&sql).bind(now_ms()).bind(&room.code).execute(pool).await?;
    Ok(())
}

// Draws the ten rounds and leaves the lobby. False when the room had already
// begun: the conditional UPDATE is the claim, so the admin's button and the
// last lobby vote landing together start the game once.
pub async fn start_game(pool: &SqlitePool, code: &str) -> Result<bool, sqlx::Error> {
    let room = load(pool, code).await?;
    let phase = if room.tutorial != 0 { "quiz" } else { "playing" };
    let mut tx = pool.begin().await?;
    let claimed = sqlx::query(
        "UPDATE room SET phase = ?, current = 0, started_at = datetime('now') WHERE code = ? AND phase = 'waiting'",
    )
    .bind(phase)
    .bind(code)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    if claimed != 1 {
        return Ok(false);
    }
    // The placeholder order-0 round from creation/restart is replaced by the
    // real draw: the first ROUNDS_PER_GAME targets, in file order.
    sqlx::query("DELETE FROM round WHERE room_code = ?")
        .bind(code)
        .execute(&mut *tx)
        .await?;
    for (i, t) in targets().iter().take(ROUNDS_PER_GAME).enumerate() {
        sqlx::query("INSERT INTO round (room_code, \"order\", target) VALUES (?, ?, ?)")
            .bind(code)
            .bind(i as i64)
            .bind(&t.text)
            .execute(&mut *tx)
            .await?;
    }
    tx.commit().await?;
    let room = load(pool, code).await?;
    if phase == "quiz" {
        open_quiz(pool, code, room.quiz_index, 0).await?;
    }
    arm(pool, &room, phase).await?;
    Ok(true)
}

async fn open_rating(pool: &SqlitePool, room: &GameRoom) -> Result<(), sqlx::Error> {
    if go(pool, room, "rating").await? {
        sqlx::query("UPDATE round SET revealed = 0 WHERE room_code = ? AND \"order\" = ?")
            .bind(&room.code)
            .bind(room.current)
            .execute(pool)
            .await?;
    }
    Ok(())
}

async fn current_round(pool: &SqlitePool, room: &GameRoom) -> Option<(i64, i64)> {
    sqlx::query_as("SELECT id, revealed FROM round WHERE room_code = ? AND \"order\" = ?")
        .bind(&room.code)
        .bind(room.current)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten()
}

// `revealed` is the cursor the CALLER checked (prompt N is complete). It is
// the claim value: re-reading the cursor here would let a caller that saw N
// complete push N+1 → N+2 after another caller already moved N → N+1.
async fn advance_reveal_and_settle(
    pool: &SqlitePool,
    room: &GameRoom,
    revealed: i64,
) -> Result<(), sqlx::Error> {
    let Some((round_id, _)) = current_round(pool, room).await else {
        return Ok(());
    };
    if room.phase != "rating" {
        return Ok(());
    }
    let count = round_answers(pool, round_id).await.len() as i64;
    if revealed + 1 < count {
        // The cursor read is the claim: two callers that both saw prompt N
        // complete move it to N+1 once, never to N+2.
        let moved = sqlx::query("UPDATE round SET revealed = revealed + 1 WHERE id = ? AND revealed = ?")
            .bind(round_id)
            .bind(revealed)
            .execute(pool)
            .await?
            .rows_affected();
        if moved == 1 {
            arm(pool, room, "rating").await?;
        }
        Ok(())
    } else if go(pool, room, "summary").await? {
        settle_round(pool, room).await
    } else {
        Ok(())
    }
}

// Written once when the rating closes; every later snapshot sums the
// stored columns. Idempotent: same rows in, same numbers out.
async fn settle_round(pool: &SqlitePool, room: &GameRoom) -> Result<(), sqlx::Error> {
    let Some((round_id, _)) = current_round(pool, room).await else {
        return Ok(());
    };
    let answers = round_answers(pool, round_id).await;
    if answers.is_empty() {
        return Ok(());
    }
    // One grouped read instead of one SELECT per answer: a 30-player settle
    // is two round-trips under contention, not thirty-plus.
    let ids: Vec<String> = answers.iter().map(|a| a.id.to_string()).collect();
    let ratings: Vec<(i64, i64)> = sqlx::query_as(&format!(
        "SELECT answer_id, score FROM rating WHERE answer_id IN ({})",
        ids.join(",")
    ))
    .fetch_all(pool)
    .await
    .unwrap_or_default();
    let mut by_answer: HashMap<i64, Vec<i64>> = HashMap::new();
    for (id, score) in ratings {
        by_answer.entry(id).or_default().push(score);
    }
    // One transaction instead of N autocommits: a single WAL write, and a
    // concurrent reader never sees a half-settled round.
    let mut tx = pool.begin().await?;
    for a in &answers {
        let scores: &[i64] = by_answer.get(&a.id).map(Vec::as_slice).unwrap_or(&[]);
        let peers =
            (!scores.is_empty()).then(|| half_up(scores.iter().sum::<i64>() as f64 / scores.len() as f64));
        let q = quality(a.quality_llm, scores);
        // Weighted 1/3 proximity, 2/3 quality: the game teaches prompting,
        // so a lucky output must not outweigh a well-built prompt.
        let round_score = (a.score as f64 + 2.0 * q as f64) / 3.0;
        sqlx::query("UPDATE answer SET quality_peers = ?, quality = ?, round_score = ? WHERE id = ?")
            .bind(peers)
            .bind(q)
            .bind(round_score)
            .bind(a.id)
            .execute(&mut *tx)
            .await?;
    }
    tx.commit().await?;
    Ok(())
}

async fn next_round(pool: &SqlitePool, room: &GameRoom) -> Result<(), sqlx::Error> {
    let rounds: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM round WHERE room_code = ?")
        .bind(&room.code)
        .fetch_one(pool)
        .await?;
    if room.current + 1 < rounds {
        let sql = format!(
            "UPDATE room SET current = current + 1, phase = 'playing', phase_deadline = {TS_FROM_MS}
             WHERE code = ? AND phase = 'summary' AND current = ?"
        );
        sqlx::query(&sql)
            .bind(now_ms() + room.round_seconds * 1000)
            .bind(&room.code)
            .bind(room.current)
            .execute(pool)
            .await?;
        Ok(())
    } else {
        go(pool, room, "done").await.map(|_| ())
    }
}

// The admin's one "next" button, and the transition a class vote runs: the
// same table so the two paths cannot drift.
pub async fn advance(pool: &SqlitePool, code: &str) -> Result<(), String> {
    let room = load(pool, code).await.map_err(|e| e.to_string())?;
    let r = match room.phase.as_str() {
        "quiz" => {
            let next = room.quiz_index + 1;
            if next < bank_size(pool, code).await {
                open_quiz(pool, code, room.quiz_index, next).await.map(|_| ())
            } else {
                go(pool, &room, "playing").await.map(|_| ())
            }
        }
        "playing" => open_rating(pool, &room).await,
        "rating" => match current_round(pool, &room).await {
            Some((_, revealed)) => advance_reveal_and_settle(pool, &room, revealed).await,
            None => Ok(()),
        },
        "summary" => next_round(pool, &room).await,
        other => return Err(format!("no advance from {other}")),
    };
    r.map_err(|e| e.to_string())
}

pub async fn round_has_answers(pool: &SqlitePool, code: &str) -> bool {
    let Ok(room) = load(pool, code).await else { return false };
    match current_round(pool, &room).await {
        Some((id, _)) => !round_answers(pool, id).await.is_empty(),
        None => false,
    }
}

// --- automatic advances ---------------------------------------------------

// Every online player answered the question on screen: close the window
// early (reveal now). Never advances — the next question is the admin's.
pub async fn maybe_close_quiz_window(pool: &SqlitePool, code: &str, online: &HashSet<i64>) {
    if online.is_empty() {
        return;
    }
    let Ok(room) = load(pool, code).await else { return };
    if room.phase != "quiz" {
        return;
    }
    let answered: HashSet<i64> = sqlx::query_scalar(
        "SELECT quiz_attempt.player_id FROM quiz_attempt
         JOIN quiz_question ON quiz_question.id = quiz_attempt.question_id
         WHERE quiz_question.room_code = ? AND quiz_question.\"order\" = ?",
    )
    .bind(code)
    .bind(room.quiz_index)
    .fetch_all(pool)
    .await
    .unwrap_or_default()
    .into_iter()
    .collect();
    if online.is_subset(&answered) {
        let _ = close_quiz(pool, &room).await;
    }
}

// Playing: everybody online wrote a prompt → rating. Rating: every online
// rater (author excluded) rated the prompt on screen → next prompt. An empty
// rater set never advances: a room must not run itself.
pub async fn maybe_advance_phase(pool: &SqlitePool, code: &str, online: &HashSet<i64>) {
    if online.is_empty() {
        return;
    }
    let Ok(room) = load(pool, code).await else { return };
    let Some((round_id, revealed)) = current_round(pool, &room).await else {
        return;
    };
    let answers = round_answers(pool, round_id).await;
    match room.phase.as_str() {
        "playing" => {
            let wrote: HashSet<i64> = answers.iter().map(|a| a.player_id).collect();
            if online.is_subset(&wrote) {
                let _ = open_rating(pool, &room).await;
            }
        }
        "rating" => {
            let Some(target) = answers.get(revealed as usize) else { return };
            let raters: HashSet<i64> =
                online.iter().copied().filter(|&p| p != target.player_id).collect();
            if raters.is_empty() {
                return;
            }
            let voted: HashSet<i64> =
                sqlx::query_scalar("SELECT rater_id FROM rating WHERE answer_id = ?")
                    .bind(target.id)
                    .fetch_all(pool)
                    .await
                    .unwrap_or_default()
                    .into_iter()
                    .collect();
            if raters.is_subset(&voted) {
                let _ = advance_reveal_and_settle(pool, &room, revealed).await;
            }
        }
        _ => {}
    }
}

// The step the class may vote on: the lobby, or the prompt being rated.
pub async fn current_step(pool: &SqlitePool, room: &GameRoom) -> Option<String> {
    match room.phase.as_str() {
        "waiting" => Some(LOBBY_STEP.into()),
        "rating" => {
            let order = room.current;
            current_round(pool, room)
                .await
                .map(|(_, revealed)| format!("rating:{order}:{revealed}"))
        }
        _ => None,
    }
}

// Online players minus the author of the prompt on screen: count and needed
// are measured over this one population, or the bar can never fill.
async fn ready_voters(pool: &SqlitePool, room: &GameRoom, online: &HashSet<i64>) -> HashSet<i64> {
    let mut voters = online.clone();
    if room.phase == "rating" {
        if let Some((round_id, revealed)) = current_round(pool, room).await {
            if let Some(a) = round_answers(pool, round_id).await.get(revealed as usize) {
                voters.remove(&a.player_id);
            }
        }
    }
    voters
}

async fn ready_ids(pool: &SqlitePool, code: &str, step: &str) -> HashSet<i64> {
    sqlx::query_scalar("SELECT player_id FROM ready WHERE room_code = ? AND step = ?")
        .bind(code)
        .bind(step)
        .fetch_all(pool)
        .await
        .unwrap_or_default()
        .into_iter()
        .collect()
}

pub async fn maybe_advance_by_votes(pool: &SqlitePool, code: &str, online: &HashSet<i64>) {
    if online.is_empty() {
        return;
    }
    let Ok(room) = load(pool, code).await else { return };
    let Some(step) = current_step(pool, &room).await else { return };
    let voters = ready_voters(pool, &room, online).await;
    let needed = threshold(voters.len());
    if needed == 0 {
        return;
    }
    let count = ready_ids(pool, code, &step).await.intersection(&voters).count();
    if count < needed {
        return;
    }
    if step == LOBBY_STEP {
        let _ = start_game(pool, code).await;
    } else if let Some(revealed) = step.rsplit(':').next().and_then(|r| r.parse().ok()) {
        // The vote was counted on `rating:<round>:<revealed>`: claim that cursor.
        let _ = advance_reveal_and_settle(pool, &room, revealed).await;
    }
}

// The phase clock ran out. Playing waits for streams still in flight so an
// answer that beat the clock is never lost; the deadline is claimed with a
// conditional UPDATE so concurrent callers run the transition once.
pub async fn expire_phase(state: &AppState, code: &str) -> bool {
    let pool = &state.pool;
    let Ok(room) = load(pool, code).await else { return false };
    if !room.phase_expired() || !matches!(room.phase.as_str(), "playing" | "rating") {
        return false;
    }
    let Some((round_id, revealed)) = current_round(pool, &room).await else {
        return false;
    };
    if room.phase == "playing" {
        let hub = crate::hub::get_hub(&state.hubs, code).await;
        if hub.round_inflight(round_id).await {
            return false;
        }
    }
    let claimed = sqlx::query(
        "UPDATE room SET phase_deadline = NULL WHERE code = ? AND phase = ? AND phase_deadline IS NOT NULL",
    )
    .bind(code)
    .bind(&room.phase)
    .execute(pool)
    .await
    .map(|r| r.rows_affected())
    .unwrap_or(0);
    if claimed != 1 {
        return false;
    }
    if room.phase == "rating" {
        let _ = advance_reveal_and_settle(pool, &room, revealed).await;
    } else if round_answers(pool, round_id).await.is_empty() {
        if go(pool, &room, "summary").await.unwrap_or(false) {
            let _ = settle_round(pool, &room).await;
        }
    } else {
        let _ = open_rating(pool, &room).await;
    }
    true
}

// Clocks move the game without any request: deadlines expire and quiz
// windows close on time. Django rebuilt the snapshot every 0.5 s per stream;
// here one task wakes every second and republishes only rooms whose clock
// actually crossed a deadline.
pub fn spawn_ticker(state: AppState) {
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(Duration::from_secs(1));
        // Quiz deadline already announced as revealed, per room.
        let mut announced: HashMap<String, i64> = HashMap::new();
        loop {
            tick.tick().await;
            // Bounded scan: without LIMIT one stuck room table makes every
            // tick linear in dead rooms the sweeper has not deleted yet.
            let sql = format!(
                "SELECT code, phase, {} AS q, {} AS p FROM room WHERE phase IN ('quiz', 'playing', 'rating') LIMIT 1000",
                ms_of("quiz_deadline"),
                ms_of("phase_deadline"),
            );
            let rows: Vec<(String, String, Option<i64>, Option<i64>)> =
                sqlx::query_as(&sql).fetch_all(&state.pool).await.unwrap_or_default();
            let now = now_ms();
            for (code, phase, quiz_deadline, phase_deadline) in rows {
                let mut changed = false;
                if matches!(phase_deadline, Some(d) if now >= d) {
                    changed |= expire_phase(&state, &code).await;
                }
                if phase == "quiz" {
                    if let Some(d) = quiz_deadline {
                        if now >= d && announced.get(&code) != Some(&d) {
                            announced.insert(code.clone(), d);
                            changed = true;
                        }
                    }
                }
                if changed {
                    let hub = crate::hub::get_hub(&state.hubs, &code).await;
                    let _ = hub.republish(&state.pool, &code).await;
                }
            }
            // An expired failure notice leaves the screen on its own.
            let hubs: Vec<(String, Arc<RoomHub>)> = state
                .hubs
                .read()
                .await
                .iter()
                .map(|(c, h)| (c.clone(), Arc::clone(h)))
                .collect();
            for (code, hub) in hubs {
                if hub.error_needs_clearing() {
                    let _ = hub.republish(&state.pool, &code).await;
                }
            }
        }
    });
}

// --- idle rooms (Django's room sweep) -------------------------------------

// No player online and not opened for this long: hidden from the console.
pub const ROOM_IDLE_TTL: Duration = Duration::from_secs(600);
// A started room idle past this (since its last activity) is deleted; a
// never-started idle room is deleted as soon as it is hidden.
pub const ROOM_KEEP_TTL: Duration = Duration::from_secs(1800);
const SWEEP_EVERY: Duration = Duration::from_secs(60);

pub struct Sweep {
    pub hide: HashSet<String>,
    pub dead: Vec<String>,
    pub active: HashSet<String>,
}

#[derive(sqlx::FromRow)]
struct SweepRow {
    code: String,
    started: bool,
    // Seconds since last_activity_at (or started_at), None if never started.
    activity_age: Option<f64>,
}

// Which rooms are idle (hidden) and which are dead (deleted). A room the
// process has no hub for counts as idle since `boot`: a restart must not
// make every room look freshly opened, nor delete them all at once.
pub async fn sweep_plan(state: &AppState, boot: std::time::Instant) -> Sweep {
    // Bounded scan for the same reason as the ticker: the sweep must stay
    // O(active), not O(every room ever created).
    let rows: Vec<SweepRow> = sqlx::query_as(
        "SELECT code, started_at IS NOT NULL AS started,
                (julianday('now') - julianday(COALESCE(last_activity_at, started_at))) * 86400.0 AS activity_age
         FROM room LIMIT 5000",
    )
    .fetch_all(&state.pool)
    .await
    .unwrap_or_default();
    let hubs = state.hubs.read().await;
    let mut plan = Sweep { hide: HashSet::new(), dead: Vec::new(), active: HashSet::new() };
    for row in rows {
        let hub = hubs.get(&row.code);
        if hub.is_some_and(|h| h.presence() > 0) {
            plan.active.insert(row.code);
            continue;
        }
        let idle = hub.map_or_else(|| boot.elapsed(), |h| h.idle_for());
        if idle <= ROOM_IDLE_TTL {
            continue;
        }
        plan.hide.insert(row.code.clone());
        let stale = row.activity_age.is_some_and(|age| age > ROOM_KEEP_TTL.as_secs_f64());
        if !row.started || stale {
            plan.dead.push(row.code);
        }
    }
    plan
}

pub async fn delete_rooms(state: &AppState, codes: &[String]) {
    for code in codes {
        // Children explicitly: the cascade only exists where foreign_keys is on.
        for sql in [
            "DELETE FROM rating WHERE answer_id IN (SELECT answer.id FROM answer JOIN round ON round.id = answer.round_id WHERE round.room_code = ?)",
            "DELETE FROM answer WHERE round_id IN (SELECT id FROM round WHERE room_code = ?)",
            "DELETE FROM quiz_attempt WHERE question_id IN (SELECT id FROM quiz_question WHERE room_code = ?)",
            "DELETE FROM ready WHERE room_code = ?",
            "DELETE FROM round WHERE room_code = ?",
            "DELETE FROM quiz_question WHERE room_code = ?",
            "DELETE FROM player WHERE room_code = ?",
            "DELETE FROM room WHERE code = ?",
        ] {
            let _ = sqlx::query(sql).bind(code).execute(&state.pool).await;
        }
        state.hubs.write().await.remove(code);
    }
}

// Every minute: stamp activity on rooms with someone online, delete dead ones.
pub fn spawn_sweeper(state: AppState) {
    let boot = state.boot;
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(SWEEP_EVERY);
        loop {
            tick.tick().await;
            let plan = sweep_plan(&state, boot).await;
            for code in &plan.active {
                let _ = sqlx::query("UPDATE room SET last_activity_at = datetime('now') WHERE code = ?")
                    .bind(code)
                    .execute(&state.pool)
                    .await;
            }
            delete_rooms(&state, &plan.dead).await;
        }
    });
}

// The `ready` block as one player sees it, for the POST response.
pub async fn ready_for(state: &AppState, code: &str, player_id: i64) -> Value {
    let online = crate::hub::get_hub(&state.hubs, code).await.online();
    match room_snapshot(&state.pool, code, Some(&online)).await {
        Ok(snap) => personalize(&snap, Some(player_id))["ready"].clone(),
        Err(_) => Value::Null,
    }
}

// --- snapshot (room_state) ------------------------------------------------

#[derive(sqlx::FromRow)]
struct PlayerRow {
    id: i64,
    name: String,
}

// Pseudonyms by arrival order (`Joueur N`), fixed for the whole game.
fn labels(players: &[PlayerRow], anonymous: bool) -> HashMap<i64, String> {
    players
        .iter()
        .enumerate()
        .map(|(i, p)| {
            let label = if anonymous { format!("Joueur {}", i + 1) } else { p.name.clone() };
            (p.id, label)
        })
        .collect()
}

// `online` None means "no hub knowledge": every player reads as online.
pub async fn room_snapshot(
    pool: &SqlitePool,
    code: &str,
    online: Option<&HashSet<i64>>,
) -> Result<Value, sqlx::Error> {
    let room = load(pool, code).await?;
    let now = now_ms();
    let anonymous = room.anonymous != 0;
    let players: Vec<PlayerRow> = sqlx::query_as(
        "SELECT id, name FROM player WHERE room_code = ? ORDER BY created_at, id",
    )
    .bind(code)
    .fetch_all(pool)
    .await?;
    let is_online = |id: i64| online.is_none_or(|s| s.contains(&id));
    let online_ids: HashSet<i64> = players.iter().map(|p| p.id).filter(|&id| is_online(id)).collect();

    let total_rounds: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM round WHERE room_code = ?")
        .bind(code)
        .fetch_one(pool)
        .await
        .unwrap_or(0);
    // Round id, reveal cursor and target in one row: the old code re-fetched
    // the target by id, doubling the round lookups on every republish.
    let round_row: Option<(i64, i64, String)> = sqlx::query_as(
        "SELECT id, revealed, target FROM round WHERE room_code = ? AND \"order\" = ?",
    )
    .bind(code)
    .bind(room.current)
    .fetch_optional(pool)
    .await
    .ok()
    .flatten();
    let round: Option<(i64, i64)> = round_row.as_ref().map(|(id, revealed, _)| (*id, *revealed));
    let target: Option<String> = round_row
        .and_then(|(_, _, t)| (!t.is_empty()).then_some(t));
    let answers = match &round {
        Some((id, _)) => round_answers(pool, *id).await,
        None => Vec::new(),
    };
    let bank = bank_size(pool, code).await;

    // Per-player aggregates in one pass each rather than one query per player.
    // player id -> (correct answers, questions attempted)
    let quiz_scores: HashMap<i64, (i64, i64)> = sqlx::query_as(
        "SELECT quiz_attempt.player_id, SUM(quiz_attempt.correct), COUNT(*) FROM quiz_attempt
         JOIN player ON player.id = quiz_attempt.player_id
         WHERE player.room_code = ? GROUP BY quiz_attempt.player_id",
    )
    .bind(code)
    .fetch_all(pool)
    .await
    .unwrap_or_default()
    .into_iter()
    .map(|(id, correct, n): (i64, i64, i64)| (id, (correct, n)))
    .collect();
    #[derive(sqlx::FromRow)]
    struct Totals {
        player_id: i64,
        settled: Option<f64>,
        open_round: Option<i64>,
        proximity: Option<i64>,
        quality: Option<i64>,
    }
    let totals: HashMap<i64, Totals> = sqlx::query_as::<_, Totals>(
        "SELECT answer.player_id,
                SUM(answer.round_score) AS settled,
                SUM(CASE WHEN answer.round_score IS NULL THEN answer.score END) AS open_round,
                SUM(CASE WHEN answer.round_score IS NOT NULL THEN answer.score END) AS proximity,
                SUM(CASE WHEN answer.round_score IS NOT NULL THEN answer.quality END) AS quality
         FROM answer JOIN player ON player.id = answer.player_id
         WHERE player.room_code = ? GROUP BY answer.player_id",
    )
    .bind(code)
    .fetch_all(pool)
    .await
    .unwrap_or_default()
    .into_iter()
    .map(|t| (t.player_id, t))
    .collect();

    // The rating phase blanks every score: a player who sees their own total
    // could cross-reference which anonymous prompt is theirs.
    let hiding = room.phase == "rating";
    let quiz_of = |id: i64| quiz_scores.get(&id).copied().unwrap_or((0, 0));
    let mut rows: Vec<(f64, usize, Value)> = players
        .iter()
        .enumerate()
        .map(|(joined, p)| {
            let (quiz_score, attempts) = quiz_of(p.id);
            let t = totals.get(&p.id);
            let total = quiz_score as f64
                + t.and_then(|t| t.settled).unwrap_or(0.0)
                + t.and_then(|t| t.open_round).unwrap_or(0) as f64;
            let mine = answers.iter().find(|a| a.player_id == p.id);
            let row = json!({
                "name": p.name,
                "score": if hiding { Value::Null } else { json!(total) },
                "answered": mine.is_some(),
                "round_score": if hiding { Value::Null } else { json!(mine.map(|a| a.score)) },
                "proximity_total": if hiding { Value::Null } else { json!(t.and_then(|t| t.proximity)) },
                "quality_total": if hiding { Value::Null } else { json!(t.and_then(|t| t.quality)) },
                "quiz_score": quiz_score,
                "quiz_done": bank > 0 && attempts == bank,
                "online": online_ids.contains(&p.id),
            });
            (if hiding { 0.0 } else { total }, joined, row)
        })
        .collect();
    rows.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal).then(a.1.cmp(&b.1)));

    let quiz = if room.phase == "quiz" {
        let revealed = matches!(room.quiz_deadline_ms, Some(d) if now >= d);
        let question: Option<(i64, String)> = sqlx::query_as(
            "SELECT correct, explanation FROM quiz_question WHERE room_code = ? AND \"order\" = ?",
        )
        .bind(code)
        .bind(room.quiz_index)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten();
        let done: HashSet<i64> = sqlx::query_scalar(
            "SELECT quiz_attempt.player_id FROM quiz_attempt
             JOIN quiz_question ON quiz_question.id = quiz_attempt.question_id
             WHERE quiz_question.room_code = ? AND quiz_question.\"order\" = ?",
        )
        .bind(code)
        .bind(room.quiz_index)
        .fetch_all(pool)
        .await
        .unwrap_or_default()
        .into_iter()
        .collect();
        // The key is public only once the window closed: before that it
        // would be readable in devtools on every phone.
        let key = question.filter(|_| revealed);
        json!({
            "index": room.quiz_index,
            "total": bank,
            "deadline_ms": room.quiz_deadline_ms.unwrap_or(0),
            "server_ms": now,
            "revealed": revealed,
            "correct": key.as_ref().map(|k| k.0),
            "explanation": key.as_ref().map(|k| k.1.clone()),
            "done": done.intersection(&online_ids).count(),
            "online_count": online_ids.len(),
            "score": online_ids.iter().map(|&id| quiz_of(id).0).max().unwrap_or(0),
        })
    } else {
        Value::Null
    };

    let mut viewer = json!({});
    let reveal = match (&round, room.phase.as_str()) {
        (Some((_, revealed)), "rating") => match answers.get(*revealed as usize) {
            Some(a) => {
                let names: HashMap<i64, &str> =
                    players.iter().map(|p| (p.id, p.name.as_str())).collect();
                let rated: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM rating WHERE answer_id = ?")
                    .bind(a.id)
                    .fetch_one(pool)
                    .await
                    .unwrap_or(0);
                viewer["author_id"] = json!(a.player_id);
                json!({
                    "index": revealed,
                    "count": answers.len(),
                    "prompt": a.prompt,
                    "author": if anonymous { Value::Null } else { json!(names.get(&a.player_id)) },
                    "author_label": labels(&players, anonymous).get(&a.player_id),
                    "rated": rated,
                    "mine": false,
                })
            }
            None => Value::Null,
        },
        _ => Value::Null,
    };

    let summary = match (&round, room.phase.as_str()) {
        (Some(_), "summary") => {
            let names: HashMap<i64, &str> = players.iter().map(|p| (p.id, p.name.as_str())).collect();
            json!({
                "round": room.current,
                "rows": answers.iter().map(|a| json!({
                    "author": names.get(&a.player_id),
                    "prompt": a.prompt,
                    "output": a.output,
                    "proximity": a.score,
                    "quality_llm": a.quality_llm,
                    "quality_comment": a.quality_comment,
                    "quality_peers": a.quality_peers,
                    "quality": a.quality,
                    "round_score": a.round_score,
                })).collect::<Vec<_>>(),
            })
        }
        _ => Value::Null,
    };

    let ready = match current_step(pool, &room).await {
        Some(step) => {
            let voters = ready_voters(pool, &room, &online_ids).await;
            let ids = ready_ids(pool, code, &step).await;
            viewer["ready_ids"] = json!(ids.iter().collect::<Vec<_>>());
            json!({
                "step": step,
                "count": ids.intersection(&voters).count(),
                "needed": threshold(voters.len()),
                "mine": false,
            })
        }
        None => Value::Null,
    };

    let timer = room
        .phase_deadline_ms
        .map(|d| json!({"deadline_ms": d, "server_ms": now}))
        .unwrap_or(Value::Null);
    let online_count = rows.iter().filter(|r| r.2["online"] == true).count();
    Ok(json!({
        "code": room.code,
        "phase": room.phase,
        "current": room.current,
        "total": total_rounds,
        "target_image": target.as_deref().and_then(image_for),
        "target": target,
        "timer": timer,
        "anonymous": anonymous,
        "players": rows.into_iter().map(|r| r.2).collect::<Vec<_>>(),
        "online_count": online_count,
        "quiz": quiz,
        "reveal": reveal,
        "summary": summary,
        "ready": ready,
        "error": Value::Null,
        VIEWER_KEY: viewer,
    }))
}

// Resolves the per-viewer `mine` flags for one socket and strips the private
// block. `viewer` None (admin, spectator) owns nothing: both flags false.
pub fn personalize(state: &Value, viewer: Option<i64>) -> Value {
    let mut out = state.clone();
    let private = out
        .as_object_mut()
        .and_then(|o| o.remove(VIEWER_KEY))
        .unwrap_or(Value::Null);
    if let Some(me) = viewer {
        if out["reveal"].is_object() {
            out["reveal"]["mine"] = json!(private["author_id"].as_i64() == Some(me));
        }
        if out["ready"].is_object() {
            let mine = private["ready_ids"]
                .as_array()
                .is_some_and(|ids| ids.iter().any(|v| v.as_i64() == Some(me)));
            out["ready"]["mine"] = json!(mine);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn content_loads() {
        assert_eq!(quiz_bank().len(), 8);
        assert!(targets().len() >= ROUNDS_PER_GAME);
        assert!(quiz_bank().iter().all(|q| q.choices.len() == 4 && (0..4).contains(&q.correct)));
    }

    #[test]
    fn quality_band_and_rounding() {
        assert_eq!(quality(Some(5), &[]), 5);
        assert_eq!(quality(None, &[6, 7]), 7); // 6.5 rounds half up
        assert_eq!(quality(Some(2), &[10, 10]), 5); // held within +3 of the critic
        assert_eq!(threshold(3), 2);
        assert_eq!(threshold(4), 3);
        assert_eq!(threshold(0), 0);
    }

    #[tokio::test]
    async fn quiz_flow_reveals_key_only_after_deadline() {
        let pool = crate::db::test_pool().await;
        crate::db::rooms::create_room(&pool, "QZ11").await.unwrap();
        create_bank(&pool, "QZ11").await.unwrap();
        let ana = crate::db::rooms::add_player(&pool, "QZ11", "ana").await.unwrap();
        assert!(start_game(&pool, "QZ11").await.unwrap());
        assert!(!start_game(&pool, "QZ11").await.unwrap(), "second start is a no-op");

        let online: HashSet<i64> = [ana.id].into();
        let snap = room_snapshot(&pool, "QZ11", Some(&online)).await.unwrap();
        assert_eq!(snap["phase"], "quiz");
        assert_eq!(snap["total"], 10);
        assert_eq!(snap["quiz"]["index"], 0);
        assert_eq!(snap["quiz"]["total"], 8);
        assert_eq!(snap["quiz"]["revealed"], false);
        assert!(snap["quiz"]["correct"].is_null(), "key hidden while open");
        assert!(snap["quiz"]["deadline_ms"].as_i64().unwrap() > now_ms());

        // Everyone online answered: the window closes and the key appears.
        let qid: i64 = sqlx::query_scalar("SELECT id FROM quiz_question WHERE room_code = 'QZ11' AND \"order\" = 0")
            .fetch_one(&pool)
            .await
            .unwrap();
        crate::db::answers::insert_quiz_attempt(&pool, ana.id, qid, 0, true).await.unwrap();
        maybe_close_quiz_window(&pool, "QZ11", &online).await;
        let snap = room_snapshot(&pool, "QZ11", Some(&online)).await.unwrap();
        assert_eq!(snap["quiz"]["revealed"], true);
        assert!(snap["quiz"]["correct"].is_i64());
        assert_eq!(snap["quiz"]["done"], 1);

        advance(&pool, "QZ11").await.unwrap();
        let snap = room_snapshot(&pool, "QZ11", Some(&online)).await.unwrap();
        assert_eq!(snap["quiz"]["index"], 1);
        assert_eq!(snap["quiz"]["revealed"], false);
        assert!(snap["_viewer"].is_object());
        assert!(personalize(&snap, Some(ana.id)).get("_viewer").is_none());
    }

    #[tokio::test]
    async fn sweep_hides_idle_rooms_and_deletes_unstarted_ones() {
        let pool = crate::db::test_pool().await;
        let state = AppState::new(pool.clone());
        for code in ["SW01", "SW02", "SW03"] {
            crate::db::rooms::create_room(&pool, code).await.unwrap();
        }
        // SW02 started and active recently; SW03 started long ago.
        sqlx::query("UPDATE room SET started_at = datetime('now'), last_activity_at = datetime('now') WHERE code = 'SW02'")
            .execute(&pool).await.unwrap();
        sqlx::query("UPDATE room SET started_at = datetime('now', '-2 hours') WHERE code = 'SW03'")
            .execute(&pool).await.unwrap();
        // Fresh process: nothing is idle yet.
        let plan = sweep_plan(&state, std::time::Instant::now()).await;
        assert!(plan.hide.is_empty() && plan.dead.is_empty());

        let long_ago = std::time::Instant::now() - ROOM_IDLE_TTL - Duration::from_secs(1);
        let plan = sweep_plan(&state, long_ago).await;
        assert_eq!(plan.hide.len(), 3, "no hub, idle since boot");
        let mut dead = plan.dead.clone();
        dead.sort();
        assert_eq!(dead, ["SW01", "SW03"], "SW02 is kept: started and recently active");
        delete_rooms(&state, &plan.dead).await;
        let left: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM room").fetch_one(&pool).await.unwrap();
        assert_eq!(left, 1);
    }

    #[tokio::test]
    async fn concurrent_completion_checks_advance_the_reveal_once() {
        let pool = crate::db::test_pool().await;
        crate::db::rooms::create_room(&pool, "CC44").await.unwrap();
        sqlx::query("UPDATE room SET tutorial = 0 WHERE code = 'CC44'").execute(&pool).await.unwrap();
        let mut ids = Vec::new();
        for name in ["a", "b", "c", "d"] {
            ids.push(crate::db::rooms::add_player(&pool, "CC44", name).await.unwrap().id);
        }
        let online: HashSet<i64> = ids.iter().copied().collect();
        start_game(&pool, "CC44").await.unwrap();
        let round: i64 = sqlx::query_scalar("SELECT id FROM round WHERE room_code = 'CC44' AND \"order\" = 0")
            .fetch_one(&pool).await.unwrap();
        let mut answers = Vec::new();
        for &id in &ids {
            answers.push(crate::db::answers::insert_answer(&pool, round, id, "p", "o", 5).await.unwrap().id);
        }
        maybe_advance_phase(&pool, "CC44", &online).await;
        for &rater in &ids[1..] {
            crate::db::answers::insert_rating(&pool, answers[0], rater, 6).await.unwrap();
        }
        // Every rater's request runs the completion check at the same time.
        let checks = (0..20).map(|_| maybe_advance_phase(&pool, "CC44", &online));
        futures_util::future::join_all(checks).await;
        let revealed: i64 = sqlx::query_scalar("SELECT revealed FROM round WHERE id = ?")
            .bind(round).fetch_one(&pool).await.unwrap();
        assert_eq!(revealed, 1, "prompt 2 must not be skipped");
        // A caller that checked cursor 0 and arrives after the move is stale.
        let room = load(&pool, "CC44").await.unwrap();
        advance_reveal_and_settle(&pool, &room, 0).await.unwrap();
        let revealed: i64 = sqlx::query_scalar("SELECT revealed FROM round WHERE id = ?")
            .bind(round).fetch_one(&pool).await.unwrap();
        assert_eq!(revealed, 1, "a stale claim on cursor 0 must not move cursor 1");
        // Same for the admin button pressed twice on the last summary step.
        let room = load(&pool, "CC44").await.unwrap();
        futures_util::future::join_all((0..5).map(|_| go(&pool, &room, "summary"))).await;
        assert_eq!(load(&pool, "CC44").await.unwrap().phase, "summary");
        futures_util::future::join_all((0..5).map(|_| advance(&pool, "CC44"))).await;
        assert_eq!(load(&pool, "CC44").await.unwrap().current, 1, "one round forward, not five");
    }

    #[tokio::test]
    async fn lobby_starts_on_a_majority_that_counts_late_joiners() {
        let pool = crate::db::test_pool().await;
        crate::db::rooms::create_room(&pool, "LB55").await.unwrap();
        let mut online = HashSet::new();
        let mut ids = Vec::new();
        for name in ["a", "b", "c"] {
            let id = crate::db::rooms::add_player(&pool, "LB55", name).await.unwrap().id;
            online.insert(id);
            ids.push(id);
        }
        let vote = |id: i64| crate::db::answers::add_ready(&pool, "LB55", id, LOBBY_STEP);
        vote(ids[0]).await.unwrap();
        maybe_advance_by_votes(&pool, "LB55", &online).await;
        assert_eq!(load(&pool, "LB55").await.unwrap().phase, "waiting", "1 of 3 is not a majority");

        // Two more arrive before the second vote: 2 of 5 is still short.
        for name in ["d", "e"] {
            online.insert(crate::db::rooms::add_player(&pool, "LB55", name).await.unwrap().id);
        }
        vote(ids[1]).await.unwrap();
        maybe_advance_by_votes(&pool, "LB55", &online).await;
        let snap = room_snapshot(&pool, "LB55", Some(&online)).await.unwrap();
        assert_eq!((snap["ready"]["count"].as_i64(), snap["ready"]["needed"].as_i64()), (Some(2), Some(3)));
        assert_eq!(snap["phase"], "waiting");

        vote(ids[2]).await.unwrap();
        maybe_advance_by_votes(&pool, "LB55", &online).await;
        assert_eq!(load(&pool, "LB55").await.unwrap().phase, "quiz", "3 of 5 starts the game");
    }

    #[tokio::test]
    async fn expired_clock_moves_the_room_once() {
        std::env::remove_var("OPENAI_API_KEY");
        let pool = crate::db::test_pool().await;
        let state = AppState::new(pool.clone());
        crate::db::rooms::create_room(&pool, "EX33").await.unwrap();
        sqlx::query("UPDATE room SET tutorial = 0 WHERE code = 'EX33'")
            .execute(&pool)
            .await
            .unwrap();
        assert!(start_game(&pool, "EX33").await.unwrap());
        assert!(!expire_phase(&state, "EX33").await, "clock still running");

        // Nobody wrote before the clock ran out: straight to the summary.
        let sql = format!("UPDATE room SET phase_deadline = {TS_FROM_MS} WHERE code = 'EX33'");
        sqlx::query(&sql).bind(now_ms() - 1000).execute(&pool).await.unwrap();
        assert!(expire_phase(&state, "EX33").await);
        assert!(!expire_phase(&state, "EX33").await, "claimed once");
        let room = load(&pool, "EX33").await.unwrap();
        assert_eq!(room.phase, "summary");
        assert!(room.phase_deadline_ms.is_none());
    }

    #[tokio::test]
    async fn round_rates_settles_and_reaches_done() {
        let pool = crate::db::test_pool().await;
        crate::db::rooms::create_room(&pool, "RD22").await.unwrap();
        sqlx::query("UPDATE room SET tutorial = 0, anonymous = 1 WHERE code = 'RD22'")
            .execute(&pool)
            .await
            .unwrap();
        let ana = crate::db::rooms::add_player(&pool, "RD22", "ana").await.unwrap();
        let bob = crate::db::rooms::add_player(&pool, "RD22", "bob").await.unwrap();
        let online: HashSet<i64> = [ana.id, bob.id].into();

        // Lobby: everybody ready starts the game, straight to playing.
        crate::db::answers::add_ready(&pool, "RD22", ana.id, LOBBY_STEP).await.unwrap();
        crate::db::answers::add_ready(&pool, "RD22", bob.id, LOBBY_STEP).await.unwrap();
        maybe_advance_by_votes(&pool, "RD22", &online).await;
        let snap = room_snapshot(&pool, "RD22", Some(&online)).await.unwrap();
        assert_eq!(snap["phase"], "playing");
        assert!(snap["target"].is_string());
        assert!(snap["timer"]["deadline_ms"].is_i64());

        let round: i64 = sqlx::query_scalar("SELECT id FROM round WHERE room_code = 'RD22' AND \"order\" = 0")
            .fetch_one(&pool)
            .await
            .unwrap();
        let a = crate::db::answers::insert_answer(&pool, round, ana.id, "p1", "o1", 6).await.unwrap();
        maybe_advance_phase(&pool, "RD22", &online).await;
        assert_eq!(load(&pool, "RD22").await.unwrap().phase, "playing", "bob has not written");
        let b = crate::db::answers::insert_answer(&pool, round, bob.id, "p2", "o2", 9).await.unwrap();
        maybe_advance_phase(&pool, "RD22", &online).await;

        let snap = room_snapshot(&pool, "RD22", Some(&online)).await.unwrap();
        assert_eq!(snap["phase"], "rating");
        assert!(snap["players"][0]["score"].is_null(), "scores hidden while rating");
        assert_eq!(snap["reveal"]["author_label"], "Joueur 1");
        assert!(snap["reveal"]["author"].is_null());
        assert_eq!(personalize(&snap, Some(ana.id))["reveal"]["mine"], true);
        assert_eq!(personalize(&snap, Some(bob.id))["reveal"]["mine"], false);
        assert_eq!(snap["ready"]["needed"], 1, "author excluded from the bar");

        crate::db::answers::insert_rating(&pool, a.id, bob.id, 8).await.unwrap();
        maybe_advance_phase(&pool, "RD22", &online).await;
        crate::db::answers::insert_rating(&pool, b.id, ana.id, 4).await.unwrap();
        maybe_advance_phase(&pool, "RD22", &online).await;

        let snap = room_snapshot(&pool, "RD22", Some(&online)).await.unwrap();
        assert_eq!(snap["phase"], "summary");
        let rows = snap["summary"]["rows"].as_array().unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["quality_peers"], 8);
        assert!(rows[0]["round_score"].is_f64());

        for _ in 0..ROUNDS_PER_GAME {
            let phase = load(&pool, "RD22").await.unwrap().phase;
            if phase == "done" {
                break;
            }
            if phase == "playing" {
                // Nobody wrote: the clock path goes straight to summary.
                go(&pool, &load(&pool, "RD22").await.unwrap(), "summary").await.unwrap();
            }
            advance(&pool, "RD22").await.unwrap();
        }
        assert_eq!(load(&pool, "RD22").await.unwrap().phase, "done");
    }
}
