use axum::{
    extract::{Request, State},
    http::StatusCode,
    middleware::{self, Next},
    response::IntoResponse,
    routing::{delete, get, post},
    Json, Router,
};
use serde_json::json;
use sqlx::SqlitePool;
use std::collections::HashMap;
use std::net::{IpAddr, Ipv4Addr};
use tower_governor::{
    errors::GovernorError,
    governor::GovernorConfigBuilder,
    key_extractor::{KeyExtractor, SmartIpKeyExtractor},
    GovernorLayer,
};
use tower_http::services::{ServeDir, ServeFile};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::{RwLock, Semaphore};

// Shed load before SQLite serializes it: at most this many REST requests run
// at once, each bounded in time and body size. The WS route is excluded — a
// long-lived socket is not a slow request, and holding a permit for its whole
// life would starve REST.
const MAX_CONCURRENT_REST: usize = 256;
const REST_TIMEOUT: Duration = Duration::from_secs(30);
const MAX_BODY_BYTES: usize = 1024 * 1024;

fn rest_slots() -> Arc<Semaphore> {
    static SLOTS: std::sync::OnceLock<Arc<Semaphore>> = std::sync::OnceLock::new();
    Arc::clone(SLOTS.get_or_init(|| Arc::new(Semaphore::new(MAX_CONCURRENT_REST))))
}

async fn limit_concurrency(State(slots): State<Arc<Semaphore>>, req: Request, next: Next) -> impl IntoResponse {
    let Ok(_permit) = slots.try_acquire_owned() else {
        tracing::warn!(path = %req.uri().path(), limit = MAX_CONCURRENT_REST, "request shed: too many concurrent REST requests");
        return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": "busy"}))).into_response();
    };
    next.run(req).await
}

async fn rest_timeout(req: Request, next: Next) -> impl IntoResponse {
    let path = req.uri().path().to_owned();
    match tokio::time::timeout(REST_TIMEOUT, next.run(req)).await {
        Ok(res) => res,
        Err(_) => {
            tracing::error!(%path, timeout_s = REST_TIMEOUT.as_secs(), "request timed out");
            (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": "timeout"}))).into_response()
        }
    }
}

// Above this a request is logged at WARN: SQLite or the snapshot build is
// struggling, which is the early sign of a classroom-sized overload.
const SLOW_REQUEST: Duration = Duration::from_millis(1000);

// One line per request: method, path, status, latency. The path only, never
// the query string: the WS upgrade carries the player token there. Probes and
// static files log at DEBUG so INFO stays readable; 5xx is ERROR, slow is WARN,
// 4xx is INFO because refusals (409 races, wrong code) are normal play.
async fn log_request(req: Request, next: Next) -> impl IntoResponse {
    let method = req.method().clone();
    let path = req.uri().path().to_owned();
    let start = std::time::Instant::now();
    let res = next.run(req).await;
    let status = res.status().as_u16();
    let latency_ms = start.elapsed().as_millis() as u64;
    let quiet = path == "/healthz" || !path.starts_with("/api/");
    if status >= 500 {
        tracing::error!(%method, %path, status, latency_ms, "request failed");
    } else if start.elapsed() >= SLOW_REQUEST {
        tracing::warn!(%method, %path, status, latency_ms, "slow request");
    } else if quiet {
        tracing::debug!(%method, %path, status, latency_ms, "request");
    } else {
        tracing::info!(%method, %path, status, latency_ms, "request");
    }
    res
}

use crate::hub::RoomHub;
use crate::llm::{self, LlmSource};
use crate::rest;
use crate::ws::ws_handler;

#[derive(Clone)]
pub struct AppState {
    pub pool: SqlitePool,
    pub hubs: Arc<RwLock<HashMap<String, Arc<RoomHub>>>>,
    pub llm: Arc<dyn LlmSource>,
    // Process start: rooms with no hub yet count as idle since then.
    pub boot: std::time::Instant,
}

impl AppState {
    pub fn new(pool: SqlitePool) -> Self {
        let llm = llm::from_env(pool.clone());
        Self {
            pool,
            hubs: Arc::new(RwLock::new(HashMap::new())),
            llm,
            boot: std::time::Instant::now(),
        }
    }
}

async fn healthz() -> impl IntoResponse {
    Json(json!({"ok": true}))
}

// The TCP peer address is the key: it cannot be forged per-request, so one
// host always owns one bucket. Forwarded headers (X-Forwarded-For, X-Real-IP,
// Forwarded) are client-controlled — trusting them behind no reverse proxy
// lets anyone rotate buckets around this limit. They are consulted only when
// there is no TCP peer at all (in-process tests): there a header is the only
// way to address two buckets from one process. Production always has a peer
// (into_make_service_with_connect_info in main.rs).
#[derive(Clone)]
struct PeerIp;

impl KeyExtractor for PeerIp {
    type Key = IpAddr;

    fn extract<T>(&self, req: &axum::http::Request<T>) -> Result<Self::Key, GovernorError> {
        if let Some(info) = req
            .extensions()
            .get::<axum::extract::ConnectInfo<std::net::SocketAddr>>()
        {
            return Ok(info.0.ip());
        }
        Ok(SmartIpKeyExtractor
            .extract(req)
            .unwrap_or(IpAddr::V4(Ipv4Addr::LOCALHOST)))
    }
}

// The retained sweepers: each built limiter registers one retain_recent()
// closure; spawn_rate_limit_retention sweeps them all every 60 s. The governor
// key map otherwise grows with every new client IP for the process's life.
static LIMITERS: std::sync::OnceLock<std::sync::Mutex<Vec<Box<dyn Fn() + Send>>>> =
    std::sync::OnceLock::new();

fn remembered_limit<F: Fn() + Send + 'static>(retain: F) {
    let list = LIMITERS.get_or_init(|| std::sync::Mutex::new(Vec::new()));
    list.lock().expect("rate limiter registry").push(Box::new(retain));
}

// Spawns the periodic garbage collection of the governor key map. Call once
// at startup, after build_router has built the limiters.
pub fn spawn_rate_limit_retention() {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(Duration::from_secs(60));
        loop {
            interval.tick().await;
            if let Some(list) = LIMITERS.get() {
                for retain in list.lock().expect("rate limiter registry").iter() {
                    retain();
                }
            }
        }
    });
}

fn keyed_limit(period: Duration, burst: u32) -> GovernorLayer<PeerIp, governor::middleware::NoOpMiddleware> {
    let config = Arc::new(
        GovernorConfigBuilder::default()
            .period(period)
            .burst_size(burst)
            .key_extractor(PeerIp)
            .finish()
            .expect("rate limit config"),
    );
    let limiter = config.limiter().clone();
    remembered_limit(move || limiter.retain_recent());
    GovernorLayer { config }
}

// `keyed_limit(period, burst)`: tower_governor refills ONE request every
// `period`. A whole event (1000 players) can share one IP behind a NAT or a
// proxy, so /join gets a burst of 2000 and 200 joins/s after it: a class never
// waits, a script flooding the player table still does.
fn join_limit() -> GovernorLayer<PeerIp, governor::middleware::NoOpMiddleware> {
    keyed_limit(Duration::from_millis(5), 2000)
}

// Admin login is brute-forced by one agent, not a roomful: a burst of 20,
// then one try every 10 s.
fn login_limit() -> GovernorLayer<PeerIp, governor::middleware::NoOpMiddleware> {
    keyed_limit(Duration::from_secs(10), 20)
}

pub fn build_router(state: AppState) -> Router {
    // Brute-forceable or spammable endpoints: login and join (see the limit
    // functions). /rate has no IP limit: it needs a player token and stores one
    // rating per player per prompt, so a spammer gains nothing, while a per-IP
    // bucket would block a whole event sharing one IP.
    let login_limited = Router::new()
        .route("/api/admin/login", post(rest::admin_login))
        .route_layer(login_limit());
    let join_limited = Router::new()
        .route("/api/rooms/:code/join", get(rest::join_get).post(rest::join))
        .route_layer(join_limit());
    let rest = Router::new()
        .route("/healthz", get(healthz))
        .route("/api/admin/stats", get(rest::admin_stats))
        .route(
            "/api/admin/rooms",
            get(rest::admin_list).post(rest::admin_create),
        )
        .route("/api/admin/rooms/:code/start", post(rest::admin_start))
        .route("/api/admin/rooms/:code/settings", post(rest::admin_settings))
        .route("/api/admin/rooms/:code/players/:name", delete(rest::admin_remove_player))
        .route("/api/admin/rooms/:code/next", post(rest::admin_next))
        .route("/api/admin/rooms/:code/bonus", post(rest::admin_bonus))
        .route("/api/admin/rooms/:code/pass", post(rest::admin_pass))
        .route("/api/admin/rooms/:code/clock", post(rest::admin_clock))
        .route("/api/admin/rooms/:code/end", post(rest::admin_end))
        .route("/api/admin/rooms/:code/skip", post(rest::admin_skip))
        .route("/api/admin/rooms/:code", delete(rest::admin_delete))
        .route("/api/admin/rooms/:code/restart", post(rest::admin_restart))
        .route("/api/rooms/:code/rate", post(rest::rate))
        .route("/api/rooms/:code/ready", post(rest::ready))
        .route("/api/images/:id", get(rest::image_get))
        .route(
            "/api/rooms/:code/quiz",
            get(rest::quiz_get).post(rest::quiz_post),
        )
        .merge(login_limited)
        .merge(join_limited)
        .route_layer(middleware::from_fn_with_state(rest_slots(), limit_concurrency))
        .route_layer(middleware::from_fn(rest_timeout))
        .layer(axum::extract::DefaultBodyLimit::max(MAX_BODY_BYTES))
        .with_state(state.clone());
    Router::new()
        .route("/api/rooms/:code/ws", get(ws_handler))
        .with_state(state)
        .merge(rest)
        // SPA static last: /app/dist files at /, index.html fallback.
        // Axum matches /api/*, WS, /healthz first; only unmatched GET falls here.
        .fallback_service(
            ServeDir::new("/app/dist").fallback(ServeFile::new("/app/dist/index.html")),
        )
        .layer(middleware::from_fn(log_request))
}

#[cfg(test)]
pub async fn test_router() -> Router {
    use sqlx::sqlite::SqlitePoolOptions;
    let pool = SqlitePoolOptions::new()
        .max_connections(1)
        .connect("sqlite::memory:")
        .await
        .expect("test pool");
    build_router(AppState::new(pool))
}
