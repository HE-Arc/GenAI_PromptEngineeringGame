#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -f backend/.env ]; then set -a; . backend/.env; set +a; fi
# Turns on the idle-room sweeper, in the SERVER process: that is where the heartbeats are, so it is
# the only process that can tell who is still connected.
export GENAI_SWEEP=1
# Every start begins with no game open: close_rooms deletes the rooms of the last session.
(cd backend && uv run python manage.py migrate && uv run python manage.py close_rooms && uv run python manage.py runserver 8000) &
BACK=$!
(cd frontend && bun run dev) &
FRONT=$!
trap 'kill $BACK $FRONT 2>/dev/null' EXIT
wait
