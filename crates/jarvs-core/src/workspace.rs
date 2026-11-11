//! Confines agent file access to one workspace root.

use std::fmt;
use std::fs;
use std::io;
use std::path::{Component, Path, PathBuf};

/// Path components that are never readable or writable, even inside the
/// workspace: exact names, then substrings, then extensions.
const SENSITIVE_NAMES: &[&str] = &[".env", ".git", ".ssh", "auth_info_baileys"];
const SENSITIVE_PARTS: &[&str] = &["credential", "token", "secret", "id_rsa", "id_ed25519"];
const SENSITIVE_EXTENSIONS: &[&str] = &[".pem", ".key"];

#[derive(Debug)]
pub enum AccessError {
    Empty,
    Outside,
    Sensitive,
    Io(io::Error),
}

impl fmt::Display for AccessError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Empty => f.write_str("path is empty"),
            Self::Outside => f.write_str("access denied: path is outside the jarvS workspace"),
            Self::Sensitive => f.write_str("access denied: path looks like a credential or secret"),
            Self::Io(err) => write!(f, "{err}"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct Workspace {
    root: PathBuf,
}

impl Workspace {
    /// Creates the root if needed and pins its canonical form.
    pub fn open(root: &Path) -> io::Result<Self> {
        fs::create_dir_all(root)?;
        Ok(Self {
            root: root.canonicalize()?,
        })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Resolve `path` (relative to the root, or absolute) to a canonical path
    /// inside the root. Symlinks are followed before the check, so a link that
    /// points outside is rejected. Paths that do not exist yet are resolved
    /// through their deepest existing ancestor.
    pub fn resolve(&self, path: &str) -> Result<PathBuf, AccessError> {
        if path.trim().is_empty() {
            return Err(AccessError::Empty);
        }
        let requested = Path::new(path);
        let joined = if requested.is_absolute() {
            requested.to_path_buf()
        } else {
            self.root.join(requested)
        };

        let mut existing = joined.as_path();
        let mut missing = Vec::new();
        let base = loop {
            match existing.canonicalize() {
                Ok(canonical) => break canonical,
                Err(err) if err.kind() == io::ErrorKind::NotFound => {
                    let Some(name) = existing.file_name() else {
                        return Err(AccessError::Outside);
                    };
                    missing.push(name.to_owned());
                    existing = existing.parent().ok_or(AccessError::Outside)?;
                }
                Err(err) => return Err(AccessError::Io(err)),
            }
        };
        let mut resolved = base;
        for part in missing.iter().rev() {
            // `..` or `.` after a missing component cannot be resolved safely.
            if matches!(
                Path::new(part).components().next(),
                Some(Component::ParentDir | Component::CurDir)
            ) {
                return Err(AccessError::Outside);
            }
            resolved.push(part);
        }
        if !resolved.starts_with(&self.root) {
            return Err(AccessError::Outside);
        }
        let inside = resolved.strip_prefix(&self.root).unwrap_or(&resolved);
        if inside
            .components()
            .any(|c| is_sensitive(c.as_os_str().to_string_lossy().as_ref()))
        {
            return Err(AccessError::Sensitive);
        }
        Ok(resolved)
    }
}

fn is_sensitive(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    SENSITIVE_NAMES
        .iter()
        .any(|n| lower == *n || lower.starts_with(&format!("{n}.")))
        || SENSITIVE_PARTS.iter().any(|p| lower.contains(p))
        || SENSITIVE_EXTENSIONS.iter().any(|e| lower.ends_with(e))
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    pub(crate) fn temp_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "jarvs-test-{tag}-{}-{:?}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn resolves_relative_absolute_and_missing_paths() {
        let base = temp_dir("ws");
        let ws = Workspace::open(&base.join("ws")).unwrap();
        fs::write(ws.root().join("a.txt"), "x").unwrap();
        assert_eq!(ws.resolve("a.txt").unwrap(), ws.root().join("a.txt"));
        let abs = ws.root().join("a.txt");
        assert_eq!(ws.resolve(abs.to_str().unwrap()).unwrap(), abs);
        assert_eq!(
            ws.resolve("new/dir/b.txt").unwrap(),
            ws.root().join("new/dir/b.txt")
        );
    }

    #[test]
    fn rejects_traversal_and_absolute_escape() {
        let base = temp_dir("trav");
        let ws = Workspace::open(&base.join("ws")).unwrap();
        fs::write(base.join("secret.txt"), "x").unwrap();
        assert!(matches!(
            ws.resolve("../secret.txt"),
            Err(AccessError::Outside)
        ));
        assert!(matches!(
            ws.resolve("missing/../../secret.txt"),
            Err(AccessError::Outside)
        ));
        assert!(matches!(
            ws.resolve(base.join("secret.txt").to_str().unwrap()),
            Err(AccessError::Outside)
        ));
        assert!(matches!(ws.resolve(""), Err(AccessError::Empty)));
    }

    #[cfg(unix)]
    #[test]
    fn rejects_symlink_escape() {
        let base = temp_dir("link");
        let ws = Workspace::open(&base.join("ws")).unwrap();
        fs::write(base.join("secret.txt"), "x").unwrap();
        std::os::unix::fs::symlink(base.join("secret.txt"), ws.root().join("link")).unwrap();
        std::os::unix::fs::symlink(&base, ws.root().join("dirlink")).unwrap();
        assert!(matches!(ws.resolve("link"), Err(AccessError::Outside)));
        assert!(matches!(
            ws.resolve("dirlink/new.txt"),
            Err(AccessError::Outside)
        ));
    }

    #[test]
    fn rejects_sensitive_names() {
        let base = temp_dir("sens");
        let ws = Workspace::open(&base.join("ws")).unwrap();
        for name in [
            ".env",
            ".env.local",
            "gmail_token.json",
            "creds/credentials.json",
            "server.pem",
            ".ssh/config",
        ] {
            assert!(
                matches!(ws.resolve(name), Err(AccessError::Sensitive)),
                "{name}"
            );
        }
        assert!(ws.resolve("notes/environment.md").is_ok());
        assert!(ws.resolve(".gitignore").is_ok());
    }
}
