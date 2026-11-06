-- Longueur de la partie, choisie par l'animateur dans le lobby : nombre de
-- questions du didacticiel et nombre de manches de prompt. 5 et 5 par défaut.
-- Les salles existantes prennent aussi 5 et 5 ; elles n'en tiennent compte
-- qu'au lancement, donc une partie déjà commencée garde sa longueur.
ALTER TABLE room ADD COLUMN quiz_count INTEGER NOT NULL DEFAULT 5;
ALTER TABLE room ADD COLUMN round_count INTEGER NOT NULL DEFAULT 5;
