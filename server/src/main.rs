use server::db::connect_pool;
use server::router::{build_router, AppState};

#[tokio::main]
async fn main() {
    // Local runs read server/.env like compose's env_file does; already-set
    // vars win, so a real environment can always override the file.
    let _ = dotenvy::dotenv();
    server::init_logging();
    // A panic in any task is a bug: log it at ERROR with its location so it
    // reaches the same sink as every other line, not only raw stderr.
    std::panic::set_hook(Box::new(|info| {
        let location = info.location().map(|l| l.to_string()).unwrap_or_default();
        tracing::error!(%location, "panic: {info}");
    }));

    let port = std::env::args()
        .nth(2)
        .filter(|_| std::env::args().nth(1).as_deref() == Some("--port"))
        .and_then(|p| p.parse::<u16>().ok())
        .unwrap_or(3001);
    tracing::info!(version = env!("CARGO_PKG_VERSION"), port, "server starting");

    // Shared SQLite file during the strangler: WAL lets Django read while Axum writes.
    let url = std::env::var("DATABASE_URL").unwrap_or_else(|_| "sqlite:server.sqlite3?mode=rwc".into());
    // The path only: the query string carries open flags, never needed in a log.
    let db = url.split('?').next().unwrap_or_default().to_string();
    let pool = match connect_pool(&url).await {
        Ok(pool) => pool,
        Err(e) => {
            tracing::error!(%db, error = %e, "database unavailable, exiting");
            std::process::exit(1);
        }
    };
    tracing::info!(%db, "database ready");
    let state = AppState::new(pool);
    server::game::spawn_ticker(state.clone());
    server::game::spawn_sweeper(state.clone());
    tracing::info!("background ticker and room sweeper started");

    let app = build_router(state);
    let addr = format!("0.0.0.0:{port}");
    let listener = match tokio::net::TcpListener::bind(&addr).await {
        Ok(l) => l,
        Err(e) => {
            tracing::error!(%addr, error = %e, "cannot bind, exiting");
            std::process::exit(1);
        }
    };
    tracing::info!(%addr, "listening");
    if let Err(e) = axum::serve(listener, app).with_graceful_shutdown(shutdown_signal()).await {
        tracing::error!(error = %e, "server stopped on error");
        std::process::exit(1);
    }
    tracing::info!("server stopped");
}

// SIGTERM is how k8s and docker stop the pod; Ctrl+C is the local run.
async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let term = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut s) => {
                s.recv().await;
            }
            Err(e) => {
                tracing::warn!(error = %e, "cannot listen for SIGTERM");
                std::future::pending::<()>().await;
            }
        }
    };
    #[cfg(not(unix))]
    let term = std::future::pending::<()>();
    tokio::select! {
        _ = ctrl_c => tracing::info!("Ctrl+C received, shutting down"),
        _ = term => tracing::info!("SIGTERM received, shutting down"),
    }
}
