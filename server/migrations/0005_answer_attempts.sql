-- Essais envoyés par un joueur pour une manche (plafonnés à MAX_ATTEMPTS côté
-- serveur). Un essai qui plante ne compte pas : la ligne témoin est écrite à 0.
-- Colonne additive : les réponses existantes comptent pour un essai.
ALTER TABLE answer ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1;
