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
pub trait LlmSource: Send + Sync + 'static {
    fn stream_into(
        &self,
        prompt: String,
        tx: mpsc::UnboundedSender<String>,
    ) -> Pin<Box<dyn Future<Output = ()> + Send + '_>>;

    fn score(
        &self,
        prompt: &str,
        output: &str,
        target: &str,
    ) -> Pin<Box<dyn Future<Output = Verdict> + Send + '_>>;
}

// OPENAI_API_KEY set => billed streaming client, else the deterministic stub.
// Tests never set the key, so they always exercise the stub path.
pub fn from_env() -> Arc<dyn LlmSource> {
    match std::env::var("OPENAI_API_KEY")
        .map(|k| !k.trim().is_empty())
        .unwrap_or(false)
    {
        true => Arc::new(openai::OpenAiSource::from_env()),
        false => Arc::new(StubSource),
    }
}
