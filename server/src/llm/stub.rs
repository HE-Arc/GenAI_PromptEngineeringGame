use tokio::sync::mpsc;

use super::Verdict;

// Quoted player-facing string, so French is correct here (comments stay English).
const STUB_COMMENT: &str = "Local, pas de cle API : regle locale, pas un vrai jury. Avec une cible longue, les scores sont bas par construction : c'est la methode, pas ton prompt.";

pub struct StubSource;

fn stub_answer(prompt: &str) -> String {
    format!("(reponse locale) {prompt}")
}

// Word-grouped chunks mirroring game/llm.py _stub_chunks: the separator goes
// BETWEEN chunks so concat(chunks) == stub_answer exactly, which is what the
// view strips and stores.
pub fn stub_chunks(prompt: &str) -> Vec<String> {
    let answer = stub_answer(prompt);
    let words: Vec<&str> = answer.split(' ').collect();
    let mut groups = Vec::new();
    for chunk in words.chunks(6) {
        groups.push(chunk.join(" "));
    }
    let last = groups.len().saturating_sub(1);
    groups
        .into_iter()
        .enumerate()
        .map(|(i, g)| if i < last { g + " " } else { g })
        .collect()
}

fn word_set(text: &str) -> std::collections::HashSet<String> {
    text.split(|c: char| !c.is_alphanumeric())
        .filter(|w| !w.is_empty())
        .map(|w| w.to_lowercase())
        .collect()
}

// Target-denominator overlap, same ceiling semantics as _stub_score: only
// reproducing every target word reaches 10.
fn stub_score(output: &str, target: &str) -> Verdict {
    let want = word_set(target);
    if want.is_empty() {
        return Verdict {
            score: 0,
            feedback: STUB_COMMENT.into(),
        };
    }
    let got = word_set(output);
    let score = (10 * want.intersection(&got).count() / want.len()) as i64;
    Verdict {
        score,
        feedback: STUB_COMMENT.into(),
    }
}

const STUB_ROLE: &[&str] = &["tu es", "role", "incarne", "agis comme", "parle en tant que"];
const STUB_PUBLIC: &[&str] = &["public", "destinataire", "lecteur", "adresse", "pour un", "pour une", "niveau"];
const STUB_CONTEXT: &[&str] = &["contexte", "situation", "cadre", "materiel", "donnees", "enonce", "ingredients"];
const STUB_FORMAT: &[&str] = &["format", "liste", "tableau", "json", "markdown", "puces", "sous forme", "en 3", "en 2", "en 5"];
const STUB_LENGTH: &[&str] = &["maximum", "moins de", "au plus", "mots", "lignes", "phrase", "court", "bref", "concis"];

// Keyword-marker heuristic standing in for the critic without a key: 1 base,
// +2 per structural criterion present, +1 for a length bound.
pub fn stub_grade(prompt: &str) -> Verdict {
    let text = prompt.to_lowercase();
    if text.trim().is_empty() {
        return Verdict { score: 0, feedback: STUB_COMMENT.into() };
    }
    let has = |markers: &[&str]| markers.iter().any(|m| text.contains(m));
    let mut points = 1;
    for markers in [STUB_ROLE, STUB_PUBLIC, STUB_CONTEXT, STUB_FORMAT] {
        points += 2 * has(markers) as i64;
    }
    points += has(STUB_LENGTH) as i64;
    Verdict { score: points, feedback: STUB_COMMENT.into() }
}

impl super::LlmSource for StubSource {
    // One 20ms beat per chunk: slow enough for the WS fan-out path to show
    // progressive frames, fast enough to keep the suite under a second.
    fn stream_into(
        &self,
        prompt: String,
        tx: mpsc::UnboundedSender<String>,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = super::StreamResult> + Send + '_>> {
        Box::pin(async move {
            for chunk in stub_chunks(&prompt) {
                tokio::time::sleep(std::time::Duration::from_millis(20)).await;
                if tx.send(chunk).is_err() {
                    break;
                }
            }
            Ok(false)
        })
    }

    fn score(
        &self,
        _prompt: &str,
        output: &str,
        target: &str,
        _criteria: &[String],
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Verdict> + Send + '_>> {
        let verdict = stub_score(output, target);
        Box::pin(async move { verdict })
    }

    fn grade_prompt(
        &self,
        prompt: &str,
        _target: &str,
        _criteria: &[String],
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Verdict> + Send + '_>> {
        let verdict = stub_grade(prompt);
        Box::pin(async move { verdict })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn stub_chunks_six_words() {
        let chunks = stub_chunks("one two three four five six seven");
        assert_eq!(chunks.len(), 2); // 6 words + 1 word, mirrors llm.py stub
        assert_eq!(chunks[0].split_whitespace().count(), 6);
        assert_eq!(chunks.concat(), stub_answer("one two three four five six seven"));
    }

    // Same enrichment as the live path (ws builds it before the llm call):
    // the stub echoes exercice + prompt eleve, and grading the enriched
    // text scores the same method as grading the raw prompt.
    #[test]
    fn stub_mirrors_enriched_input() {
        let enriched = super::super::enriched_input(2, "resume ce mail", "fais un resume concis, format liste");
        assert!(stub_chunks(&enriched).concat().contains("resume ce mail"));
        assert!(stub_chunks(&enriched).concat().contains("Prompt élève"));
        assert_eq!(
            stub_grade(&enriched).score,
            stub_grade("fais un resume concis, format liste").score
        );
    }
}
