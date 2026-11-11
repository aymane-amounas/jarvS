use std::env;
use std::io;
use std::path::PathBuf;

use directories::ProjectDirs;
use serde::Serialize;

/// Where jarvS keeps each kind of file on this machine.
///
/// Defaults follow each OS's conventions (XDG on Linux, `~/Library` on macOS,
/// `%APPDATA%`/`%LOCALAPPDATA%` on Windows). `JARVS_CONFIG_DIR` and
/// `JARVS_DATA_DIR` override them for portable installs and tests.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct AppPaths {
    pub config_dir: PathBuf,
    pub data_dir: PathBuf,
    pub cache_dir: PathBuf,
    pub log_dir: PathBuf,
    pub temp_dir: PathBuf,
    /// Default workspace: the only tree agent file tools may touch.
    pub workspace_dir: PathBuf,
}

impl AppPaths {
    pub fn discover() -> io::Result<Self> {
        let dirs = ProjectDirs::from("dev", "jarvS", "jarvs").ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotFound,
                "could not determine a home directory",
            )
        })?;
        let config_dir = env_dir("JARVS_CONFIG_DIR").unwrap_or_else(|| dirs.config_dir().into());
        let data_dir = env_dir("JARVS_DATA_DIR").unwrap_or_else(|| dirs.data_local_dir().into());
        let cache_dir = if env_dir("JARVS_DATA_DIR").is_some() {
            data_dir.join("cache")
        } else {
            dirs.cache_dir().into()
        };
        Ok(Self::from_roots(config_dir, data_dir, cache_dir))
    }

    pub fn from_roots(config_dir: PathBuf, data_dir: PathBuf, cache_dir: PathBuf) -> Self {
        Self {
            log_dir: data_dir.join("logs"),
            temp_dir: env::temp_dir().join("jarvs"),
            workspace_dir: data_dir.join("workspace"),
            config_dir,
            data_dir,
            cache_dir,
        }
    }

    pub fn config_file(&self) -> PathBuf {
        self.config_dir.join("config.toml")
    }

    pub fn ensure(&self) -> io::Result<()> {
        for dir in [
            &self.config_dir,
            &self.data_dir,
            &self.cache_dir,
            &self.log_dir,
            &self.workspace_dir,
        ] {
            std::fs::create_dir_all(dir)?;
        }
        Ok(())
    }
}

fn env_dir(name: &str) -> Option<PathBuf> {
    env::var_os(name)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn discover_returns_distinct_absolute_dirs() {
        let paths = AppPaths::discover().expect("home directory");
        assert!(paths.config_dir.is_absolute());
        assert!(paths.data_dir.is_absolute());
        assert_ne!(paths.workspace_dir, paths.data_dir);
        assert!(paths.config_file().ends_with("config.toml"));
        let text = format!("{paths:?}");
        assert!(!text.contains("/home/jarvis"));
    }

    #[test]
    fn roots_derive_logs_and_workspace() {
        let paths = AppPaths::from_roots("c".into(), "d".into(), "k".into());
        assert_eq!(paths.log_dir, PathBuf::from("d").join("logs"));
        assert_eq!(paths.workspace_dir, PathBuf::from("d").join("workspace"));
    }
}
