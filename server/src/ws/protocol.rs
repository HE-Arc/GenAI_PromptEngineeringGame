use serde::{Deserialize, Serialize};
use serde_json::Value;

// Wire types for GET /api/rooms/<code>/ws. `type` values with dots need
// explicit renames; rename_all alone cannot express them.
#[derive(Debug, Deserialize)]
#[serde(tag = "type")]
pub enum WsIn {
    #[serde(rename = "hello")]
    Hello { token: String },
    #[serde(rename = "answer.start")]
    AnswerStart { prompt: String },
    #[serde(rename = "ping")]
    Ping,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum WsOut {
    #[serde(rename = "snapshot")]
    Snapshot { seq: u64, state: Value },
    #[serde(rename = "patch")]
    Patch { seq: u64, changed: Vec<String> },
    #[serde(rename = "answer.chunk")]
    AnswerChunk { player: String, delta: String },
    #[serde(rename = "answer.scoring")]
    AnswerScoring { player: String },
    #[serde(rename = "answer.done")]
    AnswerDone {
        player: String,
        score: i64,
        feedback: String,
    },
    #[serde(rename = "error")]
    Error { code: String, message: String },
    #[serde(rename = "pong")]
    Pong,
}

impl WsOut {
    pub fn error(code: &str, message: &str) -> Self {
        WsOut::Error {
            code: code.to_string(),
            message: message.to_string(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dotted_types_round_trip() {
        let hello: WsIn = serde_json::from_str(r#"{"type":"hello","token":"abc"}"#).unwrap();
        assert!(matches!(hello, WsIn::Hello { .. }));
        let start: WsIn = serde_json::from_str(r#"{"type":"answer.start","prompt":"p"}"#).unwrap();
        assert!(matches!(start, WsIn::AnswerStart { .. }));
        let out = WsOut::AnswerChunk {
            player: "ana".into(),
            delta: "hi".into(),
        };
        let v: Value = serde_json::to_value(&out).unwrap();
        assert_eq!(v["type"], "answer.chunk");
    }
}
