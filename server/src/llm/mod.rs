pub mod openai;
pub mod stub;

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use tokio::sync::mpsc;

pub use stub::StubSource;

#[derive(Debug, Clone)]
pub struct Verdict {
    pub score: i64,
    pub feedback: String,
}

// Boxed futures keep the trait dyn-compatible so AppState holds one
// Arc<dyn LlmSource> regardless of which backend the factory picks.
// Ok(truncated): the stream ended, `true` when the token cap cut it short.
// Err: no answer could be produced at all; the caller records a failure.
pub type StreamResult = Result<bool, String>;

pub trait LlmSource: Send + Sync + 'static {
    fn stream_into(
        &self,
        prompt: String,
        tx: mpsc::UnboundedSender<String>,
    ) -> Pin<Box<dyn Future<Output = StreamResult> + Send + '_>>;

    fn score(
        &self,
        prompt: &str,
        output: &str,
        target: &str,
    ) -> Pin<Box<dyn Future<Output = Verdict> + Send + '_>>;

    // The critic: rates the prompt's METHOD (role, context, format, length),
    // never its output. Anchors the ±3 band the peer mean is held to.
    fn grade_prompt(
        &self,
        prompt: &str,
        target: &str,
    ) -> Pin<Box<dyn Future<Output = Verdict> + Send + '_>>;
}

// A key from either contract (plain OPENAI_API_KEY or the endpoint pair
// LLM_BASE_URL + LLM_API_KEY) enables the billed streaming client; otherwise
// the deterministic stub. Tests never set a key, so they always hit the stub.
pub fn from_env() -> Arc<dyn LlmSource> {
    let has_key = |name: &str| {
        std::env::var(name)
            .map(|k| !k.trim().is_empty())
            .unwrap_or(false)
    };
    match has_key("OPENAI_API_KEY") || has_key("LLM_API_KEY") {
        true => Arc::new(openai::OpenAiSource::from_env()),
        false => Arc::new(StubSource),
    }
}
