use server::db::connect_pool;
use server::router::{build_router, AppState};

#[tokio::main]
async fn main() {
    // Local runs read server/.env like compose's env_file does; already-set
    // vars win, so a real environment can always override the file.
    let _ = dotenvy::dotenv();    let port = std::env::args()
        .nth(2)
        .filter(|_| std::env::args().nth(1).as_deref() == Some("--port"))
        .and_then(|p| p.parse::<u16>().ok())
        .unwrap_or(3001);

    // Shared SQLite file during the strangler: WAL lets Django read while Axum writes.
    let url = std::env::var("DATABASE_URL").unwrap_or_else(|_| "sqlite:server.sqlite3?mode=rwc".into());
    let pool = connect_pool(&url).await.expect("sqlite pool");
    let state = AppState::new(pool);
    server::game::spawn_ticker(state.clone());
    server::game::spawn_sweeper(state.clone());

    let app = build_router(state);
    let addr = format!("0.0.0.0:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await.expect("bind");
    println!("listening on {addr}");
    axum::serve(listener, app).await.expect("serve");
}
