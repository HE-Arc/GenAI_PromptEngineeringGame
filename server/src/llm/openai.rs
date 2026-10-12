use std::sync::OnceLock;

use async_openai::config::Config;
use futures_util::StreamExt;
use reqwest::header::{HeaderMap, HeaderName, HeaderValue, USER_AGENT};
use serde_json::Value;
use tokio::sync::mpsc;

use super::Verdict;

pub struct OpenAiSource {
    client: async_openai::Client<async_openai::config::OpenAIConfig>,
    play_model: String,
    judge_model: String,
}

// OpenCode Go rejects header-less requests with 400 MissingSessionID: the
// header pins a conversation to one backend so its prompt cache stays warm.
// Every play/judge call here is its own one-turn conversation with a unique
// classroom prompt, so nothing caches across requests and one stable
// per-process id costs us nothing while keeping a single pooled reqwest
// client (per-request clients would kill connection reuse). OPENCODE_SESSION_ID
// overrides for deployments that want the same id across replicas.
fn session_id() -> &'static str {
    static SID: OnceLock<String> = OnceLock::new();
    SID.get_or_init(|| {
        std::env::var("OPENCODE_SESSION_ID").unwrap_or_else(|_| {
            let mut buf = [0u8; 16];
            getrandom::fill(&mut buf).expect("OS RNG unavailable");
            format!("ses_{}", crate::admin_session::hex(&buf))
        })
    })
}

fn http_client() -> reqwest::Client {
    let mut headers = HeaderMap::new();
    // The Go docs ask clients to name themselves rather than ride a
    // generic SDK user agent.
    headers.insert(USER_AGENT, HeaderValue::from_static("genai-game/1.0"));
    headers.insert(
        HeaderName::from_static("x-opencode-session"),
        HeaderValue::from_str(session_id()).expect("hex is a valid header value"),
    );
    reqwest::Client::builder()
        .default_headers(headers)
        .build()
        .expect("http client")
}

// The SDK's reqwest handle is private, so /responses rides its own pooled
// client carrying the same default headers; a fresh client per request
// would kill connection reuse, the same rule the SDK client follows.
fn pooled_http() -> &'static reqwest::Client {
    static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    CLIENT.get_or_init(http_client)
}

impl OpenAiSource {
    pub fn from_env() -> Self {
        let mut config = async_openai::config::OpenAIConfig::default();
        // Any OpenAI-compatible endpoint: LLM_BASE_URL points at it and
        // LLM_API_KEY carries its key, mirroring the Django-era contract.
        if let Ok(base) = std::env::var("LLM_BASE_URL") {
            if !base.trim().is_empty() {
                config = config.with_api_base(base);
            }
        }
        if let Ok(key) = std::env::var("LLM_API_KEY") {
            if !key.trim().is_empty() {
                config = config.with_api_key(key);
            }
        }
        let play_model = std::env::var("LLM_MODEL")
            .or_else(|_| std::env::var("OPENAI_PLAY_MODEL"))
            .unwrap_or_else(|_| "gpt-4o-mini".into());
        // Separate model on purpose: grading sits on the critical path of
        // every round and is the call you move to a bigger model when a
        // group's scores look wrong, without touching the play stream.
        let judge_model =
            std::env::var("JUDGE_MODEL").unwrap_or_else(|_| play_model.clone());
        Self {
            client: async_openai::Client::with_config(config).with_http_client(http_client()),
            play_model,
            judge_model,
        }
    }
}

// async-openai 0.26 predates the Responses API, so /responses is spoken by
// hand: base URL and Authorization come from the SDK config (the key never
// leaves it), the session/UA headers from the shared pooled client.
async fn post_responses(
    config: &async_openai::config::OpenAIConfig,
    body: serde_json::Value,
) -> Result<reqwest::Response, reqwest::Error> {
    pooled_http()
        .post(config.url("/responses"))
        .headers(config.headers())
        .json(&body)
        .send()
        .await
}

// Concatenated assistant text from a /responses payload. Tolerates both
// shapes: the streaming `response.completed` event nests output under
// "response", a plain body carries it at the top level. Missing content or
// unknown part types contribute nothing rather than panic.
fn response_output_text(value: &Value) -> String {
    let Some(output) = value
        .get("response")
        .and_then(|r| r.get("output"))
        .or_else(|| value.get("output"))
        .and_then(Value::as_array)
    else {
        return String::new();
    };
    let mut text = String::new();
    for item in output {
        if item.get("type").and_then(Value::as_str) != Some("message") {
            continue;
        }
        match item.get("content") {
            Some(Value::String(s)) => text.push_str(s),
            Some(Value::Array(parts)) => {
                for part in parts {
                    if part.get("type").and_then(Value::as_str) == Some("output_text") {
                        if let Some(s) = part.get("text").and_then(Value::as_str) {
                            text.push_str(s);
                        }
                    }
                }
            }
            _ => {}
        }
    }
    text
}

impl super::LlmSource for OpenAiSource {
    // Deltas forward as they arrive; a mid-stream API error just ends the
    // stream and whatever arrived is still scored and persisted.
    fn stream_into(
        &self,
        prompt: String,
        tx: mpsc::UnboundedSender<String>,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + '_>> {
        // Reasoning effort low: muse-spark defaults to high-effort thinking
        // that delays the first visible chunk and burns the whole budget in
        // reasoning tokens — a classroom wants speed, not a wall of thought.
        // Caps mirror the Django-era limits (PLAY 1600).
        let body = serde_json::json!({
            "model": self.play_model,
            "input": prompt,
            "stream": true,
            "max_output_tokens": 1600,
            "reasoning": { "effort": "low" },
        });
        let config = self.client.config();
        Box::pin(async move {
            let Ok(response) = post_responses(config, body).await else {
                return;
            };
            if !response.status().is_success() {
                return;
            }
            let mut stream = response.bytes_stream();
            // SSE frames are `data: {json}` lines; a frame may span chunk
            // boundaries, so lines are assembled from a byte buffer.
            let mut carry: Vec<u8> = Vec::new();
            let mut raw: Vec<u8> = Vec::new();
            let mut saw_delta = false;
            while let Some(chunk) = stream.next().await {
                let Ok(chunk) = chunk else { return };
                raw.extend_from_slice(&chunk);
                carry.extend_from_slice(&chunk);
                while let Some(pos) = carry.iter().position(|&b| b == b'\n') {
                    let line = String::from_utf8_lossy(&carry[..pos])
                        .trim_end_matches('\r')
                        .to_string();
                    carry.drain(..=pos);
                    let Some(data) = line.strip_prefix("data:").map(str::trim_start) else {
                        continue;
                    };
                    if data == "[DONE]" {
                        return;
                    }
                    let Ok(event) = serde_json::from_str::<Value>(data) else {
                        continue;
                    };
                    match event.get("type").and_then(Value::as_str) {
                        // Canonical streaming event: one text fragment.
                        Some("response.output_text.delta") => {
                            if let Some(delta) = event.get("delta").and_then(Value::as_str) {
                                if !delta.is_empty() {
                                    saw_delta = true;
                                    if tx.send(delta.to_owned()).is_err() {
                                        return;
                                    }
                                }
                            }
                        }
                        // Final substantive event: a server that never
                        // emitted deltas left the whole text here.
                        Some("response.completed") => {
                            if !saw_delta {
                                let text = response_output_text(&event);
                                if !text.is_empty() && tx.send(text).is_err() {
                                    return;
                                }
                            }
                            return;
                        }
                        _ => {}
                    }
                }
            }
            // A server that ignored `stream` answers with one JSON body;
            // forward it whole rather than drop the answer.
            if !saw_delta && !raw.is_empty() {
                if let Ok(value) = serde_json::from_slice::<Value>(&raw) {
                    let text = response_output_text(&value);
                    if !text.is_empty() {
                        let _ = tx.send(text);
                    }
                }
            }
        })
    }

    // Second non-streaming call: grading must see the whole output, so it
    // cannot reuse the play stream. `stream` omitted on purpose: a single
    // JSON body is easier to grade than reassembled frames.
    fn score(
        &self,
        prompt: &str,
        output: &str,
        target: &str,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Verdict> + Send + '_>> {
        let grading = format!("prompt: {prompt}\nreponse_obtenue: {output}\ncible: {target}");
        // Same reasoning/cap discipline as the play call (JUDGE 1200).
        let body = serde_json::json!({
            "model": self.judge_model,
            "input": grading,
            "max_output_tokens": 1200,
            "reasoning": { "effort": "low" },
        });
        let config = self.client.config();
        Box::pin(async move {
            let text = match post_responses(config, body).await {
                Ok(r) if r.status().is_success() => r
                    .json::<Value>()
                    .await
                    .map(|v| response_output_text(&v))
                    .unwrap_or_default(),
                _ => String::new(),
            };
            let score = text
                .split(|c: char| !c.is_numeric())
                .filter_map(|w| w.parse::<i64>().ok())
                .next()
                .unwrap_or(0)
                .clamp(0, 10);
            Verdict {
                score,
                feedback: text,
            }
        })
    }
}
