-- Images produites par le modèle d'image pour les manches « générer une image ».
-- Stockées ici plutôt qu'en base64 dans answer.output : chaque snapshot de la
-- salle embarque toutes les réponses, une image inline le ferait peser des Mo.
-- La réponse n'en garde que le lien /api/images/<id>.
CREATE TABLE IF NOT EXISTS generated_image (
    id TEXT PRIMARY KEY,
    round_id INTEGER NOT NULL REFERENCES round (id) ON DELETE CASCADE,
    mime TEXT NOT NULL,
    data BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS generated_image_round ON generated_image (round_id);
