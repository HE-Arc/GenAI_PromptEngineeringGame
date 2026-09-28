#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -f backend/.env ]; then set -a; . backend/.env; set +a; fi
(cd backend && uv run python manage.py migrate && uv run python manage.py runserver 8000) &
BACK=$!
(cd frontend && bun run dev) &
FRONT=$!
trap 'kill $BACK $FRONT 2>/dev/null' EXIT
wait
