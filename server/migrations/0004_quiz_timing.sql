-- Durée de la fenêtre de réponse du didacticiel par salle (secondes).
-- Les salles existantes gardent 30 s (comportement historique).
-- Idempotence : sqlx applique chaque version une seule fois (suivi via
-- _sqlx_migrations), donc ce fichier ne tourne qu'une fois. Pas de garde
-- `ADD COLUMN IF NOT EXISTS` : SQLite ne le supporte pas.
ALTER TABLE room ADD COLUMN quiz_seconds INTEGER NOT NULL DEFAULT 30;
