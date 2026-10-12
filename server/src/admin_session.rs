use std::sync::OnceLock;

use axum::http::HeaderMap;
use hmac::{Hmac, KeyInit, Mac};
use sha2::Sha256;

pub const COOKIE_NAME: &str = "genai_admin";
// Fixed signed value, not a bearer secret: Django's session holds one
// boolean (`request.session["admin"]`), so parity needs proof of the
// password, not an identity. Stealing it equals stealing Django's cookie.
const SIGNED_BODY: &[u8] = b"genai-admin-v1";

fn secret() -> &'static str {
    // Ephemeral per-process key, generated once on first use: a restart
    // signs every admin out, which is what a classroom session wants, and
    // nothing to misconfigure or leak beats a committed default. ADMIN_SECRET
    // remains as an explicit override for multi-process deployments, where
    // every replica must share one key.
    static SECRET: OnceLock<String> = OnceLock::new();
    SECRET.get_or_init(|| {
        std::env::var("ADMIN_SECRET").unwrap_or_else(|_| {
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

fn tag() -> Vec<u8> {
    let mut mac = Hmac::<Sha256>::new_from_slice(secret().as_bytes())
        .expect("hmac accepts any key length");
    mac.update(SIGNED_BODY);
    mac.finalize().into_bytes().to_vec()
}

pub fn issue_value() -> String {
    hex(&tag())
}

pub fn valid_value(v: &str) -> bool {
    // Decoded first so a length probe learns nothing; compared in
    // constant time so a timing probe learns nothing either.
    match unhex(v.trim()) {
        Some(raw) => {
            let expected = tag();
            raw.len() == expected.len()
                && constant_time_eq(&raw, &expected)
        }
        None => false,
    }
}

fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    let mut diff = 0u8;
    for i in 0..a.len() {
        diff |= a[i] ^ b[i];
    }
    diff == 0
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
    format!(
        "{COOKIE_NAME}={}; Path=/; HttpOnly; SameSite=Lax",
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
}
