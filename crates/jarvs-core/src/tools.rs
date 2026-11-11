//! Answers the worker's tool requests: decide, audit, then execute natively or
//! hand back a delegation. Nothing here trusts the arguments it receives.

use std::fs;
use std::io::Write;

use serde_json::{Value, json};

use crate::approvals::summarize;
use crate::audit::AuditLog;
use crate::ipc::ToolOutcome;
use crate::permissions::{Decision, Execution, Policy};
use crate::web::Web;
use crate::workspace::Workspace;

pub const FILE_LIMIT: u64 = 1_000_000;
const OUTPUT_LIMIT: usize = 20_000;

/// What the owner is asked to approve.
pub struct ApprovalRequest<'a> {
    pub task_id: &'a str,
    pub tool: &'a str,
    pub capability: &'a str,
    pub summary: String,
}

/// Asks the owner; returns true only on an explicit yes.
pub type Approver = Box<dyn Fn(&ApprovalRequest) -> bool + Send + Sync>;

pub struct ToolGate {
    pub policy: Policy,
    pub workspace: Workspace,
    pub audit: AuditLog,
    pub web: Web,
    /// `None`: approval-only tools are refused without asking.
    pub approver: Option<Approver>,
}

impl ToolGate {
    pub fn handle(&self, task_id: &str, tool: &str, args: &Value) -> (ToolOutcome, String) {
        let decision = self.policy.decide(tool);
        let spec = Policy::spec(tool);
        let (outcome, output) = match decision {
            Decision::Deny => (
                ToolOutcome::Denied,
                format!("{tool} is disabled by jarvS policy ({})", spec.capability),
            ),
            Decision::Approval => {
                let request = ApprovalRequest {
                    task_id,
                    tool,
                    capability: spec.capability,
                    summary: summarize(tool, args),
                };
                match &self.approver {
                    Some(ask) if ask(&request) => self.run_allowed(tool, args, spec.execution),
                    Some(_) => (
                        ToolOutcome::Denied,
                        format!("the owner did not approve {tool} ({})", spec.capability),
                    ),
                    None => (
                        ToolOutcome::Denied,
                        format!(
                            "{tool} needs owner approval ({}) and no approver is available for this run.",
                            spec.capability
                        ),
                    ),
                }
            }
            Decision::Allow => self.run_allowed(tool, args, spec.execution),
        };
        let label = match outcome {
            ToolOutcome::Executed => "executed",
            ToolOutcome::Delegate => "delegated",
            ToolOutcome::Denied => "denied",
        };
        self.audit.tool(
            task_id,
            tool,
            args,
            label,
            if outcome == ToolOutcome::Denied {
                &output
            } else {
                ""
            },
        );
        (outcome, output)
    }

    fn run_allowed(&self, tool: &str, args: &Value, execution: Execution) -> (ToolOutcome, String) {
        match execution {
            Execution::Delegate => (ToolOutcome::Delegate, String::new()),
            Execution::Native => (ToolOutcome::Executed, self.execute(tool, args)),
        }
    }

    fn execute(&self, tool: &str, args: &Value) -> String {
        let text = |key: &str| args.get(key).and_then(Value::as_str).unwrap_or("");
        let result = match tool {
            "read_file" | "read_local_file" => self.read_file(text("path")),
            "write_file" => self.write_file(text("path"), args.get("content")),
            "get_system_state" | "system_status" => Ok(self.system_state()),
            "fetch" => self.web.fetch(text("url")),
            "search_web" => self.web.search_web(text("query")),
            "search_news" => {
                let period = args.get("period").and_then(Value::as_str).unwrap_or("d");
                let max = args.get("max_results").and_then(Value::as_u64).unwrap_or(5);
                self.web.search_news(text("query"), period, max as usize)
            }
            other => Err(format!("{other} has no native implementation")),
        };
        let mut output = result.unwrap_or_else(|err| {
            if err.starts_with("error") || err.contains("denied") {
                err
            } else {
                format!("error: {err}")
            }
        });
        if output.len() > OUTPUT_LIMIT {
            let mut cut = OUTPUT_LIMIT;
            while !output.is_char_boundary(cut) {
                cut -= 1;
            }
            output.truncate(cut);
        }
        output
    }

    fn read_file(&self, path: &str) -> Result<String, String> {
        let target = self.workspace.resolve(path).map_err(|e| e.to_string())?;
        let meta = fs::metadata(&target).map_err(|e| format!("read failed: {e}"))?;
        if meta.len() > FILE_LIMIT {
            return Err(format!("file exceeds {FILE_LIMIT} bytes"));
        }
        fs::read_to_string(&target).map_err(|e| format!("read failed: {e}"))
    }

    fn write_file(&self, path: &str, content: Option<&Value>) -> Result<String, String> {
        let content = content
            .and_then(Value::as_str)
            .ok_or("content must be text")?;
        if content.len() as u64 > FILE_LIMIT {
            return Err(format!("content exceeds {FILE_LIMIT} bytes"));
        }
        let target = self.workspace.resolve(path).map_err(|e| e.to_string())?;
        let parent = target.parent().ok_or("invalid path")?;
        fs::create_dir_all(parent).map_err(|e| format!("write failed: {e}"))?;
        // Re-check after creating directories: a racing symlink swap must not
        // redirect the write outside the workspace.
        let target = self.workspace.resolve(path).map_err(|e| e.to_string())?;
        let temp = target.with_file_name(format!(
            ".{}.jarvs-tmp-{}",
            target
                .file_name()
                .map(|n| n.to_string_lossy())
                .unwrap_or_default(),
            std::process::id()
        ));
        let write = || -> std::io::Result<()> {
            let mut file = fs::File::create(&temp)?;
            file.write_all(content.as_bytes())?;
            file.sync_all()?;
            fs::rename(&temp, &target)
        };
        write().map_err(|e| {
            let _ = fs::remove_file(&temp);
            format!("write failed: {e}")
        })?;
        let shown = target
            .strip_prefix(self.workspace.root())
            .unwrap_or(&target);
        Ok(format!(
            "wrote {} characters to {}",
            content.chars().count(),
            shown.display()
        ))
    }

    fn system_state(&self) -> String {
        let hw = jarvs_platform::detect();
        let mut listing = Vec::new();
        if let Ok(entries) = fs::read_dir(self.workspace.root()) {
            for entry in entries.flatten().take(200) {
                let name = entry.file_name().to_string_lossy().into_owned();
                if self.workspace.resolve(&name).is_ok() {
                    let kind = entry.file_type().map_or("unknown", |t| {
                        if t.is_symlink() {
                            "symlink"
                        } else if t.is_dir() {
                            "directory"
                        } else {
                            "file"
                        }
                    });
                    listing.push(json!({"name": name, "type": kind}));
                }
            }
        }
        json!({
            "hardware": hw,
            "current_directory": self.workspace.root(),
            "directory_contents": listing,
            "shell_tool_enabled": false,
        })
        .to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::workspace::tests::temp_dir;
    use std::collections::BTreeMap;

    fn gate(grants: &[&str]) -> (ToolGate, std::path::PathBuf) {
        let base = temp_dir("gate");
        let workspace = Workspace::open(&base.join("ws")).unwrap();
        let policy = Policy::new(
            BTreeMap::new(),
            grants.iter().map(|g| g.to_string()).collect(),
        );
        (
            ToolGate {
                policy,
                workspace,
                audit: AuditLog::new(base.join("audit.log")),
                web: Web::new(),
                approver: None,
            },
            base,
        )
    }

    #[test]
    fn read_is_allowed_write_needs_grant() {
        let (gate, base) = gate(&[]);
        fs::write(gate.workspace.root().join("a.txt"), "hi").unwrap();
        assert_eq!(
            gate.handle("t", "read_file", &json!({"path": "a.txt"})),
            (ToolOutcome::Executed, "hi".into())
        );
        let (outcome, why) =
            gate.handle("t", "write_file", &json!({"path": "b.txt", "content": "x"}));
        assert_eq!(outcome, ToolOutcome::Denied);
        assert!(why.contains("approval"));
        assert!(!gate.workspace.root().join("b.txt").exists());
        let audit = fs::read_to_string(base.join("audit.log")).unwrap();
        assert_eq!(audit.lines().count(), 2);
        assert!(!audit.contains("\"x\""), "content must not be logged");
    }

    #[test]
    fn granted_write_is_atomic_and_confined() {
        let (gate, base) = gate(&["write_file"]);
        let (outcome, out) = gate.handle(
            "t",
            "write_file",
            &json!({"path": "d/b.txt", "content": "héllo"}),
        );
        assert_eq!(outcome, ToolOutcome::Executed, "{out}");
        assert_eq!(
            fs::read_to_string(gate.workspace.root().join("d/b.txt")).unwrap(),
            "héllo"
        );
        let (_, out) = gate.handle(
            "t",
            "write_file",
            &json!({"path": "../escape.txt", "content": "x"}),
        );
        assert!(out.contains("outside"));
        assert!(!base.join("escape.txt").exists());
    }

    #[test]
    fn shell_denied_and_research_native() {
        let (gate, _) = gate(&["shell"]);
        assert_eq!(
            gate.handle("t", "shell", &json!({"cmd": "rm -rf /"})).0,
            ToolOutcome::Denied
        );
        let (outcome, out) = gate.handle(
            "t",
            "fetch",
            &json!({"url": "http://127.0.0.1:11434/api/tags"}),
        );
        assert_eq!(outcome, ToolOutcome::Executed);
        assert!(out.contains("blocked"), "{out}");
        assert_eq!(
            gate.handle("t", "search_web", &json!({"query": ""})),
            (
                ToolOutcome::Executed,
                "error: search query must be 1-500 characters".into()
            )
        );
        assert_eq!(
            gate.handle("t", "my_custom_tool", &json!({})).0,
            ToolOutcome::Denied
        );
    }

    #[test]
    fn system_state_hides_sensitive_entries() {
        let (gate, _) = gate(&[]);
        fs::write(gate.workspace.root().join(".env"), "SECRET=1").unwrap();
        fs::write(gate.workspace.root().join("notes.md"), "").unwrap();
        let (_, out) = gate.handle("t", "get_system_state", &json!({}));
        assert!(out.contains("notes.md"));
        assert!(!out.contains(".env"));
    }

    #[test]
    fn approver_decides_approval_tools_only() {
        use std::sync::{Arc, Mutex};
        let (mut gate, _) = gate(&[]);
        let asked = Arc::new(Mutex::new(Vec::new()));
        let seen = asked.clone();
        gate.approver = Some(Box::new(move |req: &ApprovalRequest| {
            seen.lock().unwrap().push(req.summary.clone());
            req.tool == "write_file"
        }));
        let (outcome, _) = gate.handle(
            "t",
            "write_file",
            &json!({"path": "ok.txt", "content": "hidden"}),
        );
        assert_eq!(outcome, ToolOutcome::Executed);
        assert!(gate.workspace.root().join("ok.txt").exists());
        let (outcome, why) = gate.handle("t", "install_python_package", &json!({"name": "x"}));
        assert_eq!(outcome, ToolOutcome::Denied);
        assert!(why.contains("did not approve"));
        assert_eq!(
            gate.handle("t", "shell", &json!({"cmd": "x"})).0,
            ToolOutcome::Denied
        );
        assert_eq!(
            gate.handle("t", "read_file", &json!({"path": "ok.txt"})).0,
            ToolOutcome::Executed
        );
        let asked = asked.lock().unwrap();
        assert_eq!(
            asked.len(),
            2,
            "only approval tools reach the owner: {asked:?}"
        );
        assert!(!asked[0].contains("hidden"));
    }
}
