-- Hot-path lookups the initial schema leaves to full scans. UNIQUE
-- constraints already index their exact columns; these cover the filtered
-- and joined reads (token auth, round settle, quiz/sweep scans) instead.
CREATE INDEX IF NOT EXISTS idx_player_token ON player (token);
CREATE INDEX IF NOT EXISTS idx_answer_round_player ON answer (round_id, player_id);
CREATE INDEX IF NOT EXISTS idx_round_room_order ON round (room_code, "order");
CREATE INDEX IF NOT EXISTS idx_room_phase ON room (phase);
CREATE INDEX IF NOT EXISTS idx_quiz_attempt_question ON quiz_attempt (question_id);
CREATE INDEX IF NOT EXISTS idx_quiz_question_room_order ON quiz_question (room_code, "order");
CREATE INDEX IF NOT EXISTS idx_ready_room_step_player ON ready (room_code, step, player_id);
CREATE INDEX IF NOT EXISTS idx_rating_answer_rater ON rating (answer_id, rater_id);
