# Single deployable image: Rust API + prebuilt SPA served from /app/dist.
# Build: docker build -t codexis/genai-prompt-game:latest .
# Run:   docker run -p 3001:3001 -v <vol>:/data codexis/genai-prompt-game:latest
# The server serves /app/dist statically (relative /api calls, no API env needed
# for the frontend prod build); the other lane wires ServeDir, this file only
# guarantees /app/dist exists in the final image.

FROM oven/bun:1 AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/bun.lock ./
RUN bun install --frozen-lockfile
COPY frontend ./
RUN bun run build

FROM rust:1.89-slim AS build
WORKDIR /app
# Dependency manifests first so `cargo build` reuses the cached layer when
# only src/ changes. Full cargo-chef vendoring was rejected as overkill here.
COPY server/Cargo.toml server/Cargo.lock ./
COPY server/src ./src
COPY server/migrations ./migrations
# Quiz bank and round targets, compiled in via include_str!.
COPY server/data ./data
RUN cargo build --release --locked

FROM debian:bookworm-slim
WORKDIR /app
# ca-certificates: reqwest talks HTTPS to the LLM endpoint; without these every
# billed call fails with an opaque TLS error. curl serves the HEALTHCHECK only.
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl \
  && rm -rf /var/lib/apt/lists
# Non-root: created without login/shell, owns only what it must write.
# Created before COPY so --chown can reference it. uid/gid pinned to 1000:
# k8s/deployment.yaml sets runAsUser/fsGroup to the same numbers.
RUN groupadd --gid 1000 appuser \
  && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin appuser \
  && mkdir -p /data /app/dist \
  && chown appuser:appuser /data /app
COPY --from=build --chown=appuser:appuser /app/target/release/server /app/server
COPY --from=frontend --chown=appuser:appuser /frontend/dist /app/dist
# SQLite WAL writes -wal/-shm next to the db file, so the whole directory must
# live on the volume: a single-file symlink leaves them in the overlay and
# they vanish on restart (split-brain). DATABASE_URL points straight at /data.
ENV DATABASE_URL=sqlite:/data/server.sqlite3?mode=rwc
ENV RUST_LOG=info
EXPOSE 3001
USER appuser
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:3001/healthz | grep -q '"ok":true'
# Exec form: the server is PID 1 and receives SIGTERM directly.
CMD ["./server", "--port", "3001"]
