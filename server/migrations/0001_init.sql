-- Room first: every other table keys off it, so a failed Room INSERT
-- must leave nothing behind it.
CREATE TABLE IF NOT EXISTS room (
    code TEXT PRIMARY KEY,
    phase TEXT NOT NULL DEFAULT 'waiting',
    -- Order index of the round being played, not a round id: mirrors
    -- Room.current in game/models.py, resolved to a row by current_round().
    current INTEGER NOT NULL DEFAULT 0,
    tutorial INTEGER NOT NULL DEFAULT 1,
    anonymous INTEGER NOT NULL DEFAULT 1,
    started_at TEXT,
    last_activity_at TEXT,
    quiz_index INTEGER NOT NULL DEFAULT 0,
    quiz_deadline TEXT,
    round_seconds INTEGER NOT NULL DEFAULT 60,
    rate_seconds INTEGER NOT NULL DEFAULT 20,
    phase_deadline TEXT,
    -- No per-room key in Django (global ADMIN_PASSWORD there); added so WS
    -- auth can tell rooms apart without a shared secret.
    admin_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS round (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_code TEXT NOT NULL REFERENCES room (code) ON DELETE CASCADE,
    -- Quoted: ORDER is reserved in SQLite.
    "order" INTEGER NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    revealed INTEGER NOT NULL DEFAULT 0,
    UNIQUE (room_code, "order")
);

CREATE TABLE IF NOT EXISTS player (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_code TEXT NOT NULL REFERENCES room (code) ON DELETE CASCADE,
    token TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (room_code, name)
);

CREATE TABLE IF NOT EXISTS answer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL REFERENCES round (id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES player (id) ON DELETE CASCADE,
    prompt TEXT NOT NULL,
    output TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL DEFAULT 0,
    comment TEXT NOT NULL DEFAULT '',
    quality_llm INTEGER,
    quality_comment TEXT NOT NULL DEFAULT '',
    quality_peers INTEGER,
    quality INTEGER,
    round_score REAL,
    -- One prompt per player per round: the already_answered 409 rests on this.
    UNIQUE (round_id, player_id)
);

CREATE TABLE IF NOT EXISTS quiz_question (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_code TEXT NOT NULL REFERENCES room (code) ON DELETE CASCADE,
    "order" INTEGER NOT NULL,
    prompt TEXT NOT NULL,
    -- JSON array of choices, never matched against: stored whole, read whole.
    choices TEXT NOT NULL DEFAULT '[]',
    correct INTEGER NOT NULL DEFAULT 0,
    explanation TEXT NOT NULL DEFAULT '',
    UNIQUE (room_code, "order")
);

CREATE TABLE IF NOT EXISTS quiz_attempt (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES player (id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL REFERENCES quiz_question (id) ON DELETE CASCADE,
    choice INTEGER NOT NULL,
    correct INTEGER NOT NULL DEFAULT 0,
    -- Phone retries are indistinguishable from second answers: the DB decides.
    UNIQUE (player_id, question_id)
);

CREATE TABLE IF NOT EXISTS rating (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    answer_id INTEGER NOT NULL REFERENCES answer (id) ON DELETE CASCADE,
    rater_id INTEGER NOT NULL REFERENCES player (id) ON DELETE CASCADE,
    score INTEGER NOT NULL,
    -- One score per rater per prompt: the double-vote 409 rests on this.
    UNIQUE (answer_id, rater_id),
    -- Bounds are prompts.SCALE_MIN/SCALE_MAX in Django; literals here must match.
    CHECK (score >= 1 AND score <= 10)
);

CREATE TABLE IF NOT EXISTS ready (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_code TEXT NOT NULL REFERENCES room (code) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES player (id) ON DELETE CASCADE,
    -- Step strings like quiz:3 or rating:2:0: readiness is per step, not per player.
    step TEXT NOT NULL,
    UNIQUE (room_code, step, player_id)
);

-- Lookup paths: room membership scans, round/player answer scans, rater scans.
CREATE INDEX IF NOT EXISTS idx_round_room ON round (room_code);
CREATE INDEX IF NOT EXISTS idx_player_room ON player (room_code);
CREATE INDEX IF NOT EXISTS idx_answer_round ON answer (round_id);
CREATE INDEX IF NOT EXISTS idx_answer_player ON answer (player_id);
CREATE INDEX IF NOT EXISTS idx_rating_answer ON rating (answer_id);
CREATE INDEX IF NOT EXISTS idx_rating_rater ON rating (rater_id);
CREATE INDEX IF NOT EXISTS idx_ready_room_step ON ready (room_code, step);
CREATE INDEX IF NOT EXISTS idx_quiz_question_room ON quiz_question (room_code);
CREATE INDEX IF NOT EXISTS idx_quiz_attempt_player ON quiz_attempt (player_id);
