use tokio::sync::mpsc;

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

    // A fixed placeholder so image rounds play end to end without a key.
    fn generate_image(
        &self,
        _prompt: &str,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = super::ImageResult> + Send + '_>> {
        Box::pin(async {
            Ok(super::GeneratedImage {
                mime: "image/svg+xml".into(),
                data: STUB_IMAGE.as_bytes().to_vec(),
                text: "(image locale)".into(),
            })
        })
    }
}

const STUB_IMAGE: &str = r##"<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200" viewBox="0 0 320 200"><rect width="320" height="200" fill="#1d2433"/><text x="160" y="105" fill="#ffc83d" font-family="sans-serif" font-size="18" text-anchor="middle">Image locale (pas de clé)</text></svg>"##;

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
    // the stub echoes exercice + prompt eleve.
    #[test]
    fn stub_mirrors_enriched_input() {
        let enriched = super::super::enriched_input(2, "resume ce mail", "fais un resume concis, format liste");
        assert!(stub_chunks(&enriched).concat().contains("resume ce mail"));
        assert!(stub_chunks(&enriched).concat().contains("Prompt élève"));
    }
}
