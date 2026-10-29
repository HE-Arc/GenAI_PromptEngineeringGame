pub mod openai;
pub mod stub;

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use tokio::sync::mpsc;

pub use stub::StubSource;

// Shown when a round has no exercice text: never a silent empty fallback.
// Player-facing, so French.
pub const MISSING_EXERCISE_NOTICE: &str =
    "Aucun exercice pour cette manche : impossible d'interroger le modèle.";

// The play call never receives the raw player prompt alone: the stored
// target IS the exercice (round.target, see data/targets.json), so the model
// always gets exercice + consigne + prompt eleve together and never answers
// "aucun exercice precise".
pub fn enriched_input(manche: i64, target: &str, prompt: &str) -> String {
    format!(
        "Exercice/manche {} : {}\nConsigne : {}\nRéponse attendue / cible : {}\nPrompt élève : {}",
        manche,
        target.trim(),
        target.trim(),
        target.trim(),
        prompt.trim()
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn enriched_input_never_raw_prompt_alone() {
        let out = enriched_input(3, "resume ce mail", "fais court");
        assert!(out.contains("Exercice/manche 3"));
        assert!(out.contains("resume ce mail"));
        assert!(out.contains("Prompt élève : fais court"));
        assert_ne!(out, "fais court");
    }
}

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

    // `criteria`: the exercise's grading criteria (data/targets.json), the
    // same list the player sees beside the note.
    fn score(
        &self,
        prompt: &str,
        output: &str,
        target: &str,
        criteria: &[String],
    ) -> Pin<Box<dyn Future<Output = Verdict> + Send + '_>>;

    // The critic: rates the prompt's METHOD against the exercise criteria,
    // never its output. Anchors the ±3 band the peer mean is held to.
    fn grade_prompt(
        &self,
        prompt: &str,
        target: &str,
        criteria: &[String],
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
        false => {
            tracing::warn!("no LLM API key: using the local stub, scores are not real verdicts");
            Arc::new(StubSource)
        }
    }
}
