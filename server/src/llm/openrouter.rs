// Image generation for the rounds whose exercise is a picture (data/targets.json
// entries with an `image`). OpenRouter speaks the chat-completions protocol and
// returns generated pictures as base64 data URLs under `message.images`.
use base64::Engine;
use serde_json::Value;

use super::{GeneratedImage, ImageResult};

const DEFAULT_BASE: &str = "https://openrouter.ai/api/v1";
const DEFAULT_MODEL: &str = "meta/muse-image";
// An image takes far longer than a first text token: its own client, so the
// shared 30s text timeout stays tight.
const IMAGE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(120);
// Generous for one PNG; caps what one answer can push into SQLite.
const MAX_IMAGE_BYTES: usize = 8 * 1024 * 1024;
// Raster formats only: a provider-supplied SVG served from our origin could
// carry script.
const ALLOWED_MIME: &[&str] = &["image/png", "image/jpeg", "image/webp"];

pub struct ImageGen {
    http: reqwest::Client,
    url: String,
    key: String,
    model: String,
}

impl ImageGen {
    // None without OPENROUTER_API_KEY: image rounds then fail loudly per answer
    // instead of the whole server refusing to start.
    pub fn from_env() -> Option<Self> {
        let key = std::env::var("OPENROUTER_API_KEY").ok().filter(|k| !k.trim().is_empty())?;
        let base = std::env::var("OPENROUTER_BASE_URL")
            .ok()
            .filter(|b| !b.trim().is_empty())
            .unwrap_or_else(|| DEFAULT_BASE.into());
        let model = std::env::var("IMAGE_MODEL")
            .ok()
            .filter(|m| !m.trim().is_empty())
            .unwrap_or_else(|| DEFAULT_MODEL.into());
        tracing::info!(base_url = %base, image_model = %model, "image backend configured");
        let http = reqwest::Client::builder()
            .user_agent("genai-game/1.0")
            .timeout(IMAGE_TIMEOUT)
            .build()
            .unwrap_or_else(|_| reqwest::Client::new());
        Some(Self {
            http,
            url: format!("{}/chat/completions", base.trim_end_matches('/')),
            key: key.trim().to_string(),
            model,
        })
    }

    pub async fn generate(&self, prompt: &str) -> ImageResult {
        let body = serde_json::json!({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            // Image only: an image-only model (muse-image) refuses a request
            // that also asks for text, and a model that can do both still
            // returns the picture.
            "modalities": ["image"],
        });
        let response = self
            .http
            .post(&self.url)
            .bearer_auth(&self.key)
            .header("X-Title", "GenAI Prompt Engineering Game")
            .json(&body)
            .send()
            .await
            .map_err(|e| format!("image call failed: {e}"))?;
        let status = response.status();
        if !status.is_success() {
            // The body names the cause (bad model id, no credits); it never
            // echoes the key.
            let detail = response.text().await.unwrap_or_default();
            return Err(format!("image call failed: HTTP {status}: {}", detail.chars().take(300).collect::<String>()));
        }
        let value: Value = response.json().await.map_err(|e| format!("image call unreadable: {e}"))?;
        parse_image_response(&value)
    }
}

fn parse_image_response(value: &Value) -> ImageResult {
    let message = value
        .pointer("/choices/0/message")
        .ok_or_else(|| "image call returned no message".to_string())?;
    let text = match message.get("content") {
        Some(Value::String(s)) => s.trim().to_string(),
        _ => String::new(),
    };
    let url = message
        .get("images")
        .and_then(Value::as_array)
        .and_then(|images| images.iter().find_map(|i| i.pointer("/image_url/url").and_then(Value::as_str)))
        // The model sometimes answers in words only (refusal, a question back).
        .ok_or_else(|| format!("image call returned no image{}", if text.is_empty() { String::new() } else { format!(": {}", text.chars().take(200).collect::<String>()) }))?;
    let (mime, data) = decode_data_url(url)?;
    Ok(GeneratedImage { mime, data, text })
}

// `data:image/png;base64,<payload>` into its type and bytes.
fn decode_data_url(url: &str) -> Result<(String, Vec<u8>), String> {
    let rest = url.strip_prefix("data:").ok_or("image is not a data URL")?;
    let (meta, payload) = rest.split_once(',').ok_or("malformed data URL")?;
    let mime = meta.strip_suffix(";base64").ok_or("data URL is not base64")?.to_ascii_lowercase();
    if !ALLOWED_MIME.contains(&mime.as_str()) {
        return Err(format!("unsupported image type {mime}"));
    }
    // 4 base64 chars per 3 bytes: refuse before decoding an oversized payload.
    if payload.len() / 4 * 3 > MAX_IMAGE_BYTES {
        return Err("image too large".into());
    }
    let data = base64::engine::general_purpose::STANDARD
        .decode(payload.trim())
        .map_err(|e| format!("image base64 invalid: {e}"))?;
    Ok((mime, data))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_image_and_text_from_openrouter_shape() {
        let v = serde_json::json!({"choices": [{"message": {
            "content": "Voici le trapèze.",
            "images": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}]
        }}]});
        let img = parse_image_response(&v).unwrap();
        assert_eq!(img.mime, "image/png");
        assert_eq!(&img.data[..4], b"\x89PNG");
        assert_eq!(img.text, "Voici le trapèze.");
    }

    #[test]
    fn text_only_reply_is_an_error_naming_the_text() {
        let v = serde_json::json!({"choices": [{"message": {"content": "Je ne peux pas."}}]});
        assert!(parse_image_response(&v).unwrap_err().contains("Je ne peux pas."));
    }

    #[test]
    fn refuses_svg_and_non_data_urls() {
        assert!(decode_data_url("data:image/svg+xml;base64,PHN2Zy8+").is_err());
        assert!(decode_data_url("https://example.com/a.png").is_err());
    }
}
