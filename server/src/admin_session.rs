use std::sync::OnceLock;

use axum::http::HeaderMap;
use hmac::{Hmac, KeyInit, Mac};
use sha2::Sha256;

pub const COOKIE_NAME: &str = "genai_admin";
// Signed expiry, not an identity: Django's session holds one boolean
// (`request.session["admin"]`), so parity needs proof of the password.
const SESSION_SECS: u64 = 12 * 60 * 60;

/// Release builds must not fall back to a known default: refuse to start.
pub fn check_env() -> Result<(), String> {
    if !cfg!(debug_assertions)
        && std::env::var("ADMIN_PASSWORD").map_or(true, |v| v.is_empty())
    {
        return Err("ADMIN_PASSWORD must be set in release builds".into());
    }
    if !cfg!(debug_assertions) && std::env::var("ADMIN_SECRET").map_or(true, |v| v.is_empty()) {
        return Err("ADMIN_SECRET must be set in release builds".into());
    }
    Ok(())
}

fn secret() -> &'static str {
    // Release builds require ADMIN_SECRET (check_env refuses to start
    // without it) so every replica shares one key. Debug builds fall back to
    // an ephemeral per-process key: a restart signs every admin out.
    static SECRET: OnceLock<String> = OnceLock::new();
    SECRET.get_or_init(|| {
        std::env::var("ADMIN_SECRET").unwrap_or_else(|_| {
            assert!(cfg!(debug_assertions), "ADMIN_SECRET missing in release build");
            let mut buf = [0u8; 32];
            getrandom::fill(&mut buf).expect("OS RNG unavailable");
            hex(&buf)
        })
    })
}

pub(crate) fn hex(bytes: &[u8]) -> String {
    const H: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(H[(b >> 4) as usize] as char);
        s.push(H[(b & 15) as usize] as char);
    }
    s
}

fn unhex(s: &str) -> Option<Vec<u8>> {
    if s.len() % 2 != 0 {
        return None;
    }
    let mut out = Vec::with_capacity(s.len() / 2);
    let b = s.as_bytes();
    let val = |c: u8| match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    };
    let mut i = 0;
    while i < b.len() {
        out.push(val(b[i])? << 4 | val(b[i + 1])?);
        i += 2;
    }
    Some(out)
}

fn mac(expiry: u64) -> Hmac<Sha256> {
    let mut mac = Hmac::<Sha256>::new_from_slice(secret().as_bytes())
        .expect("hmac accepts any key length");
    mac.update(format!("genai-admin-v1:{expiry}").as_bytes());
    mac
}

fn now_unix() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}

// Cookie value: `<expiry_unix>.<hex hmac>`.
fn issue_value_with_expiry(expiry: u64) -> String {
    format!("{expiry}.{}", hex(&mac(expiry).finalize().into_bytes()))
}

pub fn issue_value() -> String {
    issue_value_with_expiry(now_unix() + SESSION_SECS)
}

fn valid_value_at(v: &str, now: u64) -> bool {
    let Some((exp, sig)) = v.trim().split_once('.') else {
        return false;
    };
    let (Ok(expiry), Some(sig)) = (exp.parse::<u64>(), unhex(sig)) else {
        return false;
    };
    // verify_slice compares in constant time.
    mac(expiry).verify_slice(&sig).is_ok() && expiry > now
}

pub fn valid_value(v: &str) -> bool {
    valid_value_at(v, now_unix())
}

/// Constant-time equality for secrets; a length mismatch is simply false.
pub fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    use subtle::ConstantTimeEq;
    a.ct_eq(b).into()
}

fn cookie_header(headers: &HeaderMap) -> &str {
    headers
        .get(axum::http::header::COOKIE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
}

// Browsers attach cookies to the WS upgrade on their own, so the admin
// console subscribes with no token in the URL: the session rides the
// Cookie header instead of a query parameter that proxies would log.
pub fn request_is_admin(headers: &HeaderMap) -> bool {
    for part in cookie_header(headers).split(';') {
        let mut kv = part.splitn(2, '=');
        if kv.next().map(str::trim) == Some(COOKIE_NAME) {
            if let Some(v) = kv.next() {
                if valid_value(v) {
                    return true;
                }
            }
        }
    }
    false
}

// HttpOnly so page JS never sees it; SameSite=Lax so top-level
// navigation keeps the console signed in without opening CSRF on POSTs
// (state-changing admin routes still need the password-born session).
pub fn set_cookie_header() -> String {
    // Secure in release only: debug runs over plain http on localhost.
    let secure = if cfg!(debug_assertions) { "" } else { "; Secure" };
    format!(
        "{COOKIE_NAME}={}; Path=/; Max-Age={SESSION_SECS}; HttpOnly; SameSite=Lax{secure}",
        issue_value()
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip_and_tamper() {
        let v = issue_value();
        assert!(valid_value(&v));
        assert!(!valid_value("deadbeef"));
        let mut bad = v.clone();
        bad.push('0');
        assert!(!valid_value(&bad));
    }

    #[test]
    fn expiry_is_signed() {
        let v = issue_value_with_expiry(1_000);
        assert!(valid_value_at(&v, 999));
        // expired
        assert!(!valid_value_at(&v, 1_000));
        assert!(!valid_value_at(&v, 5_000));
        // tampered expiry keeps the old signature
        let sig = v.split_once('.').unwrap().1;
        assert!(!valid_value_at(&format!("9999999999.{sig}"), 999));
        assert!(!valid_value_at("garbage", 0));
    }
}
