-- Manches « conversation » (data/targets.json, "conversation": true) : les
-- prompts s'enchaînent au lieu de se remplacer. Chaque échange est gardé ici,
-- en JSON [{"prompt": ..., "output": ...}], pour redonner l'historique au
-- modèle au prompt suivant. NULL pour les manches à un seul prompt.
ALTER TABLE answer ADD COLUMN turns TEXT;
