FROM rust:1.89-slim AS build
WORKDIR /app
# Dependency manifests first so `cargo build` reuses the cached layer when
# only src/ changes. Full cargo-chef vendoring was rejected as overkill here.
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY migrations ./migrations
# Quiz bank and round targets, compiled in via include_str!.
COPY data ./data
RUN cargo build --release --locked

FROM debian:bookworm-slim
WORKDIR /app
# ca-certificates: reqwest talks HTTPS to the LLM endpoint; without these every
# billed call fails with an opaque TLS error. curl serves the HEALTHCHECK only.
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl \
  && rm -rf /var/lib/apt/lists
COPY --from=build /app/target/release/server /app/server
# SQLite WAL writes -wal/-shm next to the db file, so the whole directory must
# live on the volume: a single-file symlink leaves them in the overlay and
# they vanish on restart (split-brain). DATABASE_URL points straight at /data.
ENV DATABASE_URL=sqlite:/data/server.sqlite3?mode=rwc
ENV RUST_LOG=info
EXPOSE 3001
# Non-root: created without login/shell, owns only what it must write.
RUN useradd --no-create-home --shell /usr/sbin/nologin appuser \
  && mkdir -p /data \
  && chown appuser:appuser /data
USER appuser
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:3001/healthz | grep -q '"ok":true'
# Exec form: the server is PID 1 and receives SIGTERM directly.
CMD ["./server", "--port", "3001"]
