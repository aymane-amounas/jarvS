//! The single authority deciding whether an agent tool call may run.
//! Decisions are made here, in Rust, immediately before execution; prompts
//! and the Python runtime only ever ask.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Decision {
    Allow,
    /// Needs a grant from the owner for this run.
    Approval,
    Deny,
}

/// Who performs the operation once it is allowed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Execution {
    /// Rust executes it; Python receives only the output.
    Native,
    /// Python may execute its existing implementation after this approval.
    /// Temporary, for tools not yet ported; see docs/ARCHITECTURE.md.
    Delegate,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct ToolSpec {
    pub name: &'static str,
    pub capability: &'static str,
    pub default: Decision,
    pub execution: Execution,
}

const fn spec(
    name: &'static str,
    capability: &'static str,
    default: Decision,
    execution: Execution,
) -> ToolSpec {
    ToolSpec {
        name,
        capability,
        default,
        execution,
    }
}

use Decision::{Allow, Approval, Deny};
use Execution::{Delegate, Native};

/// Every tool the Python agent knows, with its default policy.
pub const TOOLS: &[ToolSpec] = &[
    spec("get_system_state", "read system information", Allow, Native),
    spec("system_status", "read system information", Allow, Native),
    spec("read_file", "read workspace files", Allow, Native),
    spec("read_local_file", "read workspace files", Allow, Native),
    spec("write_file", "create or modify files", Approval, Native),
    spec("search_web", "web research", Allow, Native),
    spec("search_news", "web research", Allow, Native),
    spec("fetch", "web research", Allow, Native),
    spec("notify_owner", "send external messages", Approval, Delegate),
    spec("add_goal", "change missions", Approval, Delegate),
    spec(
        "install_python_package",
        "install software",
        Approval,
        Delegate,
    ),
    spec(
        "run_project_checks",
        "run project commands",
        Approval,
        Delegate,
    ),
    spec(
        "create_python_tool",
        "execute generated code",
        Approval,
        Delegate,
    ),
    spec("shell", "execute arbitrary shell commands", Deny, Delegate),
];

/// Tools whose default `Deny` no configuration can lift: there is no real OS
/// isolation for them yet.
const LOCKED: &[&str] = &["shell"];

/// Unknown names (e.g. user-created custom tools) need approval.
pub const UNKNOWN: ToolSpec = spec("<custom>", "run custom tools", Approval, Delegate);

#[derive(Debug, Clone, Default)]
pub struct Policy {
    overrides: BTreeMap<String, Decision>,
    grants: Vec<String>,
}

impl Policy {
    pub fn new(overrides: BTreeMap<String, Decision>, grants: Vec<String>) -> Self {
        Self { overrides, grants }
    }

    pub fn spec(tool: &str) -> ToolSpec {
        TOOLS
            .iter()
            .copied()
            .find(|t| t.name == tool)
            .unwrap_or(UNKNOWN)
    }

    pub fn decide(&self, tool: &str) -> Decision {
        if LOCKED.contains(&tool) {
            return Deny;
        }
        let configured = self
            .overrides
            .get(tool)
            .copied()
            .unwrap_or(Self::spec(tool).default);
        match configured {
            Approval if self.grants.iter().any(|g| g == tool) => Allow,
            other => other,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_follow_security_table() {
        let policy = Policy::default();
        assert_eq!(policy.decide("read_file"), Allow);
        assert_eq!(policy.decide("search_web"), Allow);
        assert_eq!(policy.decide("write_file"), Approval);
        assert_eq!(policy.decide("install_python_package"), Approval);
        assert_eq!(policy.decide("notify_owner"), Approval);
        assert_eq!(policy.decide("shell"), Deny);
        assert_eq!(policy.decide("some_custom_tool"), Approval);
    }

    #[test]
    fn grants_lift_only_approval() {
        let policy = Policy::new(
            BTreeMap::from([("fetch".into(), Deny)]),
            vec!["write_file".into(), "fetch".into(), "shell".into()],
        );
        assert_eq!(policy.decide("write_file"), Allow);
        assert_eq!(policy.decide("fetch"), Deny);
        assert_eq!(policy.decide("shell"), Deny);
    }

    #[test]
    fn shell_cannot_be_enabled_by_config() {
        let policy = Policy::new(BTreeMap::from([("shell".into(), Allow)]), vec![]);
        assert_eq!(policy.decide("shell"), Deny);
    }
}
