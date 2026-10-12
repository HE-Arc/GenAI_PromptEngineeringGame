FROM rust:1.89-slim AS build
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY migrations ./migrations
# Quiz bank and round targets, compiled in via include_str!.
COPY data ./data
RUN cargo build --release

FROM debian:bookworm-slim
WORKDIR /app
COPY --from=build /app/target/release/server /app/server
# SQLite lives on a named volume; the symlink keeps the default DATABASE_URL working.
CMD ln -sf /data/server.sqlite3 /app/server.sqlite3 \
  && ./server --port 3001
