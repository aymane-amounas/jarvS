//! Append-only JSON-lines record of every tool decision.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};

#[derive(Debug, Clone)]
pub struct AuditLog {
    path: Option<PathBuf>,
}

impl AuditLog {
    pub fn new(path: PathBuf) -> Self {
        Self { path: Some(path) }
    }

    /// Discards records; for tests and dry runs.
    pub fn disabled() -> Self {
        Self { path: None }
    }

    /// Records the tool, decision and argument *names* only. Argument values
    /// can carry message text or secrets, so only a file path is kept.
    pub fn tool(&self, task_id: &str, tool: &str, args: &Value, decision: &str, detail: &str) {
        let arg_names: Vec<&str> = args
            .as_object()
            .map(|o| o.keys().map(String::as_str).collect())
            .unwrap_or_default();
        self.write(json!({
            "event": "tool",
            "task_id": task_id,
            "tool": tool,
            "arg_names": arg_names,
            "path": args.get("path").and_then(Value::as_str),
            "decision": decision,
            "detail": detail,
        }));
    }

    fn write(&self, mut record: Value) {
        let Some(path) = &self.path else { return };
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |d| d.as_secs());
        record["ts"] = json!(ts);
        if let Some(dir) = path.parent() {
            let _ = fs::create_dir_all(dir);
        }
        // Audit failures must not break the task; they surface in `jarvs status`.
        if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
            let _ = writeln!(file, "{record}");
        }
    }
}
