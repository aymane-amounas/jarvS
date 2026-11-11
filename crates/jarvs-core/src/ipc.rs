//! Wire format between the Rust runtime and the Python worker: one JSON object
//! per line, every message carrying `"v": PROTOCOL_VERSION`. See
//! docs/IPC_PROTOCOL.md for the full contract.

use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const PROTOCOL_VERSION: u32 = 1;

/// Messages the runtime sends to the worker.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum ToWorker {
    #[serde(rename = "ping")]
    Ping { nonce: String },
    #[serde(rename = "task.run")]
    TaskRun {
        id: String,
        agent: String,
        task: String,
        want: String,
    },
    #[serde(rename = "task.cancel")]
    TaskCancel { id: String },
    #[serde(rename = "tool.response")]
    ToolResponse {
        call_id: String,
        outcome: ToolOutcome,
        output: String,
    },
    #[serde(rename = "shutdown")]
    Shutdown,
}

/// Messages the worker sends to the runtime.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum FromWorker {
    #[serde(rename = "ready")]
    Ready {
        protocol: u32,
        worker: String,
        agents: Vec<String>,
    },
    #[serde(rename = "pong")]
    Pong { nonce: String },
    #[serde(rename = "task.progress")]
    TaskProgress {
        id: String,
        stage: String,
        message: String,
    },
    #[serde(rename = "tool.request")]
    ToolRequest {
        id: String,
        call_id: String,
        tool: String,
        args: Value,
    },
    #[serde(rename = "task.result")]
    TaskResult {
        id: String,
        status: TaskStatus,
        answer: String,
    },
    #[serde(rename = "error")]
    Error {
        id: Option<String>,
        code: String,
        message: String,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ToolOutcome {
    /// The runtime ran the tool; `output` is its result.
    Executed,
    /// The worker may run its own implementation (not-yet-ported tools).
    Delegate,
    /// Refused; `output` explains why.
    Denied,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskStatus {
    Ok,
    Incomplete,
    Error,
    Cancelled,
}

#[derive(Serialize, Deserialize)]
struct Envelope<T> {
    v: u32,
    #[serde(flatten)]
    body: T,
}

pub fn encode(message: &ToWorker) -> String {
    serde_json::to_string(&Envelope {
        v: PROTOCOL_VERSION,
        body: message,
    })
    .expect("messages always serialize")
}

#[derive(Debug, PartialEq)]
pub enum DecodeError {
    Json(String),
    Version(u32),
}

pub fn decode(line: &str) -> Result<FromWorker, DecodeError> {
    let envelope: Envelope<FromWorker> =
        serde_json::from_str(line).map_err(|e| DecodeError::Json(e.to_string()))?;
    if envelope.v != PROTOCOL_VERSION {
        return Err(DecodeError::Version(envelope.v));
    }
    Ok(envelope.body)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn encodes_with_version_and_type() {
        let line = encode(&ToWorker::TaskCancel { id: "t1".into() });
        let value: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(value, json!({"v": 1, "type": "task.cancel", "id": "t1"}));
        assert!(!line.contains('\n'));
    }

    #[test]
    fn decodes_worker_messages() {
        let msg = decode(r#"{"v":1,"type":"tool.request","id":"t","call_id":"c1","tool":"read_file","args":{"path":"a"}}"#).unwrap();
        assert_eq!(
            msg,
            FromWorker::ToolRequest {
                id: "t".into(),
                call_id: "c1".into(),
                tool: "read_file".into(),
                args: json!({"path": "a"})
            }
        );
        let result =
            decode(r#"{"v":1,"type":"task.result","id":"t","status":"cancelled","answer":""}"#)
                .unwrap();
        assert!(matches!(
            result,
            FromWorker::TaskResult {
                status: TaskStatus::Cancelled,
                ..
            }
        ));
    }

    #[test]
    fn rejects_wrong_version_and_garbage() {
        assert_eq!(
            decode(r#"{"v":2,"type":"pong","nonce":"x"}"#),
            Err(DecodeError::Version(2))
        );
        assert!(matches!(decode("not json"), Err(DecodeError::Json(_))));
        assert!(matches!(
            decode(r#"{"v":1,"type":"unknown"}"#),
            Err(DecodeError::Json(_))
        ));
    }

    #[test]
    fn malformed_payload_corpus_never_panics_or_decodes_unexpected_types() {
        let corpus = [
            "",
            "[]",
            "null",
            "true",
            "{}",
            r#"{"v":1}"#,
            r#"{"v":1,"type":[]}"#,
            r#"{"v":1,"type":"tool.request","id":null,"call_id":{},"tool":false,"args":[]}"#,
            r#"{"v":1,"type":"task.result","id":"x","status":"unknown","answer":""}"#,
        ];
        for line in corpus {
            assert!(decode(line).is_err(), "unexpectedly decoded: {line}");
        }

        let mut state = 0x9e37_79b9_u32;
        let alphabet = b"{}[],:\"0123456789truefalsenull";
        for _ in 0..2_000 {
            let length = (state as usize) % 512;
            let mut bytes = Vec::with_capacity(length);
            for _ in 0..length {
                state ^= state << 13;
                state ^= state >> 17;
                state ^= state << 5;
                bytes.push(alphabet[state as usize % alphabet.len()]);
            }
            let input = String::from_utf8(bytes).expect("corpus is ASCII");
            let _ = decode(&input);
        }
    }
}
