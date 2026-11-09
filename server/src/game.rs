// The game engine: what used to live in Django's views.py/models.py
// (start, the quizz window, the reveal walk, settling, the votes and
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

pub const QUIZ_SECONDS: i64 = 60;
pub const QUIZ_SECONDS_DEFAULT: i64 = 60;
pub const QUIZ_SECONDS_RANGE: (i64, i64) = (5, 300);
pub const QUIZ_REVEAL_SECONDS: i64 = 8;
pub const PROMPT_GRACE_MS: i64 = 2000;
// How long a game is, fixed by the data files and not by the host: every
// quizz question, then every core exercise. The optional exercises are never
// drawn: the host adds them one by one after the last core round
// (`bonus_round`). The host shortens a game by skipping, not by a setting.
pub fn quiz_count() -> i64 {
    quiz_bank().len() as i64
}
pub fn round_count() -> i64 {
    core_targets().count() as i64
}
// Prompts a player may send for one round; the latest replaces earlier ones
// (conversation rounds chain instead — see below) and is graded by the peers.
pub const MAX_ATTEMPTS: i64 = 3;
// Image rounds allow fewer: each attempt is a billed picture.
pub const MAX_IMAGE_ATTEMPTS: i64 = 2;
// Conversation rounds chain their prompts instead of replacing them: the
// three steps the exercise names (context, synthesis, test) plus one follow-up.
pub const MAX_CONVERSATION_PROMPTS: i64 = 4;
pub const ROUND_SECONDS: i64 = 300;
pub const RATE_SECONDS: i64 = 60;
// En mode automatique, le résumé reste affiché 30 s pour relire les réponses
// avant d'enchaîner seul ; en manuel, pas de délai (l'animateur avance).
pub const SUMMARY_AUTO_SECONDS: i64 = 30;
pub const ROUND_SECONDS_RANGE: (i64, i64) = (10, 600);
pub const RATE_SECONDS_RANGE: (i64, i64) = (5, 300);
pub const LOBBY_STEP: &str = "lobby";
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
    // A hint the player can open from an icon while writing.
    #[serde(default)]
    pub tip: Option<String>,
    // What the critic grades the prompt on, shown to the player beside the
    // note: the note explains itself through these, not through prose.
    #[serde(default)]
    pub criteria: Vec<String>,
    // Each send continues the same conversation with the model instead of
    // replacing the previous prompt; the whole chain is graded and rated.
    #[serde(default)]
    pub conversation: bool,
    // The round is still answered by the image model, but its picture is not
    // shown: the player builds the visual from the data alone.
    #[serde(default)]
    pub hide_image: bool,
    // Not part of the drawn game: offered to the host after the last core
    // round, in file order, as a bonus round (`bonus_round`).
    #[serde(default)]
    pub optional: bool,
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

fn core_targets() -> impl Iterator<Item = &'static Target> {
    targets().iter().filter(|t| !t.optional)
}

fn optional_targets() -> impl Iterator<Item = &'static Target> {
    targets().iter().filter(|t| t.optional)
}

// Looked up by text, as Django did: a round stores only its target.
pub fn image_for(target: &str) -> Option<String> {
    targets()
        .iter()
        .find(|t| t.text == target)
        .and_then(|t| t.image.clone())
}

#[cfg(test)]
pub fn targets_with_image() -> impl Iterator<Item = &'static str> {
    targets().iter().filter(|t| t.image.is_some()).map(|t| t.text.as_str())
}

// The picture shown to the players, unless the exercise hides it.
pub fn shown_image_for(target: &str) -> Option<String> {
    targets()
        .iter()
        .find(|t| t.text == target && !t.hide_image)
        .and_then(|t| t.image.clone())
}

pub fn is_conversation(target: &str) -> bool {
    targets().iter().any(|t| t.text == target && t.conversation)
}

pub fn max_attempts_for(target: &str) -> i64 {
    if image_for(target).is_some() {
        MAX_IMAGE_ATTEMPTS
    } else if is_conversation(target) {
        MAX_CONVERSATION_PROMPTS
    } else {
        MAX_ATTEMPTS
    }
}

pub fn tip_for(target: &str) -> Option<String> {
    targets()
        .iter()
        .find(|t| t.text == target)
        .and_then(|t| t.tip.clone())
}

pub fn criteria_for(target: &str) -> Vec<String> {
    targets()
        .iter()
        .find(|t| t.text == target)
        .map(|t| t.criteria.clone())
        .unwrap_or_default()
}

// --- scoring rules (prompts.py) -------------------------------------------

pub fn half_up(x: f64) -> i64 {
    (x + 0.5).floor() as i64
}

// The round score is the class's average grade, rounded half up. No grade,
// no score: the prompt stays unrated instead of counting as a zero.
pub fn quality(peers: &[i64]) -> Option<i64> {
    (!peers.is_empty()).then(|| half_up(peers.iter().sum::<i64>() as f64 / peers.len() as f64))
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
    #[sqlx(default)]
    pub quiz_seconds: i64,
    #[sqlx(default)]
    pub auto_mode: i64,
    #[sqlx(default)]
    pub quiz_count: i64,
    #[sqlx(default)]
    pub round_count: i64,
    // Time left on the clock the host paused, or None while it runs.
    #[sqlx(default)]
    pub paused_ms: Option<i64>,
}

pub async fn load(pool: &SqlitePool, code: &str) -> Result<GameRoom, sqlx::Error> {
    let sql = format!(
        "SELECT code, phase, current, tutorial, anonymous, quiz_index,
                {} AS quiz_deadline_ms, {} AS phase_deadline_ms, round_seconds, rate_seconds,
                COALESCE(quiz_seconds, 60) AS quiz_seconds, COALESCE(auto_mode, 0) AS auto_mode,
                quiz_count, round_count, paused_ms
         FROM room WHERE code = ?",
        ms_of("quiz_deadline"),
        ms_of("phase_deadline"),
    );
    sqlx::query_as::<_, GameRoom>(&sql).bind(code).fetch_one(pool).await
}

impl GameRoom {
    // A paused question stays open: the class may still answer.
    pub fn quiz_open(&self) -> bool {
        (self.phase == "quiz" && self.paused_ms.is_some()) || matches!(self.quiz_deadline_ms, Some(d) if now_ms() < d)
    }
    pub fn phase_expired(&self) -> bool {
        matches!(self.phase_deadline_ms, Some(d) if now_ms() >= d)
    }
    // Writing closes PROMPT_GRACE_MS after the clock: the draft the client
    // auto-sends in the last second must land, be generated and be rated.
    pub fn prompt_window_closed(&self) -> bool {
        matches!(self.phase_deadline_ms, Some(d) if now_ms() >= d + PROMPT_GRACE_MS)
    }
    pub fn is_auto(&self) -> bool {
        self.auto_mode != 0
    }
}

#[derive(Debug, Clone, sqlx::FromRow)]
struct AnswerRow {
    id: i64,
    player_id: i64,
    prompt: String,
    output: String,
    round_score: Option<f64>,
    attempts: i64,
    turns: Option<String>,
}

async fn round_answers(pool: &SqlitePool, round_id: i64) -> Vec<AnswerRow> {
    sqlx::query_as::<_, AnswerRow>(
        "SELECT id, player_id, prompt, output, round_score, attempts, turns
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

// A random permutation of 0..n (Fisher-Yates).
fn shuffled(n: usize) -> Vec<usize> {
    let mut order: Vec<usize> = (0..n).collect();
    for i in (1..order.len()).rev() {
        order.swap(i, (random_u64() % (i as u64 + 1)) as usize);
    }
    order
}

// A permutation of the whole bank, so the room has questions before its start.
// `start_game` draws again, so every run of the room gets its own selection.
pub async fn create_bank(pool: &SqlitePool, code: &str) -> Result<(), sqlx::Error> {
    let bank = quiz_bank();
    for (pos, &i) in shuffled(bank.len()).iter().enumerate() {
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

// Sets the phase and arms its clock in one write: playing is timed, every
// other phase clears the deadline. Rating has no clock: the class discusses
// each prompt aloud, and the host moves on (in automatic mode, once every
// online rater has rated, or on the votes). Every write clears a pause: a new
// step starts on a running clock.
// Applies only if the room is still in the phase and round `room` was read
// in; true when it moved.
// En mode automatique, le résumé est aussi minuté (30 s) pour s'enchaîner seul ;
// en manuel, aucun délai : l'animateur avance quand la relecture est finie.
async fn go(pool: &SqlitePool, room: &GameRoom, phase: &str) -> Result<bool, sqlx::Error> {
    let seconds = match phase {
        "playing" => Some(room.round_seconds),
        "summary" if room.is_auto() => Some(SUMMARY_AUTO_SECONDS),
        _ => None,
    };
    let sql = format!(
        "UPDATE room SET phase = ?, phase_deadline = {TS_FROM_MS}, paused_ms = NULL WHERE code = ? AND phase = ? AND current = ?"
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
    // A same-phase write is a clock re-arm (next prompt to rate), not a transition.
    if moved == 1 && phase != room.phase {
        tracing::info!(room = %room.code, from = %room.phase, to = phase, round = room.current + 1, "phase change");
    }
    Ok(moved == 1)
}

// Re-arms the clock of the phase the room is already in.
async fn arm(pool: &SqlitePool, room: &GameRoom, phase: &str) -> Result<bool, sqlx::Error> {
    go(pool, room, phase).await
}

// Opens question `index`, if the room is still on question `from`. The window
// length is the room's own `quiz_seconds` (configurable at creation).
async fn open_quiz(pool: &SqlitePool, code: &str, from: i64, index: i64) -> Result<bool, sqlx::Error> {
    let secs: i64 = sqlx::query_scalar("SELECT COALESCE(quiz_seconds, 60) FROM room WHERE code = ?")
        .bind(code)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten()
        .unwrap_or(QUIZ_SECONDS_DEFAULT);
    let sql = format!(
        "UPDATE room SET quiz_index = ?, quiz_deadline = {TS_FROM_MS}, paused_ms = NULL WHERE code = ? AND quiz_index = ?"
    );
    let moved = sqlx::query(&sql)
        .bind(index)
        .bind(now_ms() + secs * 1000)
        .bind(code)
        .bind(from)
        .execute(pool)
        .await?
        .rows_affected();
    if moved == 1 {
        tracing::info!(room = %code, question = index + 1, seconds = secs, "quizz question opened");
    }
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
    let sql = format!("UPDATE room SET quiz_deadline = {TS_FROM_MS}, paused_ms = NULL WHERE code = ?");
    sqlx::query(&sql).bind(now_ms()).bind(&room.code).execute(pool).await?;
    Ok(())
}

// Appends `count` random questions from the bank, numbered from `from`,
// skipping the prompts in `used`.
async fn draw_questions(
    conn: &mut sqlx::SqliteConnection,
    code: &str,
    from: i64,
    count: i64,
    used: &[String],
) -> Result<(), sqlx::Error> {
    let bank = quiz_bank();
    let fresh = shuffled(bank.len()).into_iter().map(|i| &bank[i]).filter(|q| !used.contains(&q.prompt));
    for (pos, q) in (from..).zip(fresh.take(count.max(0) as usize)) {
        sqlx::query(
            "INSERT INTO quiz_question (room_code, \"order\", prompt, choices, correct, explanation)
             VALUES (?, ?, ?, ?, ?, ?)",
        )
        .bind(code)
        .bind(pos)
        .bind(&q.prompt)
        .bind(serde_json::to_string(&q.choices).unwrap_or_else(|_| "[]".into()))
        .bind(q.correct)
        .bind(&q.explanation)
        .execute(&mut *conn)
        .await?;
    }
    Ok(())
}

// Appends `count` random core rounds, numbered from `from`, skipping the
// targets in `used`. Optional targets are never drawn (see `bonus_round`).
async fn draw_rounds(
    conn: &mut sqlx::SqliteConnection,
    code: &str,
    from: i64,
    count: i64,
    used: &[String],
) -> Result<(), sqlx::Error> {
    let core: Vec<&Target> = core_targets().collect();
    let fresh = shuffled(core.len()).into_iter().map(|i| core[i]).filter(|t| !used.contains(&t.text));
    for (pos, t) in (from..).zip(fresh.take(count.max(0) as usize)) {
        sqlx::query("INSERT INTO round (room_code, \"order\", target) VALUES (?, ?, ?)")
            .bind(code)
            .bind(pos)
            .bind(&t.text)
            .execute(&mut *conn)
            .await?;
    }
    Ok(())
}

// Removes a player at any point of the game, with everything they wrote:
// answers, ratings, quizz attempts and votes go with the row (ON DELETE
// CASCADE). During rating, the cursor on the prompt being rated is kept on
// the same prompt when an earlier one disappears; when the removed prompt
// was the last one left to rate, the round moves to its summary. False when
// the player is not in this room.
pub async fn remove_player(pool: &SqlitePool, code: &str, player_id: i64) -> Result<bool, sqlx::Error> {
    let room = load(pool, code).await?;
    let round = current_round(pool, &room).await;
    let mut tx = pool.begin().await?;
    if let (Some((round_id, revealed)), "rating") = (round, room.phase.as_str()) {
        let ids: Vec<i64> = sqlx::query_scalar("SELECT player_id FROM answer WHERE round_id = ? ORDER BY id")
            .bind(round_id)
            .fetch_all(&mut *tx)
            .await?;
        if ids.iter().position(|&p| p == player_id).is_some_and(|i| (i as i64) < revealed) {
            sqlx::query("UPDATE round SET revealed = revealed - 1 WHERE id = ?")
                .bind(round_id)
                .execute(&mut *tx)
                .await?;
        }
    }
    let gone = sqlx::query("DELETE FROM player WHERE id = ? AND room_code = ?")
        .bind(player_id)
        .bind(code)
        .execute(&mut *tx)
        .await?
        .rows_affected();
    tx.commit().await?;
    if gone == 0 {
        return Ok(false);
    }
    tracing::info!(room = %code, player_id, "player removed");
    if let Some((round_id, revealed)) = current_round(pool, &room).await.filter(|_| room.phase == "rating") {
        let count = round_answers(pool, round_id).await.len() as i64;
        if revealed >= count {
            // The removed prompt was the one on screen and the last: nothing is left to rate.
            advance_reveal_and_settle(pool, &room, revealed).await?;
        }
    }
    Ok(true)
}

// Draws the room's rounds, trims the quizz to its length, and leaves the lobby. False when the room had already
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
    // A fresh draw on every start, restarts included: the whole quizz bank and
    // every core target, each in random order. The counts are written for the
    // snapshot, which reports the game's length.
    // Attempts cannot exist yet: nobody can answer before the start, and a
    // restart deletes them.
    sqlx::query("DELETE FROM quiz_question WHERE room_code = ?")
        .bind(code)
        .execute(&mut *tx)
        .await?;
    sqlx::query("UPDATE room SET quiz_count = ?, round_count = ? WHERE code = ?")
        .bind(quiz_count())
        .bind(round_count())
        .bind(code)
        .execute(&mut *tx)
        .await?;
    draw_questions(&mut tx, code, 0, quiz_count(), &[]).await?;
    // The placeholder order-0 round from creation/restart is replaced by the
    // real draw.
    sqlx::query("DELETE FROM round WHERE room_code = ?")
        .bind(code)
        .execute(&mut *tx)
        .await?;
    draw_rounds(&mut tx, code, 0, round_count(), &[]).await?;
    tx.commit().await?;
    tracing::info!(room = %code, first_phase = phase, rounds = round_count(), questions = quiz_count(), "game started");
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
            tracing::info!(room = %room.code, round = room.current + 1, prompt = revealed + 2, of = count, "next prompt to rate");
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
        // Nothing to settle and nothing is invented: an empty round still
        // reaches `summary` (the expiry path moves it there), where the
        // snapshot carries `rows: []` and the screen says so in words.
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
        sqlx::query("UPDATE answer SET round_score = ? WHERE id = ?")
            .bind(quality(scores))
            .bind(a.id)
            .execute(&mut *tx)
            .await?;
    }
    tx.commit().await?;
    tracing::info!(room = %room.code, round = room.current + 1, answers = answers.len(), "round settled");
    Ok(())
}

async fn next_round(pool: &SqlitePool, room: &GameRoom) -> Result<(), sqlx::Error> {
    let rounds: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM round WHERE room_code = ?")
        .bind(&room.code)
        .fetch_one(pool)
        .await?;
    if room.current + 1 < rounds {
        let sql = format!(
            "UPDATE room SET current = current + 1, phase = 'playing', phase_deadline = {TS_FROM_MS}, paused_ms = NULL
             WHERE code = ? AND phase = 'summary' AND current = ?"
        );
        let moved = sqlx::query(&sql)
            .bind(now_ms() + room.round_seconds * 1000)
            .bind(&room.code)
            .bind(room.current)
            .execute(pool)
            .await?
            .rows_affected();
        if moved == 1 {
            tracing::info!(room = %room.code, round = room.current + 2, of = rounds, "round opened");
        }
        Ok(())
    } else {
        go(pool, room, "done").await.map(|_| ())
    }
}

// The optional targets this room has not played yet, in file order.
async fn remaining_bonus(
    conn: &mut sqlx::SqliteConnection,
    code: &str,
) -> Result<Vec<&'static Target>, sqlx::Error> {
    let used: Vec<String> = sqlx::query_scalar("SELECT target FROM round WHERE room_code = ?")
        .bind(code)
        .fetch_all(&mut *conn)
        .await?;
    Ok(optional_targets().filter(|t| !used.contains(&t.text)).collect())
}

// How many bonus rounds the host can still add: only on the summary of the
// last round, since that is the one moment the choice is offered.
async fn bonus_left(pool: &SqlitePool, room: &GameRoom, total_rounds: i64) -> i64 {
    if room.phase != "summary" || room.current + 1 < total_rounds {
        return 0;
    }
    let Ok(mut conn) = pool.acquire().await else { return 0 };
    remaining_bonus(&mut conn, &room.code).await.map(|v| v.len() as i64).unwrap_or(0)
}

// The host's alternative to ending the game on the last summary: append the
// next optional target as one more round and open it. The insert and the
// move are one transaction, claimed on (summary, current), so a double click
// adds one round.
pub async fn bonus_round(pool: &SqlitePool, code: &str) -> Result<(), String> {
    let room = load(pool, code).await.map_err(|e| e.to_string())?;
    if room.phase != "summary" {
        return Err(format!("pas de manche bonus en phase {}", room.phase));
    }
    let mut tx = pool.begin().await.map_err(|e| e.to_string())?;
    let rounds: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM round WHERE room_code = ?")
        .bind(code)
        .fetch_one(&mut *tx)
        .await
        .map_err(|e| e.to_string())?;
    if room.current + 1 < rounds {
        return Err("la manche bonus vient après la dernière manche".into());
    }
    let Some(target) = remaining_bonus(&mut tx, code).await.map_err(|e| e.to_string())?.first().copied() else {
        return Err("plus de manche bonus".into());
    };
    let sql = format!(
        "UPDATE room SET current = current + 1, phase = 'playing', phase_deadline = {TS_FROM_MS}, paused_ms = NULL
         WHERE code = ? AND phase = 'summary' AND current = ?"
    );
    let moved = sqlx::query(&sql)
        .bind(now_ms() + room.round_seconds * 1000)
        .bind(code)
        .bind(room.current)
        .execute(&mut *tx)
        .await
        .map_err(|e| e.to_string())?
        .rows_affected();
    if moved != 1 {
        return Ok(());
    }
    sqlx::query("INSERT INTO round (room_code, \"order\", target) VALUES (?, ?, ?)")
        .bind(code)
        .bind(rounds)
        .bind(&target.text)
        .execute(&mut *tx)
        .await
        .map_err(|e| e.to_string())?;
    tx.commit().await.map_err(|e| e.to_string())?;
    tracing::info!(room = %code, round = rounds + 1, "bonus round opened");
    Ok(())
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
        // Nothing to rate: the round closes into its summary, as the clock does.
        "playing" if !has_answers(pool, &room).await => close_round(pool, &room).await,
        "playing" => open_rating(pool, &room).await,
        "rating" => match current_round(pool, &room).await {
            Some((_, revealed)) => advance_reveal_and_settle(pool, &room, revealed).await,
            None => Ok(()),
        },
        "summary" => next_round(pool, &room).await,
        other => return Err(format!("no advance from {other}")),
    };
    r.map_err(|e| {
        tracing::error!(room = %code, phase = %room.phase, error = %e, "advance failed");
        e.to_string()
    })
}

// Longest an ending round waits for answers still being generated: the play
// stream and both verdicts, with margin. Past it the round closes anyway.
pub const DRAIN_TIMEOUT: Duration = Duration::from_secs(240);

// The admin's next/skip while answers are still being generated: they land
// first, so every player sees the answer they paid a prompt for. Returns true
// when the action was deferred (the caller must not run it): the round takes
// no new prompt, its clock stops so the ticker cannot close it under the
// admin, and the action runs once the last answer is stored.
pub async fn defer_while_answering(state: &AppState, code: &str, skip_round: bool) -> bool {
    let pool = &state.pool;
    let Ok(room) = load(pool, code).await else { return false };
    if room.phase != "playing" {
        return false;
    }
    let Some((round_id, _)) = current_round(pool, &room).await else { return false };
    let hub = crate::hub::get_hub(&state.hubs, code).await;
    if !hub.round_inflight(round_id).await {
        return false;
    }
    // Already ending (a second click, or the clock got there first): one close.
    if !hub.set_closing(room.current) {
        return true;
    }
    let _ = sqlx::query("UPDATE room SET phase_deadline = NULL WHERE code = ? AND phase = 'playing'")
        .bind(code)
        .execute(pool)
        .await;
    tracing::info!(room = %code, round = room.current + 1, skip = skip_round, "round ending: waiting for answers in flight");
    let _ = hub.republish(pool, code).await;
    let state = state.clone();
    let code = code.to_string();
    let current = room.current;
    tokio::spawn(async move {
        hub.wait_round_idle(round_id, DRAIN_TIMEOUT).await;
        // A restart while waiting starts another game: leave it alone.
        match load(&state.pool, &code).await {
            Ok(r) if r.phase == "playing" && r.current == current => {}
            _ => return,
        }
        let r = if skip_round { skip(&state.pool, &code).await } else { advance(&state.pool, &code).await };
        if let Err(e) = r {
            tracing::warn!(room = %code, error = %e, "deferred round close failed");
        }
        let _ = hub.republish(&state.pool, &code).await;
    });
    true
}

async fn has_answers(pool: &SqlitePool, room: &GameRoom) -> bool {
    match current_round(pool, room).await {
        Some((id, _)) => !round_answers(pool, id).await.is_empty(),
        None => false,
    }
}

// Straight to the summary, settled with whatever ratings exist (none: the
// final note is the AI's).
async fn close_round(pool: &SqlitePool, room: &GameRoom) -> Result<(), sqlx::Error> {
    if go(pool, room, "summary").await? {
        settle_round(pool, room).await?;
    }
    Ok(())
}

// The admin's skip: the quizz jumps to the first round, a round being
// written or rated jumps to its summary.
pub async fn skip(pool: &SqlitePool, code: &str) -> Result<(), String> {
    let room = load(pool, code).await.map_err(|e| e.to_string())?;
    let r = match room.phase.as_str() {
        "quiz" => go(pool, &room, "playing").await.map(|_| ()),
        "playing" | "rating" => close_round(pool, &room).await,
        other => return Err(format!("rien à passer en phase {other}")),
    };
    r.map_err(|e| e.to_string())
}

// The admin's one-click pass: the question or round on screen is dropped
// and the next one opens, with no reveal, rating or summary in between. The
// last question opens the first round. The last round stops on its summary,
// where the host ends the game or adds a bonus round. A passed round keeps
// the ratings it already has (settled as a skip). It does not wait for
// answers still being generated: they land on the passed round, unseen.
pub async fn pass(pool: &SqlitePool, code: &str) -> Result<(), String> {
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
        "playing" | "rating" => pass_round(pool, &room).await,
        other => return Err(format!("rien à passer en phase {other}")),
    };
    r.map_err(|e| e.to_string())
}

async fn pass_round(pool: &SqlitePool, room: &GameRoom) -> Result<(), sqlx::Error> {
    close_round(pool, room).await?;
    let rounds: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM round WHERE room_code = ?")
        .bind(&room.code)
        .fetch_one(pool)
        .await?;
    let now = load(pool, &room.code).await?;
    // Claimed on the round passed: a concurrent click finds it moved on.
    if now.phase == "summary" && now.current == room.current && room.current + 1 < rounds {
        next_round(pool, &now).await?;
    }
    Ok(())
}

// --- the host's clock and early end --------------------------------------

// The clock on screen: the quizz question's window in the quizz, the phase
// deadline elsewhere (writing, rating, a timed summary).
fn clock_column(room: &GameRoom) -> &'static str {
    if room.phase == "quiz" { "quiz_deadline" } else { "phase_deadline" }
}

// Each write below is claimed on the step `room` was read in (phase and
// question or round), so a click that lands as the step changes is a no-op.
fn step_claim(room: &GameRoom) -> &'static str {
    if room.phase == "quiz" { "phase = ? AND quiz_index = ?" } else { "phase = ? AND current = ?" }
}

fn step_index(room: &GameRoom) -> i64 {
    if room.phase == "quiz" { room.quiz_index } else { room.current }
}

// Freezes the running clock: the time left is kept and the deadline is
// cleared, so nothing expires until `resume_clock`. Answers and ratings are
// still taken meanwhile.
pub async fn pause_clock(pool: &SqlitePool, code: &str) -> Result<(), String> {
    let room = load(pool, code).await.map_err(|e| e.to_string())?;
    let col = clock_column(&room);
    let sql = format!(
        "UPDATE room SET paused_ms = {ms} - ?, {col} = NULL
         WHERE code = ? AND {claim} AND paused_ms IS NULL AND {ms} > ?",
        ms = ms_of(col),
        claim = step_claim(&room),
    );
    let now = now_ms();
    let moved = sqlx::query(&sql)
        .bind(now).bind(code).bind(&room.phase).bind(step_index(&room)).bind(now)
        .execute(pool).await.map_err(|e| e.to_string())?.rows_affected();
    if moved != 1 {
        return Err("aucun chrono en cours".into());
    }
    tracing::info!(room = %code, phase = %room.phase, "clock paused");
    Ok(())
}

pub async fn resume_clock(pool: &SqlitePool, code: &str) -> Result<(), String> {
    let room = load(pool, code).await.map_err(|e| e.to_string())?;
    let col = clock_column(&room);
    let sql = format!(
        "UPDATE room SET {col} = strftime('%Y-%m-%d %H:%M:%f', (? + paused_ms) / 1000.0, 'unixepoch'), paused_ms = NULL
         WHERE code = ? AND {claim} AND paused_ms IS NOT NULL",
        claim = step_claim(&room),
    );
    let moved = sqlx::query(&sql)
        .bind(now_ms()).bind(code).bind(&room.phase).bind(step_index(&room))
        .execute(pool).await.map_err(|e| e.to_string())?.rows_affected();
    if moved != 1 {
        return Err("le chrono n'est pas en pause".into());
    }
    tracing::info!(room = %code, phase = %room.phase, "clock resumed");
    Ok(())
}

// Adds `seconds` to the clock on screen, running or paused. Relative in SQL,
// so two quick clicks add twice instead of one overwriting the other.
pub async fn add_time(pool: &SqlitePool, code: &str, seconds: i64) -> Result<(), String> {
    let room = load(pool, code).await.map_err(|e| e.to_string())?;
    let col = clock_column(&room);
    let ms = seconds * 1000;
    let claim = step_claim(&room);
    let paused = format!("UPDATE room SET paused_ms = paused_ms + ? WHERE code = ? AND {claim} AND paused_ms IS NOT NULL");
    let running = format!(
        "UPDATE room SET {col} = strftime('%Y-%m-%d %H:%M:%f', ({m} + ?) / 1000.0, 'unixepoch')
         WHERE code = ? AND {claim} AND paused_ms IS NULL AND {m} > ?",
        m = ms_of(col),
    );
    let moved = if room.paused_ms.is_some() {
        sqlx::query(&paused).bind(ms).bind(code).bind(&room.phase).bind(step_index(&room)).execute(pool).await
    } else {
        sqlx::query(&running).bind(ms).bind(code).bind(&room.phase).bind(step_index(&room)).bind(now_ms()).execute(pool).await
    }
    .map_err(|e| e.to_string())?
    .rows_affected();
    if moved != 1 {
        return Err("aucun chrono en cours".into());
    }
    tracing::info!(room = %code, phase = %room.phase, seconds, "clock extended");
    Ok(())
}

// The host ends the game from any step, straight to the final ranking. A
// round being written or rated is settled with the ratings it already has;
// rounds not reached are simply not played.
pub async fn end_game(pool: &SqlitePool, code: &str) -> Result<(), String> {
    let room = load(pool, code).await.map_err(|e| e.to_string())?;
    if !matches!(room.phase.as_str(), "quiz" | "playing" | "rating" | "summary") {
        return Err(format!("rien à terminer en phase {}", room.phase));
    }
    let moved = go(pool, &room, "done").await.map_err(|e| e.to_string())?;
    if moved && matches!(room.phase.as_str(), "playing" | "rating") {
        settle_round(pool, &room).await.map_err(|e| e.to_string())?;
    }
    tracing::info!(room = %code, from = %room.phase, "game ended by the host");
    Ok(())
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

// Rating, automatic mode only: every online rater (author excluded) rated
// the prompt on screen → next prompt. In manual mode the host moves on: the
// class discusses each prompt aloud after rating it. Playing never closes early: a player may send new attempts
// until the clock (or the animateur) ends the round. An empty rater set
// never advances: a room must not run itself.
pub async fn maybe_advance_phase(pool: &SqlitePool, code: &str, online: &HashSet<i64>) {
    if online.is_empty() {
        return;
    }
    let Ok(room) = load(pool, code).await else { return };
    if !room.is_auto() {
        return;
    }
    let Some((round_id, revealed)) = current_round(pool, &room).await else {
        return;
    };
    let answers = round_answers(pool, round_id).await;
    match room.phase.as_str() {
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
    // Le lobby ne démarre jamais seul : seul l'animateur (admin_start) lance la
    // partie. Les votes restent comptés pour l'affichage du quorum.
    // Ni la notation en mode manuel : l'animateur passe au prompt suivant.
    if step == LOBBY_STEP || !room.is_auto() {
        return;
    } else if let Some(revealed) = step.rsplit(':').next().and_then(|r| r.parse().ok()) {
        // The vote was counted on `rating:<round>:<revealed>`: claim that cursor.
        let _ = advance_reveal_and_settle(pool, &room, revealed).await;
    }
}

// The phase clock ran out. Playing waits for streams still in flight so an
// answer that beat the clock is never lost; the deadline is claimed with a
// conditional UPDATE so concurrent callers run the transition once.
// En mode automatique, le résumé minuté et le quizz s'enchaînent seuls.
pub async fn expire_phase(state: &AppState, code: &str) -> bool {
    let pool = &state.pool;
    let Ok(room) = load(pool, code).await else { return false };
    // Auto : le quizz avance après la fenêtre de réponse PLUS le délai
    // de révélation, pour laisser le verdict visible avant d'enchaîner.
    if room.phase == "quiz" && room.is_auto() {
        if matches!(room.quiz_deadline_ms, Some(d) if now_ms() >= d + QUIZ_REVEAL_SECONDS * 1000) {
            let _ = advance(pool, code).await;
            return true;
        }
        return false;
    }
    if room.phase == "summary" && room.is_auto() {
        if !room.phase_expired() {
            return false;
        }
        let claimed = sqlx::query(
            "UPDATE room SET phase_deadline = NULL WHERE code = ? AND phase = 'summary' AND phase_deadline IS NOT NULL",
        )
        .bind(code)
        .execute(pool)
        .await
        .map(|r| r.rows_affected())
        .unwrap_or(0);
        if claimed != 1 {
            return false;
        }
        let _ = advance(pool, code).await;
        return true;
    }
    let expired = if room.phase == "playing" { room.prompt_window_closed() } else { room.phase_expired() };
    if !expired || !matches!(room.phase.as_str(), "playing" | "rating") {
        return false;
    }
    let Some((round_id, revealed)) = current_round(pool, &room).await else {
        return false;
    };
    if room.phase == "playing" {
        let hub = crate::hub::get_hub(&state.hubs, code).await;
        if hub.round_inflight(round_id).await {
            // Said once, so the screens show why the round has not moved.
            if hub.set_closing(room.current) {
                let _ = hub.republish(pool, code).await;
            }
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
    tracing::info!(room = %code, phase = %room.phase, round = room.current + 1, "phase clock expired");
    if room.phase == "rating" {
        let _ = advance_reveal_and_settle(pool, &room, revealed).await;
    } else if round_answers(pool, round_id).await.is_empty() {
        // An unanswered round is still a round: it gets its summary with
        // `rows: []` instead of staying silent on `playing`.
        let _ = close_round(pool, &room).await;
    } else {
        let _ = open_rating(pool, &room).await;
    }
    true
}

// Clocks move the game without any request: deadlines expire and quiz
// windows close on time. Django rebuilt the snapshot every 0.5 s per stream;
// here one task wakes every second and republishes only rooms whose clock
// actually crossed a deadline.
// One tick of the scheduler, exposed for tests: the playing timer is not
// armed separately — the sweep re-runs expire_phase every second while the
// clock is past its deadline, which is what carries a room through the
// prompt grace and past a round still generating.
pub async fn ticker_sweep(state: &AppState, announced: &mut HashMap<String, i64>) {
    let tick_start = std::time::Instant::now();
    let now = now_ms();
    // Bounded scan: without LIMIT one stuck room table makes every
    // tick linear in dead rooms the sweeper has not deleted yet.
    // Le résumé est inclus : en mode automatique il est minuté.
    let sql = format!(
        "SELECT code, phase, {} AS q, {} AS p, COALESCE(auto_mode, 0) AS a FROM room WHERE phase IN ('quiz', 'playing', 'rating', 'summary') LIMIT 1000",
        ms_of("quiz_deadline"),
        ms_of("phase_deadline"),
    );
    let rows: Vec<(String, String, Option<i64>, Option<i64>, i64)> =
        match sqlx::query_as(&sql).fetch_all(&state.pool).await {
            Ok(rows) => rows,
            Err(e) => {
                // Every timed room stalls while this fails: worth an ERROR each tick.
                tracing::error!(error = %e, "ticker: cannot read timed rooms");
                return;
            }
        };
    let mut seen: HashSet<String> = HashSet::new();
    for (code, phase, quiz_deadline, phase_deadline, auto_mode) in rows {
        let mut changed = false;
        if matches!(phase_deadline, Some(d) if now >= d) {
            changed |= expire_phase(&state, &code).await;
        }
        if phase == "quiz" {
            if let Some(d) = quiz_deadline {
                // Auto : la fin de la fenêtre révèle, l'avance n'a lieu qu'après
                // QUIZ_REVEAL_SECONDS (voir expire_phase). Pendant la fenêtre de
                // révélation on republie chaque tick pour diffuser le verdict.
                if auto_mode != 0 && now >= d {
                    if announced.get(&code) != Some(&d) {
                        announced.insert(code.clone(), d);
                    }
                    changed = true;
                    changed |= expire_phase(&state, &code).await;
                } else if now >= d && announced.get(&code) != Some(&d) {
                    announced.insert(code.clone(), d);
                    changed = true;
                }
            }
        }
        if changed {
            let hub = crate::hub::get_hub(&state.hubs, &code).await;
            let _ = hub.republish(&state.pool, &code).await;
        }
        if phase == "quiz" {
            seen.insert(code);
        }
    }
    // Purge les salles disparues ou sorties du quiz : `announced` ne
    // garde qu'un marqueur par salle suivie, sinon fuite lente.
    announced.retain(|code, _| seen.contains(code));
    let tick_ms = tick_start.elapsed().as_millis() as u64;
    if tick_ms > 500 {
        tracing::warn!(tick_ms, "ticker: slow tick, clocks may lag");
    }
}

pub fn spawn_ticker(state: AppState) {
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(Duration::from_secs(1));
        // Quiz deadline already announced as revealed, per room.
        let mut announced: HashMap<String, i64> = HashMap::new();
        loop {
            tick.tick().await;
            ticker_sweep(&state, &mut announced).await;
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
            "DELETE FROM generated_image WHERE round_id IN (SELECT id FROM round WHERE room_code = ?)",
            "DELETE FROM quiz_attempt WHERE question_id IN (SELECT id FROM quiz_question WHERE room_code = ?)",
            "DELETE FROM ready WHERE room_code = ?",
            "DELETE FROM round WHERE room_code = ?",
            "DELETE FROM quiz_question WHERE room_code = ?",
            "DELETE FROM player WHERE room_code = ?",
            "DELETE FROM room WHERE code = ?",
        ] {
            if let Err(e) = sqlx::query(sql).bind(code).execute(&state.pool).await {
                tracing::error!(room = %code, error = %e, "room delete step failed");
            }
        }
        state.hubs.write().await.remove(code);
        tracing::info!(room = %code, "idle room deleted");
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
                if let Err(e) = sqlx::query("UPDATE room SET last_activity_at = datetime('now') WHERE code = ?")
                    .bind(code)
                    .execute(&state.pool)
                    .await
                {
                    tracing::warn!(room = %code, error = %e, "sweeper: cannot stamp activity");
                }
            }
            tracing::debug!(active = plan.active.len(), hidden = plan.hide.len(), deleted = plan.dead.len(), "sweep");
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
    // Unrated answers have a NULL round_score: SUM skips them. Old rows may
    // hold a fractional score, so the total rounds half up instead of truncating.
    let totals: HashMap<i64, i64> = sqlx::query_as::<_, (i64, Option<i64>)>(
        "SELECT answer.player_id, CAST(SUM(answer.round_score) + 0.5 AS INTEGER)
         FROM answer JOIN player ON player.id = answer.player_id
         WHERE player.room_code = ? GROUP BY answer.player_id",
    )
    .bind(code)
    .fetch_all(pool)
    .await
    .unwrap_or_default()
    .into_iter()
    .map(|(id, sum)| (id, sum.unwrap_or(0)))
    .collect();

    // The rating phase blanks every score: a player who sees their own total
    // could cross-reference which anonymous prompt is theirs.
    let hiding = room.phase == "rating";
    let quiz_of = |id: i64| quiz_scores.get(&id).copied().unwrap_or((0, 0));
    let mut rows: Vec<(i64, usize, Value)> = players
        .iter()
        .enumerate()
        .map(|(joined, p)| {
            let (quiz_score, attempts) = quiz_of(p.id);
            let total = quiz_score + totals.get(&p.id).copied().unwrap_or(0);
            let mine = answers.iter().find(|a| a.player_id == p.id);
            let row = json!({
                "name": p.name,
                "score": if hiding { Value::Null } else { json!(total) },
                "answered": mine.is_some(),
                "round_score": if hiding { Value::Null } else { json!(mine.and_then(|a| a.round_score).map(half_up)) },
                "quiz_score": quiz_score,
                "quiz_done": bank > 0 && attempts == bank,
                "online": online_ids.contains(&p.id),
            });
            (if hiding { 0 } else { total }, joined, row)
        })
        .collect();
    rows.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));

    let quiz = if room.phase == "quiz" {
        let revealed = matches!(room.quiz_deadline_ms, Some(d) if now >= d);
        let question: Option<(i64, String, String, String)> = sqlx::query_as(
            "SELECT correct, explanation, prompt, choices FROM quiz_question WHERE room_code = ? AND \"order\" = ?",
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
        // The question itself is not secret (every phone shows it): sent so
        // the console can show what the class is reading.
        let (prompt, choices) = question
            .as_ref()
            .map(|q| (q.2.clone(), serde_json::from_str::<Value>(&q.3).unwrap_or(json!([]))))
            .unwrap_or((String::new(), json!([])));
        let key = question.filter(|_| revealed);
        json!({
            "index": room.quiz_index,
            "prompt": prompt,
            "choices": choices,
            "total": bank,
            "deadline_ms": room.paused_ms.map(|l| now + l).or(room.quiz_deadline_ms).unwrap_or(0),
            "server_ms": now,
            "paused_left_ms": room.paused_ms,
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

    // Per-viewer: each player sees only their own attempt count for the round.
    let mut viewer = json!({
        "attempts": answers
            .iter()
            .map(|a| (a.player_id.to_string(), json!(a.attempts)))
            .collect::<serde_json::Map<_, _>>(),
        // Per-viewer too: a player's own conversation, so a reload redraws it.
        "turns": answers
            .iter()
            .map(|a| (a.player_id.to_string(), json!(crate::llm::parse_turns(a.turns.as_deref()))))
            .collect::<serde_json::Map<_, _>>(),
    });
    // Admin only (kept by `personalize` for viewer None): every grade of the round,
    // with the author, so the console shows progress live.
    let all_ratings: Vec<(i64, i64, i64)> = match &round {
        Some((id, _)) => sqlx::query_as(
            "SELECT rating.answer_id, rating.rater_id, rating.score FROM rating
             JOIN answer ON answer.id = rating.answer_id WHERE answer.round_id = ?",
        )
        .bind(id)
        .fetch_all(pool)
        .await
        .unwrap_or_default(),
        None => Vec::new(),
    };
    let shown = match (&round, room.phase.as_str()) {
        (Some((_, revealed)), "rating") => answers.get(*revealed as usize),
        _ => None,
    };
    let mut admin_results: Vec<Value> = players
        .iter()
        .map(|p| {
            let mine = answers.iter().find(|a| a.player_id == p.id);
            let grades: Vec<i64> = mine
                .map(|a| all_ratings.iter().filter(|r| r.0 == a.id).map(|r| r.2).collect())
                .unwrap_or_default();
            json!({
                "name": p.name,
                "answered": mine.is_some(),
                "prompt": mine.map(|a| a.prompt.clone()),
                "grades": grades,
                "average": quality(&grades),
                // None for the shown prompt's own author, who grades nothing.
                "graded_current": shown.and_then(|s| {
                    (s.player_id != p.id)
                        .then(|| all_ratings.iter().any(|r| r.0 == s.id && r.1 == p.id))
                }),
            })
        })
        .collect();
    admin_results.sort_by_key(|r| std::cmp::Reverse(r["average"].as_i64().unwrap_or(i64::MIN)));
    viewer["admin_results"] = json!(admin_results);

    let reveal = match (&round, room.phase.as_str()) {
        (Some((_, revealed)), "rating") => match answers.get(*revealed as usize) {
            Some(a) => {
                let names: HashMap<i64, &str> =
                    players.iter().map(|p| (p.id, p.name.as_str())).collect();
                let ratings: Vec<(i64, i64)> =
                    sqlx::query_as("SELECT rater_id, score FROM rating WHERE answer_id = ?")
                        .bind(a.id)
                        .fetch_all(pool)
                        .await
                        .unwrap_or_default();
                let rated = ratings.len() as i64;
                viewer["author_id"] = json!(a.player_id);
                // Lets `personalize` leave the viewer's own prompt out of the progress bar.
                viewer["answerers"] = json!(answers.iter().map(|x| x.player_id).collect::<Vec<_>>());
                // Per-viewer: each rater gets back the note they gave (reconnects included).
                viewer["ratings"] = json!(ratings
                    .iter()
                    .map(|(rater, score)| (rater.to_string(), json!(score)))
                    .collect::<serde_json::Map<_, _>>());
                json!({
                    "index": revealed,
                    "count": answers.len(),
                    // Per-viewer, resolved in `personalize`; admins and spectators grade nothing.
                    "gradable_count": answers.len(),
                    "gradable_index": revealed,
                    "prompt": a.prompt,
                    "output": a.output,
                    "author": if anonymous { Value::Null } else { json!(names.get(&a.player_id)) },
                    "author_label": labels(&players, anonymous).get(&a.player_id),
                    "rated": rated,
                    "mine": false,
                    "my_score": Value::Null,
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
                    "round_score": a.round_score.map(half_up),
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

    // A reconnect re-arms its clock from these two numbers, and its place from
    // `phase` and `current` below: all four travel in every snapshot.
    // A paused clock travels as its frozen time left: the screens show it
    // without counting down (`paused_left_ms`).
    let paused_clock = |left: i64| json!({"deadline_ms": now + left, "server_ms": now, "paused_left_ms": left});
    let timer = match (room.paused_ms, room.phase.as_str()) {
        (Some(left), phase) if phase != "quiz" => paused_clock(left),
        _ => room
            .phase_deadline_ms
            .map(|d| json!({"deadline_ms": d, "server_ms": now}))
            .unwrap_or(Value::Null),
    };
    let online_count = rows.iter().filter(|r| r.2["online"] == true).count();
    Ok(json!({
        "code": room.code,
        "phase": room.phase,
        "current": room.current,
        "total": total_rounds,
        "bonus_left": bonus_left(pool, &room, total_rounds).await,
        "target_image": target.as_deref().and_then(shown_image_for),
        "target_tip": target.as_deref().and_then(tip_for),
        // Hidden while writing: a phone writing must not hold the checklist, not even
        // in devtools, or it would write to the list. Raters need it to grade.
        "target_criteria": if room.phase == "summary" || room.phase == "rating" {
            target.as_deref().map(criteria_for).unwrap_or_default()
        } else {
            Vec::new()
        },
        "target": target,
        "timer": timer,
        "paused": room.paused_ms.is_some(),
        "anonymous": anonymous,
        "auto_mode": room.auto_mode != 0,
        "quiz_seconds": room.quiz_seconds,
        "quiz_count": room.quiz_count,
        "round_count": room.round_count,
        "tutorial": room.tutorial != 0,
        "round_seconds": room.round_seconds,
        "rate_seconds": room.rate_seconds,
        "max_attempts": target.as_deref().map(max_attempts_for).unwrap_or(MAX_ATTEMPTS),
        "my_attempts": 0,
        "conversation": target.as_deref().is_some_and(is_conversation),
        "my_turns": [],
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
    if viewer.is_none() {
        out["admin_results"] = private["admin_results"].clone();
    }
    if let Some(me) = viewer {
        out["my_attempts"] = json!(private["attempts"][me.to_string()].as_i64().unwrap_or(0));
        if let Some(turns) = private["turns"].get(me.to_string()) {
            out["my_turns"] = turns.clone();
        }
        if out["reveal"].is_object() {
            out["reveal"]["mine"] = json!(private["author_id"].as_i64() == Some(me));
            out["reveal"]["my_score"] = private["ratings"][me.to_string()].clone();
            // Own prompt is not gradable: drop it from the count, and from the index when it
            // comes before the one on screen. On the own prompt the index is the number of
            // gradable prompts already passed, so the bar stays put.
            let own = private["answerers"]
                .as_array()
                .and_then(|ids| ids.iter().position(|v| v.as_i64() == Some(me)));
            let index = out["reveal"]["index"].as_i64().unwrap_or(0);
            out["reveal"]["gradable_count"] =
                json!(out["reveal"]["count"].as_i64().unwrap_or(0) - own.is_some() as i64);
            out["reveal"]["gradable_index"] = json!(index - own.is_some_and(|o| (o as i64) < index) as i64);
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
        assert_eq!(quiz_bank().len(), 6);
        assert_eq!(targets().len(), 7);
        assert_eq!(round_count(), 5);
        assert_eq!(optional_targets().count(), 2);
        assert!(quiz_bank().iter().all(|q| q.choices.len() == 4 && (0..4).contains(&q.correct)));
    }

    #[test]
    fn round_score_is_peer_average() {
        assert_eq!(quality(&[]), None);
        assert_eq!(quality(&[7]), Some(7));
        assert_eq!(quality(&[6, 7]), Some(7)); // 6.5 rounds half up
        assert_eq!(quality(&[10, 10, 10]), Some(10));
        assert_eq!(quality(&[1, 1]), Some(1));
        assert_eq!(threshold(3), 2);
        assert_eq!(threshold(4), 3);
        assert_eq!(threshold(0), 0);
    }

    #[tokio::test]
    async fn start_plays_every_question_and_every_core_round() {
        let pool = crate::db::test_pool().await;
        crate::db::rooms::create_room(&pool, "LN33").await.unwrap();
        create_bank(&pool, "LN33").await.unwrap();
        // A count left in the row by an older lobby is ignored: the length is fixed.
        sqlx::query("UPDATE room SET quiz_count = 2, round_count = 3 WHERE code = 'LN33'")
            .execute(&pool)
            .await
            .unwrap();
        crate::db::rooms::add_player(&pool, "LN33", "ana").await.unwrap();
        assert!(start_game(&pool, "LN33").await.unwrap());
        let snap = room_snapshot(&pool, "LN33", None).await.unwrap();
        assert_eq!(snap["total"], 5);
        assert_eq!(snap["quiz"]["total"], 6);
        assert_eq!(snap["quiz_count"], 6);
        assert_eq!(snap["round_count"], 5);
    }

    #[tokio::test]
    async fn each_start_draws_questions_and_core_targets_at_random() {
        let pool = crate::db::test_pool().await;
        crate::db::rooms::create_room(&pool, "RN44").await.unwrap();
        let mut firsts = HashSet::new();
        for _ in 0..20 {
            sqlx::query("UPDATE room SET phase = 'waiting' WHERE code = 'RN44'").execute(&pool).await.unwrap();
            assert!(start_game(&pool, "RN44").await.unwrap());
            let targets: Vec<String> = sqlx::query_scalar("SELECT target FROM round WHERE room_code = 'RN44' ORDER BY \"order\"")
                .fetch_all(&pool).await.unwrap();
            let prompts: Vec<String> = sqlx::query_scalar("SELECT prompt FROM quiz_question WHERE room_code = 'RN44' ORDER BY \"order\"")
                .fetch_all(&pool).await.unwrap();
            assert_eq!(targets.len() as i64, round_count());
            assert_eq!(prompts.len() as i64, quiz_count());
            let core: HashSet<&str> = core_targets().map(|t| t.text.as_str()).collect();
            assert_eq!(targets.iter().map(String::as_str).collect::<HashSet<_>>(), core, "every core target once, no optional one");
            assert_eq!(prompts.iter().collect::<HashSet<_>>().len(), prompts.len(), "no question twice");
            firsts.insert((targets[0].clone(), prompts[0].clone()));
        }
        assert!(firsts.len() > 1, "20 starts never changed the draw");
    }

    #[tokio::test]
    async fn pass_drops_the_step_on_screen_and_opens_the_next() {
        let pool = crate::db::test_pool().await;
        crate::db::rooms::create_room(&pool, "PS66").await.unwrap();
        create_bank(&pool, "PS66").await.unwrap();
        crate::db::rooms::add_player(&pool, "PS66", "ana").await.unwrap();
        assert!(start_game(&pool, "PS66").await.unwrap());
        // Quizz: the open question is dropped, the next opens; the last opens round 1.
        pass(&pool, "PS66").await.unwrap();
        let room = load(&pool, "PS66").await.unwrap();
        assert_eq!((room.phase.as_str(), room.quiz_index), ("quiz", 1));
        assert!(room.quiz_open(), "the next question is open at once");
        for _ in 1..quiz_count() {
            pass(&pool, "PS66").await.unwrap();
        }
        let room = load(&pool, "PS66").await.unwrap();
        assert_eq!((room.phase.as_str(), room.current), ("playing", 0));
        // Rounds: straight to the next one, from writing or from rating.
        pass(&pool, "PS66").await.unwrap();
        let room = load(&pool, "PS66").await.unwrap();
        assert_eq!((room.phase.as_str(), room.current), ("playing", 1));
        go(&pool, &room, "rating").await.unwrap();
        pass(&pool, "PS66").await.unwrap();
        assert_eq!(load(&pool, "PS66").await.unwrap().current, 2);
        // The last round stops on its summary: the host ends or adds a bonus.
        sqlx::query("UPDATE room SET current = 4 WHERE code = 'PS66'").execute(&pool).await.unwrap();
        pass(&pool, "PS66").await.unwrap();
        let room = load(&pool, "PS66").await.unwrap();
        assert_eq!((room.phase.as_str(), room.current), ("summary", 4));
        assert!(pass(&pool, "PS66").await.is_err(), "nothing to pass on a summary");
    }

    #[tokio::test]
    async fn host_pauses_extends_and_resumes_the_clock() {
        let pool = crate::db::test_pool().await;
        crate::db::rooms::create_room(&pool, "CK77").await.unwrap();
        create_bank(&pool, "CK77").await.unwrap();
        crate::db::rooms::add_player(&pool, "CK77", "ana").await.unwrap();
        assert!(start_game(&pool, "CK77").await.unwrap());
        // Quizz: a paused question stays open and shows no answer.
        pause_clock(&pool, "CK77").await.unwrap();
        assert!(pause_clock(&pool, "CK77").await.is_err(), "already paused");
        let room = load(&pool, "CK77").await.unwrap();
        assert!(room.quiz_open() && room.quiz_deadline_ms.is_none());
        let left = room.paused_ms.unwrap();
        add_time(&pool, "CK77", 30).await.unwrap();
        assert_eq!(load(&pool, "CK77").await.unwrap().paused_ms, Some(left + 30_000));
        let snap = room_snapshot(&pool, "CK77", None).await.unwrap();
        assert_eq!(snap["paused"], true);
        assert_eq!(snap["quiz"]["revealed"], false);
        assert_eq!(snap["quiz"]["paused_left_ms"], left + 30_000);
        resume_clock(&pool, "CK77").await.unwrap();
        let room = load(&pool, "CK77").await.unwrap();
        assert!(room.paused_ms.is_none());
        assert!(room.quiz_deadline_ms.unwrap() > now_ms() + left);
        // A new step starts on a running clock.
        pause_clock(&pool, "CK77").await.unwrap();
        skip(&pool, "CK77").await.unwrap();
        let room = load(&pool, "CK77").await.unwrap();
        assert_eq!(room.phase, "playing");
        assert!(room.paused_ms.is_none() && room.phase_deadline_ms.is_some());
        // Writing: paused, then nothing expires and the snapshot carries the frozen time.
        let before = room.phase_deadline_ms.unwrap();
        add_time(&pool, "CK77", 60).await.unwrap();
        assert!(load(&pool, "CK77").await.unwrap().phase_deadline_ms.unwrap() >= before + 59_000);
        pause_clock(&pool, "CK77").await.unwrap();
        let snap = room_snapshot(&pool, "CK77", None).await.unwrap();
        assert!(snap["timer"]["paused_left_ms"].as_i64().unwrap() > 0);
        // The rating has no clock: the host moves on after the discussion.
        let room = load(&pool, "CK77").await.unwrap();
        go(&pool, &room, "rating").await.unwrap();
        let room = load(&pool, "CK77").await.unwrap();
        assert!(room.paused_ms.is_none() && room.phase_deadline_ms.is_none(), "rating is not timed");
    }

    #[tokio::test]
    async fn host_ends_the_game_from_any_step() {
        let pool = crate::db::test_pool().await;
        crate::db::rooms::create_room(&pool, "EN88").await.unwrap();
        create_bank(&pool, "EN88").await.unwrap();
        crate::db::rooms::add_player(&pool, "EN88", "ana").await.unwrap();
        assert!(end_game(&pool, "EN88").await.is_err(), "nothing to end in the lobby");
        assert!(start_game(&pool, "EN88").await.unwrap());
        end_game(&pool, "EN88").await.unwrap();
        let room = load(&pool, "EN88").await.unwrap();
        assert_eq!(room.phase, "done");
        assert!(room.quiz_deadline_ms.is_some() && room.phase_deadline_ms.is_none());
        assert!(end_game(&pool, "EN88").await.is_err(), "already over");
    }

    #[tokio::test]
    async fn manual_rating_waits_for_the_host_even_when_everyone_rated() {
        let pool = crate::db::test_pool().await;
        crate::db::rooms::create_room(&pool, "MR99").await.unwrap();
        sqlx::query("UPDATE room SET tutorial = 0 WHERE code = 'MR99'").execute(&pool).await.unwrap();
        let ana = crate::db::rooms::add_player(&pool, "MR99", "ana").await.unwrap();
        let bob = crate::db::rooms::add_player(&pool, "MR99", "bob").await.unwrap();
        let online: HashSet<i64> = [ana.id, bob.id].into();
        assert!(start_game(&pool, "MR99").await.unwrap());
        let round: i64 = sqlx::query_scalar("SELECT id FROM round WHERE room_code = 'MR99' AND \"order\" = 0")
            .fetch_one(&pool).await.unwrap();
        let a = crate::db::answers::insert_answer(&pool, round, ana.id, "p1", "o1").await.unwrap();
        crate::db::answers::insert_answer(&pool, round, bob.id, "p2", "o2").await.unwrap();
        advance(&pool, "MR99").await.unwrap();
        let room = load(&pool, "MR99").await.unwrap();
        assert_eq!(room.phase, "rating");
        assert!(room.phase_deadline_ms.is_none(), "no clock on the rating");
        // bob rated ana's prompt, and votes: nothing moves without the host.
        crate::db::answers::insert_rating(&pool, a.id, bob.id, 7).await.unwrap();
        crate::db::answers::add_ready(&pool, "MR99", bob.id, "rating:0:0").await.unwrap();
        maybe_advance_phase(&pool, "MR99", &online).await;
        maybe_advance_by_votes(&pool, "MR99", &online).await;
        let revealed: i64 = sqlx::query_scalar("SELECT revealed FROM round WHERE id = ?").bind(round).fetch_one(&pool).await.unwrap();
        assert_eq!(revealed, 0, "still on the first prompt");
        advance(&pool, "MR99").await.unwrap();
        let revealed: i64 = sqlx::query_scalar("SELECT revealed FROM round WHERE id = ?").bind(round).fetch_one(&pool).await.unwrap();
        assert_eq!(revealed, 1, "the host moved on");
    }

    #[tokio::test]
    async fn last_summary_offers_the_optional_rounds_one_by_one() {
        let pool = crate::db::test_pool().await;
        crate::db::rooms::create_room(&pool, "BN55").await.unwrap();
        crate::db::rooms::add_player(&pool, "BN55", "ana").await.unwrap();
        sqlx::query("UPDATE room SET tutorial = 0 WHERE code = 'BN55'").execute(&pool).await.unwrap();
        assert!(start_game(&pool, "BN55").await.unwrap());
        // Mid-game summary: no bonus offered, and the button refuses.
        go(&pool, &load(&pool, "BN55").await.unwrap(), "summary").await.unwrap();
        assert_eq!(room_snapshot(&pool, "BN55", None).await.unwrap()["bonus_left"], 0);
        assert!(bonus_round(&pool, "BN55").await.is_err());
        sqlx::query("UPDATE room SET current = 4 WHERE code = 'BN55'").execute(&pool).await.unwrap();
        assert_eq!(room_snapshot(&pool, "BN55", None).await.unwrap()["bonus_left"], 2);

        let bonus: Vec<&str> = optional_targets().map(|t| t.text.as_str()).collect();
        for (i, text) in bonus.iter().enumerate() {
            bonus_round(&pool, "BN55").await.unwrap();
            let snap = room_snapshot(&pool, "BN55", None).await.unwrap();
            assert_eq!(snap["phase"], "playing");
            assert_eq!(snap["target"], *text, "optional rounds follow the file order");
            assert_eq!(snap["total"], 6 + i as i64);
            go(&pool, &load(&pool, "BN55").await.unwrap(), "summary").await.unwrap();
        }
        assert_eq!(room_snapshot(&pool, "BN55", None).await.unwrap()["bonus_left"], 0);
        assert!(bonus_round(&pool, "BN55").await.is_err(), "nothing left");
        advance(&pool, "BN55").await.unwrap();
        assert_eq!(load(&pool, "BN55").await.unwrap().phase, "done");
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
        assert_eq!(snap["total"], 5, "every core round");
        assert_eq!(snap["quiz"]["index"], 0);
        assert_eq!(snap["quiz"]["total"], 6, "the whole quizz bank");
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
        assert!(personalize(&snap, Some(ana.id)).get("admin_results").is_none());
        assert_eq!(personalize(&snap, None)["admin_results"][0]["name"], "ana");
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
        // The class's own advance runs in automatic mode only.
        sqlx::query("UPDATE room SET auto_mode = 1 WHERE code = 'CC44'").execute(&pool).await.unwrap();
        let round: i64 = sqlx::query_scalar("SELECT id FROM round WHERE room_code = 'CC44' AND \"order\" = 0")
            .fetch_one(&pool).await.unwrap();
        let mut answers = Vec::new();
        for &id in &ids {
            answers.push(crate::db::answers::insert_answer(&pool, round, id, "p", "o").await.unwrap().id);
        }
        // Writing never closes early (players may retry): the clock or the admin opens the rating.
        maybe_advance_phase(&pool, "CC44", &online).await;
        assert_eq!(load(&pool, "CC44").await.unwrap().phase, "playing");
        advance(&pool, "CC44").await.unwrap();
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
    async fn lobby_votes_never_auto_start_only_manual_start_counts_late_joiners() {
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
        assert_eq!(load(&pool, "LB55").await.unwrap().phase, "waiting", "3 of 5 still waits for the animateur");
        let snap = room_snapshot(&pool, "LB55", Some(&online)).await.unwrap();
        assert_eq!((snap["ready"]["count"].as_i64(), snap["ready"]["needed"].as_i64()), (Some(3), Some(3)));

        // Seul le bouton de l'animateur lance la partie.
        assert!(start_game(&pool, "LB55").await.unwrap());
        assert_eq!(load(&pool, "LB55").await.unwrap().phase, "quiz");
        assert!(!start_game(&pool, "LB55").await.unwrap(), "second start is a no-op");
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
        assert!(!expire_phase(&state, "EX33").await, "late prompts are still taken during the grace");
        sqlx::query(&sql).bind(now_ms() - PROMPT_GRACE_MS - 1000).execute(&pool).await.unwrap();
        assert!(expire_phase(&state, "EX33").await);
        assert!(!expire_phase(&state, "EX33").await, "claimed once");
        let room = load(&pool, "EX33").await.unwrap();
        assert_eq!(room.phase, "summary");
        assert!(room.phase_deadline_ms.is_none());
    }

    // The playing timer is not armed once: the ticker replays expire_phase
    // while the clock reads past the deadline, which is what carries the room
    // through PROMPT_GRACE_MS (an in-flight draft must not be cut).
    #[tokio::test]
    async fn ticker_keeps_rechecking_playing_until_prompt_window_closed() {
        std::env::remove_var("OPENAI_API_KEY");
        let pool = crate::db::test_pool().await;
        let state = AppState::new(pool.clone());
        crate::db::rooms::create_room(&pool, "SC77").await.unwrap();
        sqlx::query("UPDATE room SET tutorial = 0 WHERE code = 'SC77'")
            .execute(&pool)
            .await
            .unwrap();
        assert!(start_game(&pool, "SC77").await.unwrap());
        assert_eq!(load(&pool, "SC77").await.unwrap().phase, "playing");
        let mut announced: HashMap<String, i64> = HashMap::new();

        // The sweep runs at the deadline: the prompt window is still open, the
        // room must not move even once.
        let sql = format!("UPDATE room SET phase_deadline = {TS_FROM_MS} WHERE code = 'SC77'");
        sqlx::query(&sql).bind(now_ms() - 1000).execute(&pool).await.unwrap();
        ticker_sweep(&state, &mut announced).await;
        assert_eq!(load(&pool, "SC77").await.unwrap().phase, "playing");

        // The same sweep, with the grace now lapsed: it advances the room on
        // its own — no retry, no extra arming needed.
        sqlx::query(&sql).bind(now_ms() - PROMPT_GRACE_MS - 1000).execute(&pool).await.unwrap();
        ticker_sweep(&state, &mut announced).await;
        let room = load(&pool, "SC77").await.unwrap();
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

        // Lobby : les votes comptent le quorum, seul l'animateur lance la partie.
        crate::db::answers::add_ready(&pool, "RD22", ana.id, LOBBY_STEP).await.unwrap();
        crate::db::answers::add_ready(&pool, "RD22", bob.id, LOBBY_STEP).await.unwrap();
        maybe_advance_by_votes(&pool, "RD22", &online).await;
        assert_eq!(load(&pool, "RD22").await.unwrap().phase, "waiting", "votes never start the game");
        assert!(start_game(&pool, "RD22").await.unwrap());
        // The class's own advance runs in automatic mode only.
        sqlx::query("UPDATE room SET auto_mode = 1 WHERE code = 'RD22'").execute(&pool).await.unwrap();
        let snap = room_snapshot(&pool, "RD22", Some(&online)).await.unwrap();
        assert_eq!(snap["phase"], "playing");
        assert!(snap["target"].is_string());
        assert!(snap["target_criteria"].as_array().unwrap().is_empty(), "criteria hidden while writing");
        assert!(snap["timer"]["deadline_ms"].is_i64());

        let round: i64 = sqlx::query_scalar("SELECT id FROM round WHERE room_code = 'RD22' AND \"order\" = 0")
            .fetch_one(&pool)
            .await
            .unwrap();
        let a = crate::db::answers::insert_answer(&pool, round, ana.id, "p1", "o1").await.unwrap();
        maybe_advance_phase(&pool, "RD22", &online).await;
        assert_eq!(load(&pool, "RD22").await.unwrap().phase, "playing", "bob has not written");
        let b = crate::db::answers::insert_answer(&pool, round, bob.id, "p2", "o2").await.unwrap();
        maybe_advance_phase(&pool, "RD22", &online).await;
        assert_eq!(load(&pool, "RD22").await.unwrap().phase, "playing", "everyone wrote, retries stay open");
        advance(&pool, "RD22").await.unwrap();

        let snap = room_snapshot(&pool, "RD22", Some(&online)).await.unwrap();
        assert_eq!(snap["phase"], "rating");
        assert!(!snap["target_criteria"].as_array().unwrap().is_empty(), "criteria shown while rating");
        assert_eq!(snap["reveal"]["output"], "o1");
        assert!(snap["players"][0]["score"].is_null(), "scores hidden while rating");
        assert_eq!(snap["reveal"]["author_label"], "Joueur 1");
        assert!(snap["reveal"]["author"].is_null());
        assert_eq!(personalize(&snap, Some(ana.id))["reveal"]["mine"], true);
        assert_eq!(personalize(&snap, Some(bob.id))["reveal"]["mine"], false);
        assert_eq!(snap["ready"]["needed"], 1, "author excluded from the bar");

        crate::db::answers::insert_rating(&pool, a.id, bob.id, 8).await.unwrap();
        // A reconnecting rater gets back the note they gave; the author gets none.
        let snap = room_snapshot(&pool, "RD22", Some(&online)).await.unwrap();
        assert_eq!(personalize(&snap, Some(bob.id))["reveal"]["my_score"], 8);
        assert!(personalize(&snap, Some(ana.id))["reveal"]["my_score"].is_null());
        // The per-rater list never leaves the private block: no viewer, admin included, gets rater ids.
        for viewer in [Some(ana.id), Some(bob.id), None] {
            let out = personalize(&snap, viewer);
            assert!(out["reveal"].get("ratings").is_none());
            assert!(out.get(VIEWER_KEY).is_none());
            assert!(out.get("ratings").is_none());
        }
        // Admin sees the grades per author (no rater ids); a player never gets them.
        let admin_grades = &personalize(&snap, None)["admin_results"];
        assert!(admin_grades.as_array().unwrap().iter().any(|r| r["grades"] == json!([8])));
        assert!(personalize(&snap, Some(ana.id)).get("admin_results").is_none());
        maybe_advance_phase(&pool, "RD22", &online).await;
        crate::db::answers::insert_rating(&pool, b.id, ana.id, 4).await.unwrap();
        maybe_advance_phase(&pool, "RD22", &online).await;

        let snap = room_snapshot(&pool, "RD22", Some(&online)).await.unwrap();
        assert_eq!(snap["phase"], "summary");
        assert!(!snap["target_criteria"].as_array().unwrap().is_empty(), "criteria shown on the round summary");
        let rows = snap["summary"]["rows"].as_array().unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["round_score"], 8);
        assert_eq!(rows[1]["round_score"], 4);

        for _ in 0..round_count() {
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

    #[tokio::test]
    async fn removing_a_player_keeps_the_rating_cursor_on_its_prompt() {
        let pool = crate::db::test_pool().await;
        crate::db::rooms::create_room(&pool, "RM22").await.unwrap();
        sqlx::query("UPDATE room SET tutorial = 0 WHERE code = 'RM22'").execute(&pool).await.unwrap();
        let mut players = Vec::new();
        for name in ["ana", "bob", "cy"] {
            players.push(crate::db::rooms::add_player(&pool, "RM22", name).await.unwrap());
        }
        assert!(start_game(&pool, "RM22").await.unwrap());
        let round: i64 = sqlx::query_scalar("SELECT id FROM round WHERE room_code = 'RM22' AND \"order\" = 0")
            .fetch_one(&pool).await.unwrap();
        for p in &players {
            crate::db::answers::insert_answer(&pool, round, p.id, "p", "o").await.unwrap();
        }
        advance(&pool, "RM22").await.unwrap();
        sqlx::query("UPDATE round SET revealed = 1 WHERE id = ?").bind(round).execute(&pool).await.unwrap();
        // bob's prompt is on screen; ana's, before it, goes: the cursor follows bob's.
        assert!(remove_player(&pool, "RM22", players[0].id).await.unwrap());
        let revealed: i64 = sqlx::query_scalar("SELECT revealed FROM round WHERE id = ?").bind(round).fetch_one(&pool).await.unwrap();
        assert_eq!(revealed, 0);
        assert_eq!(round_answers(&pool, round).await[0].player_id, players[1].id);
        assert!(!remove_player(&pool, "RM22", players[0].id).await.unwrap(), "already gone");
        // cy's prompt, the last, is on screen and goes: nothing is left, the round closes.
        sqlx::query("UPDATE round SET revealed = 1 WHERE id = ?").bind(round).execute(&pool).await.unwrap();
        assert!(remove_player(&pool, "RM22", players[2].id).await.unwrap());
        assert_eq!(load(&pool, "RM22").await.unwrap().phase, "summary");
    }

    #[tokio::test]
    async fn progress_bar_leaves_out_the_viewers_own_prompt() {
        let pool = crate::db::test_pool().await;
        crate::db::rooms::create_room(&pool, "GR44").await.unwrap();
        sqlx::query("UPDATE room SET tutorial = 0 WHERE code = 'GR44'").execute(&pool).await.unwrap();
        let mut players = Vec::new();
        for name in ["ana", "bob", "cy", "di"] {
            players.push(crate::db::rooms::add_player(&pool, "GR44", name).await.unwrap());
        }
        let online: HashSet<i64> = players.iter().map(|p| p.id).collect();
        assert!(start_game(&pool, "GR44").await.unwrap());
        let round: i64 = sqlx::query_scalar("SELECT id FROM round WHERE room_code = 'GR44' AND \"order\" = 0")
            .fetch_one(&pool)
            .await
            .unwrap();
        for p in &players {
            crate::db::answers::insert_answer(&pool, round, p.id, "p", "o").await.unwrap();
        }
        advance(&pool, "GR44").await.unwrap();

        // Answers are listed in submission order, so prompt i belongs to players[i].
        for (v, viewer) in players.iter().enumerate() {
            let mut seen = Vec::new();
            for i in 0..4i64 {
                sqlx::query("UPDATE round SET revealed = ? WHERE id = ?")
                    .bind(i)
                    .bind(round)
                    .execute(&pool)
                    .await
                    .unwrap();
                let snap = room_snapshot(&pool, "GR44", Some(&online)).await.unwrap();
                let reveal = &personalize(&snap, Some(viewer.id))["reveal"];
                assert_eq!(reveal["count"], 4);
                assert_eq!(reveal["gradable_count"], 3);
                if i as usize != v {
                    seen.push(reveal["gradable_index"].as_i64().unwrap());
                }
            }
            assert_eq!(seen, [0, 1, 2], "viewer {v}");
        }
    }
}
