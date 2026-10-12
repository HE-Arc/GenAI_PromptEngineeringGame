use axum::http::HeaderMap;
use sqlx::SqlitePool;

use crate::admin_session;
use crate::db::rooms::{self, Player};

pub enum Auth {
    Player(Player),
    Admin,
}

pub enum AuthError {
    UnknownRoom,
    BadToken,
}

// Same accept/reject shape as Django: unknown room 404, wrong token 403.
pub async fn validate(pool: &SqlitePool, code: &str, token: &str) -> Result<Auth, AuthError> {    let room = rooms::get_room(pool, code).await.map_err(|e| match e {
        sqlx::Error::RowNotFound => AuthError::UnknownRoom,
        _ => AuthError::UnknownRoom,
    })?;
    if token == room.admin_key {
        return Ok(Auth::Admin);
    }
    match rooms::player_by_token(pool, token).await {
        Ok(Some(p)) if p.room_code == code => Ok(Auth::Player(p)),
        _ => Err(AuthError::BadToken),
    }
}

// WS admission: the query token first, then the admin session cookie.
// The cookie branch exists because browsers attach cookies to the WS
// upgrade automatically while the console never holds an admin_key.
pub async fn validate_ws(
    pool: &SqlitePool,
    code: &str,
    token: &str,
    headers: &HeaderMap,
) -> Result<Auth, AuthError> {
    match validate(pool, code, token).await {
        Ok(a) => Ok(a),
        Err(AuthError::UnknownRoom) => Err(AuthError::UnknownRoom),
        Err(AuthError::BadToken) => {
            if admin_session::request_is_admin(headers) {
                rooms::get_room(pool, code).await.map_err(|_| AuthError::UnknownRoom)?;
                Ok(Auth::Admin)
            } else {
                Err(AuthError::BadToken)
            }
        }
    }
}
