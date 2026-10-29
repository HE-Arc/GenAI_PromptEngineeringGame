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
        // The AI's note (game::ai_score): a real division, hence a float.
        score: f64,
        feedback: String,
        // The token cap cut the answer short; the player is told so.
        #[serde(default)]
        truncated: bool,
    },
    // `player` set: a refusal or failure that belongs to that player alone.
    // The hub broadcasts it, and each socket's writer drops it unless the
    // socket is that player's, so it never lands on another screen.
    #[serde(rename = "error")]
    Error {
        code: String,
        message: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        player: Option<String>,
    },
    #[serde(rename = "pong")]
    Pong,
}

impl WsOut {
    pub fn error(code: &str, message: &str) -> Self {
        WsOut::Error {
            code: code.to_string(),
            message: message.to_string(),
            player: None,
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
