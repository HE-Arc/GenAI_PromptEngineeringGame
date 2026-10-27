-- Mode automatique : la salle enchaîne les manches sans intervention.
-- 0 = manuel (comportement historique), 1 = automatique.
-- Colonne additive : les salles existantes restent en manuel.
-- Idempotence : sqlx applique chaque version une seule fois (suivi via
-- _sqlx_migrations), donc ce fichier ne tourne qu'une fois. Pas de garde
-- `ADD COLUMN IF NOT EXISTS` : SQLite ne le supporte pas, et un garde
-- procédural sortirait du cadre `migrate!` (un seul statement simple).
ALTER TABLE room ADD COLUMN auto_mode INTEGER NOT NULL DEFAULT 0;
