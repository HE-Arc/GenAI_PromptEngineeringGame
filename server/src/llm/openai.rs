use async_openai::types::{
    ChatCompletionRequestAssistantMessageArgs, CreateChatCompletionRequestArgs,
    CreateChatCompletionStreamResponse,
};
use futures_util::StreamExt;
use tokio::sync::mpsc;

use super::Verdict;

pub struct OpenAiSource {
    client: async_openai::Client<async_openai::config::OpenAIConfig>,
    play_model: String,
    judge_model: String,
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
            client: async_openai::Client::with_config(config),
            play_model,
            judge_model,
        }
    }

    fn play_request(&self, prompt: &str) -> async_openai::types::CreateChatCompletionRequest {
        CreateChatCompletionRequestArgs::default()
            .model(&self.play_model)
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
        let model = self.judge_model.clone();
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
