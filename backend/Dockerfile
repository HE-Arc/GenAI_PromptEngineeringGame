FROM python:3.12-slim

# Install uv for locked, reproducible installs.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first so the layer survives code edits.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY . .

# SQLite lives on a named volume; the symlink keeps Django's BASE_DIR path working.
CMD ln -sf /data/db.sqlite3 /app/db.sqlite3 \
  && uv run python manage.py migrate \
  && uv run python manage.py close_rooms \
  && uv run python manage.py runserver 0.0.0.0:8000
