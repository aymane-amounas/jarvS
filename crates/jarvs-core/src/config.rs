use std::collections::BTreeMap;
use std::env;
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

use jarvs_platform::AppPaths;
use serde::{Deserialize, Serialize};

use crate::permissions::Decision;

/// User settings, stored as TOML in the OS config directory. Every field has a
/// default, so jarvS runs with no config file at all.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    /// Root of the files agent tools may read and write.
    pub workspace: Option<PathBuf>,
    pub agent: AgentConfig,
    pub providers: ProvidersConfig,
    /// Per-tool overrides of the default policy, e.g. `write_file = "allow"`.
    pub permissions: BTreeMap<String, Decision>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(default, deny_unknown_fields)]
pub struct AgentConfig {
    /// Directory containing `ipc_worker.py` (the Python agent runtime).
    pub python_dir: Option<PathBuf>,
    /// Python interpreter; defaults to the project venv, then `python3`/`python`.
    pub python: Option<PathBuf>,
    pub task_timeout_secs: u64,
    /// How long a tool call waits for the owner's approval before it is denied.
    pub approval_timeout_secs: u64,
}

impl Default for AgentConfig {
    fn default() -> Self {
        Self {
            python_dir: None,
            python: None,
            task_timeout_secs: 1800,
            approval_timeout_secs: 600,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(default, deny_unknown_fields)]
pub struct ProvidersConfig {
    pub ollama_host: String,
}

impl Default for ProvidersConfig {
    fn default() -> Self {
        Self {
            ollama_host: "http://127.0.0.1:11434".into(),
        }
    }
}

#[derive(Debug)]
pub enum ConfigError {
    Io(PathBuf, std::io::Error),
    Parse(PathBuf, String),
}

impl fmt::Display for ConfigError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(path, err) => write!(f, "cannot read {}: {err}", path.display()),
            Self::Parse(path, err) => write!(f, "invalid config {}: {err}", path.display()),
        }
    }
}

impl std::error::Error for ConfigError {}

impl Config {
    /// Load `path` if it exists, then apply environment overrides.
    pub fn load(path: &Path) -> Result<Self, ConfigError> {
        let mut config = match fs::read_to_string(path) {
            Ok(text) => toml::from_str(&text)
                .map_err(|err| ConfigError::Parse(path.into(), err.to_string()))?,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => Self::default(),
            Err(err) => return Err(ConfigError::Io(path.into(), err)),
        };
        if let Some(dir) = env_path("JARVS_WORKSPACE") {
            config.workspace = Some(dir);
        }
        if let Some(dir) = env_path("JARVS_PYTHON_DIR") {
            config.agent.python_dir = Some(dir);
        }
        if let Some(host) = env::var("OLLAMA_HOST").ok().filter(|h| !h.is_empty()) {
            config.providers.ollama_host = host;
        }
        Ok(config)
    }

    pub fn to_toml(&self) -> String {
        toml::to_string_pretty(self).expect("config always serializes")
    }

    pub fn workspace_dir(&self, paths: &AppPaths) -> PathBuf {
        self.workspace
            .clone()
            .unwrap_or_else(|| paths.workspace_dir.clone())
    }

    /// The Python agent directory: configured, or found next to the working
    /// directory or the executable (a source checkout).
    pub fn python_dir(&self) -> Option<PathBuf> {
        if let Some(dir) = &self.agent.python_dir {
            return Some(dir.clone());
        }
        let starts = [env::current_dir().ok(), env::current_exe().ok()];
        starts
            .into_iter()
            .flatten()
            .flat_map(|start| start.ancestors().map(Path::to_path_buf).collect::<Vec<_>>())
            .find(|dir| dir.join("ipc_worker.py").is_file())
    }

    pub fn python_interpreter(&self, python_dir: &Path) -> PathBuf {
        if let Some(python) = &self.agent.python {
            return python.clone();
        }
        let venv = if cfg!(windows) {
            python_dir.join(".venv").join("Scripts").join("python.exe")
        } else {
            python_dir.join(".venv").join("bin").join("python")
        };
        if venv.is_file() {
            return venv;
        }
        PathBuf::from(if cfg!(windows) { "python" } else { "python3" })
    }
}

fn env_path(name: &str) -> Option<PathBuf> {
    env::var_os(name)
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_file_gives_defaults() {
        let config = Config::load(Path::new("/definitely/not/here/config.toml")).unwrap();
        assert_eq!(config.agent.task_timeout_secs, 1800);
        assert!(config.permissions.is_empty());
    }

    #[test]
    fn round_trips_and_rejects_unknown_keys() {
        let mut config = Config::default();
        config
            .permissions
            .insert("write_file".into(), Decision::Allow);
        let parsed: Config = toml::from_str(&config.to_toml()).unwrap();
        assert_eq!(parsed, config);
        assert!(toml::from_str::<Config>("typo_key = 1").is_err());
    }
}
