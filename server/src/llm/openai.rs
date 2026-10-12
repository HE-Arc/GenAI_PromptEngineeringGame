use async_openai::types::{
    ChatCompletionRequestAssistantMessageArgs, CreateChatCompletionRequestArgs,
    CreateChatCompletionStreamResponse,
};
use futures_util::StreamExt;
use tokio::sync::mpsc;

use super::Verdict;

pub struct OpenAiSource {
    client: async_openai::Client<async_openai::config::OpenAIConfig>,
    model: String,
}

impl OpenAiSource {
    pub fn from_env() -> Self {
        Self {
            client: async_openai::Client::new(),
            model: std::env::var("OPENAI_PLAY_MODEL").unwrap_or_else(|_| "gpt-4o-mini".into()),
        }
    }

    fn play_request(&self, prompt: &str) -> async_openai::types::CreateChatCompletionRequest {
        CreateChatCompletionRequestArgs::default()
            .model(&self.model)
            .messages([ChatCompletionRequestAssistantMessageArgs::default()
                .content(prompt)
                .build()
                .expect("prompt message")
                .into()])
            .build()
            .expect("play request")
    }
}

impl super::LlmSource for OpenAiSource {
    // Deltas forward as they arrive; a mid-stream API error just ends the
    // stream and whatever arrived is still scored and persisted.
    fn stream_into(
        &self,
        prompt: String,
        tx: mpsc::UnboundedSender<String>,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + '_>> {
        let req = self.play_request(&prompt);
        Box::pin(async move {
            let mut stream = match self.client.chat().create_stream(req).await {
                Ok(s) => s,
                Err(_) => return,
            };
            while let Some(item) = stream.next().await {
                let delta: String = item
                    .map(|r: CreateChatCompletionStreamResponse| {
                        r.choices
                            .into_iter()
                            .filter_map(|c| c.delta.content)
                            .collect::<Vec<_>>()
                            .concat()
                    })
                    .unwrap_or_default();
                if !delta.is_empty() && tx.send(delta).is_err() {
                    return;
                }
            }
        })
    }

    // Second non-streaming call: grading must see the whole output, so it
    // cannot reuse the play stream.
    fn score(
        &self,
        prompt: &str,
        output: &str,
        target: &str,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Verdict> + Send + '_>> {
        let grading = format!("prompt: {prompt}\nreponse_obtenue: {output}\ncible: {target}");
        let model = self.model.clone();
        let client = self.client.clone();
        Box::pin(async move {
            let req = CreateChatCompletionRequestArgs::default()
                .model(&model)
                .messages([ChatCompletionRequestAssistantMessageArgs::default()
                    .content(grading)
                    .build()
                    .expect("grading message")
                    .into()])
                .build()
                .expect("grading request");
            let text = client
                .chat()
                .create(req)
                .await
                .ok()
                .and_then(|r| r.choices.into_iter().next())
                .and_then(|c| c.message.content)
                .unwrap_or_default();
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
