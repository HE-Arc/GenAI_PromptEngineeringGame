-- L'animateur peut mettre le chrono en pause : le temps restant est gardé
-- ici (en ms) et l'échéance est effacée, si bien que rien n'expire pendant
-- la pause. NULL quand le chrono tourne. Toute transition remet à NULL.
ALTER TABLE room ADD COLUMN paused_ms INTEGER;
