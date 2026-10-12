use sqlx::SqlitePool;

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
pub async fn validate(pool: &SqlitePool, code: &str, token: &str) -> Result<Auth, AuthError> {
    let room = rooms::get_room(pool, code).await.map_err(|e| match e {
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
