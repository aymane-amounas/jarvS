//! Owner approvals for tools whose policy is `approval`.
//!
//! A request is a small JSON file under `<data>/approvals/pending/`. Any process
//! (`jarvs approve <id>`, later the dashboard or a chat command) answers it by
//! writing `<data>/approvals/decisions/<id>`. Files keep it simple, cross-process
//! and inspectable; a request that is not answered in time counts as denied.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PendingApproval {
    pub id: String,
    pub task_id: String,
    pub tool: String,
    pub capability: String,
    pub summary: String,
    pub created: u64,
}

#[derive(Debug, Clone)]
pub struct ApprovalStore {
    dir: PathBuf,
}

static COUNTER: AtomicU32 = AtomicU32::new(0);

impl ApprovalStore {
    pub fn new(dir: PathBuf) -> Self {
        Self { dir }
    }

    fn pending_dir(&self) -> PathBuf {
        self.dir.join("pending")
    }

    fn decisions_dir(&self) -> PathBuf {
        self.dir.join("decisions")
    }

    pub fn submit(
        &self,
        task_id: &str,
        tool: &str,
        capability: &str,
        summary: &str,
    ) -> Result<PendingApproval, String> {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default();
        let request = PendingApproval {
            id: format!(
                "{:x}{:x}{:x}",
                now.as_nanos(),
                std::process::id(),
                COUNTER.fetch_add(1, Ordering::Relaxed)
            ),
            task_id: task_id.into(),
            tool: tool.into(),
            capability: capability.into(),
            summary: summary.into(),
            created: now.as_secs(),
        };
        fs::create_dir_all(self.pending_dir()).map_err(|e| e.to_string())?;
        fs::create_dir_all(self.decisions_dir()).map_err(|e| e.to_string())?;
        let json = serde_json::to_string_pretty(&request).map_err(|e| e.to_string())?;
        let mut pending = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(self.pending_dir().join(format!("{}.json", request.id)))
            .map_err(|e| e.to_string())?;
        if let Err(err) = pending.write_all(json.as_bytes()) {
            drop(pending);
            let _ = fs::remove_file(self.pending_dir().join(format!("{}.json", request.id)));
            return Err(err.to_string());
        }
        Ok(request)
    }

    /// Blocks until the owner decides, `timeout` passes, or `cancel` is set.
    /// Only an explicit "allow" returns true.
    pub fn wait(&self, id: &str, timeout: Duration, cancel: &AtomicBool) -> bool {
        if id.is_empty() || !id.chars().all(|c| c.is_ascii_hexdigit()) {
            return false;
        }
        let deadline = Instant::now() + timeout;
        let decision_file = self.decisions_dir().join(id);
        let allowed = loop {
            if let Ok(text) = fs::read_to_string(&decision_file) {
                match text.trim() {
                    "allow" => break true,
                    "deny" => break false,
                    _ => {}
                }
            }
            if Instant::now() >= deadline || cancel.load(Ordering::Relaxed) {
                break false;
            }
            thread::sleep(Duration::from_millis(200));
        };
        let _ = fs::remove_file(self.pending_dir().join(format!("{id}.json")));
        let _ = fs::remove_file(&decision_file);
        allowed
    }

    pub fn list(&self) -> Vec<PendingApproval> {
        let mut items: Vec<PendingApproval> = fs::read_dir(self.pending_dir())
            .into_iter()
            .flatten()
            .flatten()
            .filter_map(|entry| fs::read_to_string(entry.path()).ok())
            .filter_map(|text| serde_json::from_str(&text).ok())
            .collect();
        items.sort_by_key(|item| item.created);
        items
    }

    pub fn decide(&self, id: &str, allow: bool) -> Result<PendingApproval, String> {
        if id.is_empty() || !id.chars().all(|c| c.is_ascii_hexdigit()) {
            return Err(format!("invalid approval id: {id}"));
        }
        let request_path = self.pending_dir().join(format!("{id}.json"));
        let pending = fs::read_to_string(&request_path)
            .ok()
            .and_then(|text| serde_json::from_str::<PendingApproval>(&text).ok())
            .filter(|item| item.id == id)
            .ok_or_else(|| format!("no pending approval {id}"))?;
        let decision_path = self.decisions_dir().join(id);
        let mut decision = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&decision_path)
            .map_err(|err| {
                if err.kind() == std::io::ErrorKind::AlreadyExists {
                    format!("approval {id} already has a decision")
                } else {
                    err.to_string()
                }
            })?;
        if let Err(err) = decision.write_all(if allow { b"allow" } else { b"deny" }) {
            drop(decision);
            let _ = fs::remove_file(&decision_path);
            return Err(err.to_string());
        }
        if !request_path.exists() {
            let _ = fs::remove_file(&decision_path);
            return Err(format!("no pending approval {id}"));
        }
        Ok(pending)
    }
}

/// One-line, value-light description of a tool call for the owner to judge.
/// Free text (message bodies, file contents) is reduced to its length.
pub fn summarize(tool: &str, args: &Value) -> String {
    let Some(map) = args.as_object() else {
        return format!("{tool}()");
    };
    let parts: Vec<String> = map
        .iter()
        .map(|(key, value)| match (key.as_str(), value) {
            ("content" | "text" | "source", Value::String(s)) => {
                format!("{key}=<{} chars>", s.chars().count())
            }
            (_, Value::String(s)) => {
                let short: String = s.chars().take(120).collect();
                format!("{key}={short:?}")
            }
            (_, other) => {
                let text = other.to_string();
                format!("{key}={}", text.chars().take(120).collect::<String>())
            }
        })
        .collect();
    format!("{tool}({})", parts.join(", "))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::workspace::tests::temp_dir;
    use std::sync::Arc;

    #[test]
    fn approve_deny_timeout_and_cancel() {
        let store = Arc::new(ApprovalStore::new(temp_dir("approvals")));
        let never = AtomicBool::new(false);

        let req = store
            .submit(
                "t1",
                "write_file",
                "create or modify files",
                "write_file(path=\"a\")",
            )
            .unwrap();
        assert_eq!(store.list(), vec![req.clone()]);
        let waiter = {
            let store = store.clone();
            let id = req.id.clone();
            thread::spawn(move || store.wait(&id, Duration::from_secs(5), &AtomicBool::new(false)))
        };
        store.decide(&req.id, true).unwrap();
        assert!(waiter.join().unwrap());
        assert!(store.list().is_empty(), "answered requests are removed");

        let req = store.submit("t1", "write_file", "c", "s").unwrap();
        store.decide(&req.id, false).unwrap();
        assert!(!store.wait(&req.id, Duration::from_secs(5), &never));

        let req = store.submit("t1", "write_file", "c", "s").unwrap();
        assert!(
            !store.wait(&req.id, Duration::from_millis(300), &never),
            "timeout denies"
        );

        let req = store.submit("t1", "write_file", "c", "s").unwrap();
        assert!(
            !store.wait(&req.id, Duration::from_secs(5), &AtomicBool::new(true)),
            "cancel denies"
        );
    }

    #[test]
    fn decide_rejects_unknown_and_malicious_ids() {
        let store = ApprovalStore::new(temp_dir("approvals-ids"));
        assert!(store.decide("../../etc/passwd", true).is_err());
        assert!(
            store
                .decide("abc123", true)
                .unwrap_err()
                .contains("no pending")
        );
    }

    #[test]
    fn approval_decision_is_one_time_and_cannot_be_replayed() {
        let store = ApprovalStore::new(temp_dir("approval-replay"));
        let req = store.submit("t1", "write_file", "files", "write").unwrap();

        store.decide(&req.id, true).unwrap();
        assert!(store.decide(&req.id, false).is_err());
        assert!(store.wait(&req.id, Duration::from_secs(1), &AtomicBool::new(false)));
        assert!(store.decide(&req.id, true).is_err());
        assert!(!store.wait(&req.id, Duration::from_millis(10), &AtomicBool::new(false)));
    }

    #[test]
    fn summary_hides_free_text() {
        let s = summarize(
            "write_file",
            &serde_json::json!({"path": "notes/a.md", "content": "secret body"}),
        );
        assert!(s.contains("path=\"notes/a.md\""));
        assert!(s.contains("content=<11 chars>"));
        assert!(!s.contains("secret"));
    }
}
