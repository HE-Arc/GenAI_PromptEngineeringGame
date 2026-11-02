pub mod openai;
pub mod openrouter;
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

// One exchange of a conversation round (answer.turns).
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct Turn {
    pub prompt: String,
    pub output: String,
}

// NULL, empty or unreadable all mean "no earlier exchange": a broken column
// must restart the conversation, never block the player.
pub fn parse_turns(raw: Option<&str>) -> Vec<Turn> {
    raw.and_then(|s| serde_json::from_str(s).ok()).unwrap_or_default()
}

// Play input of a conversation round: the exercise, then every earlier
// exchange, then the new prompt, so the model answers in context.
pub fn conversation_input(manche: i64, target: &str, history: &[Turn], prompt: &str) -> String {
    let mut out = format!("Exercice/manche {} : {}\n", manche, target.trim());
    if !history.is_empty() {
        out.push_str("\nConversation précédente avec l'élève (réponds en tenant compte de ces échanges) :\n");
        for (i, t) in history.iter().enumerate() {
            out.push_str(&format!(
                "\n--- Prompt {n} de l'élève ---\n{p}\n--- Ta réponse {n} ---\n{o}\n",
                n = i + 1,
                p = t.prompt.trim(),
                o = t.output.trim()
            ));
        }
    }
    out.push_str(&format!(
        "\nNouveau prompt élève (prompt {}) : {}",
        history.len() + 1,
        prompt.trim()
    ));
    out
}

// The chain as one text, for the critic, the judge and the rating screen:
// the class rates the whole sequence of prompts, not its last step.
pub fn chain_prompt(turns: &[Turn]) -> String {
    turns
        .iter()
        .enumerate()
        .map(|(i, t)| format!("Prompt {} :\n{}", i + 1, t.prompt.trim()))
        .collect::<Vec<_>>()
        .join("\n\n")
}

// Markdown, since every screen renders answers through ModelOutput.
pub fn chain_output(turns: &[Turn]) -> String {
    turns
        .iter()
        .enumerate()
        .map(|(i, t)| format!("**Réponse au prompt {}**\n\n{}", i + 1, t.output.trim()))
        .collect::<Vec<_>>()
        .join("\n\n---\n\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn conversation_input_carries_history_in_order() {
        let history = vec![
            Turn { prompt: "voici les notions".into(), output: "compris".into() },
            Turn { prompt: "fais une synthèse".into(), output: "synthèse".into() },
        ];
        let out = conversation_input(7, "prépare un examen", &history, "teste-moi");
        let a = out.find("voici les notions").unwrap();
        let b = out.find("synthèse").unwrap();
        let c = out.find("Nouveau prompt élève (prompt 3) : teste-moi").unwrap();
        assert!(a < b && b < c);
        assert!(out.contains("prépare un examen"));
        assert_eq!(chain_prompt(&history), "Prompt 1 :\nvoici les notions\n\nPrompt 2 :\nfais une synthèse");
    }

    #[test]
    fn unreadable_turns_restart_the_conversation() {
        assert!(parse_turns(None).is_empty());
        assert!(parse_turns(Some("not json")).is_empty());
        assert_eq!(parse_turns(Some(r#"[{"prompt":"p","output":"o"}]"#)).len(), 1);
    }

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

// One picture from the image model, plus any words it sent alongside.
#[derive(Debug, Clone)]
pub struct GeneratedImage {
    pub mime: String,
    pub data: Vec<u8>,
    pub text: String,
}
pub type ImageResult = Result<GeneratedImage, String>;

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

    // Image rounds: the player's prompt alone goes to the image model. It must
    // not see the reference picture or the exercise, or a vague prompt would
    // still get the right drawing. Default: unavailable, so a backend without
    // an image model fails the answer visibly.
    fn generate_image(&self, _prompt: &str) -> Pin<Box<dyn Future<Output = ImageResult> + Send + '_>> {
        Box::pin(async { Err("image generation not available".into()) })
    }
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
