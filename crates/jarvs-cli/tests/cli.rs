//! End-to-end: the real `jarvs` binary driving the real Python worker
//! (`selftest` agent, so no LLM or third-party Python packages are needed).

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::{Duration, Instant};

struct Env {
    root: PathBuf,
}

impl Env {
    fn new(tag: &str) -> Self {
        let root = std::env::temp_dir().join(format!(
            "jarvs-cli-{tag}-{}-{}",
            std::process::id(),
            Instant::now().elapsed().as_nanos()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("data/workspace")).unwrap();
        fs::create_dir_all(root.join("config")).unwrap();
        // Unanswered approvals are denied after 1s instead of 10 minutes.
        fs::write(
            root.join("config/config.toml"),
            "[agent]\napproval_timeout_secs = 1\n",
        )
        .unwrap();
        Self { root }
    }

    fn workspace(&self) -> PathBuf {
        self.root.join("data/workspace")
    }

    fn jarvs(&self, args: &[&str]) -> Output {
        self.command(args).output().expect("run jarvs")
    }

    fn command(&self, args: &[&str]) -> Command {
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let mut command = Command::new(env!("CARGO_BIN_EXE_jarvs"));
        command
            .args(args)
            .env("JARVS_CONFIG_DIR", self.root.join("config"))
            .env("JARVS_DATA_DIR", self.root.join("data"))
            .env("JARVS_PYTHON_DIR", repo)
            .env("JARVIS_HOME", self.root.join("agent-home"))
            .env_remove("JARVS_WORKSPACE");
        command
    }
}

impl Drop for Env {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn text(out: &Output) -> (String, String) {
    (
        String::from_utf8_lossy(&out.stdout).into(),
        String::from_utf8_lossy(&out.stderr).into(),
    )
}

#[test]
fn hardware_json_reports_memory_and_suggestion() {
    let env = Env::new("hw");
    let out = env.jarvs(&["hardware", "--json"]);
    assert!(out.status.success(), "{:?}", text(&out));
    let value: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert!(value["hardware"]["memory_total_bytes"].as_u64().unwrap() > 0);
    assert!(value["suggestion"]["max_billion_params"].as_f64().unwrap() > 0.0);
}

#[test]
fn status_and_paths_work_without_config_or_ollama() {
    let env = Env::new("status");
    let out = env.jarvs(&["status"]);
    let (stdout, stderr) = text(&out);
    assert!(out.status.success(), "{stderr}");
    fs::remove_file(env.root.join("config/config.toml")).unwrap();
    assert!(
        text(&env.jarvs(&["status"]))
            .0
            .contains("not created; using defaults")
    );
    assert!(stdout.contains("ollama:"));
    assert!(env.jarvs(&["config", "init"]).status.success());
    assert!(env.root.join("config/config.toml").is_file());
}

#[test]
fn worker_handshake_and_ping() {
    let env = Env::new("ping");
    let out = env.jarvs(&["worker"]);
    let (stdout, stderr) = text(&out);
    assert!(out.status.success(), "{stdout}{stderr}");
    assert!(stdout.contains("selftest"), "{stdout}");
}

#[test]
fn run_enforces_policy_on_every_tool() {
    let env = Env::new("run");
    fs::write(env.workspace().join("selftest.txt"), "workspace file").unwrap();
    fs::write(env.root.join("data/outside.txt"), "outside").unwrap();

    let out = env.jarvs(&["run", "--agent", "selftest", "hello"]);
    let (stdout, stderr) = text(&out);
    assert!(out.status.success(), "{stdout}{stderr}");
    assert!(stdout.contains("read_file: workspace file"), "{stdout}");
    assert!(
        stdout.contains("write_file: denied by jarvS policy: the owner did not approve"),
        "{stdout}"
    );
    assert!(stdout.contains("outside the jarvS workspace"), "{stdout}");
    assert!(stdout.contains("shell: denied by jarvS policy"), "{stdout}");
    assert!(!env.workspace().join("selftest-out.txt").exists());

    let out = env.jarvs(&[
        "run",
        "--agent",
        "selftest",
        "--allow",
        "write_file",
        "--allow",
        "shell",
        "granted",
    ]);
    let (stdout, _) = text(&out);
    assert_eq!(
        fs::read_to_string(env.workspace().join("selftest-out.txt")).unwrap(),
        "granted"
    );
    assert!(
        stdout.contains("shell: denied by jarvS policy"),
        "shell must stay denied: {stdout}"
    );

    let audit = fs::read_to_string(env.root.join("data/logs/audit.log")).unwrap();
    assert_eq!(audit.lines().count(), 10);
    assert!(
        !audit.contains("should-not-run"),
        "argument values must not be logged"
    );
}

#[test]
fn worker_crash_is_reported() {
    let env = Env::new("crash");
    let out = env.jarvs(&["run", "--agent", "selftest", "crash"]);
    let (_, stderr) = text(&out);
    assert!(!out.status.success());
    assert!(stderr.contains("exited unexpectedly"), "{stderr}");
}

#[test]
fn timeout_cancels_task() {
    let env = Env::new("timeout");
    let start = Instant::now();
    let out = env.jarvs(&["run", "--agent", "selftest", "--timeout", "2", "sleep"]);
    let (_, stderr) = text(&out);
    assert!(!out.status.success());
    assert!(stderr.contains("timed out"), "{stderr}");
    assert!(start.elapsed() < Duration::from_secs(15));
}

#[test]
fn unknown_agent_is_rejected() {
    let env = Env::new("agent");
    let out = env.jarvs(&["run", "--agent", "nope", "x"]);
    assert!(!out.status.success());
    assert!(text(&out).1.contains("unknown agent"));
}

#[test]
fn approval_queue_lets_owner_approve_from_another_process() {
    let env = Env::new("approve");
    fs::write(
        env.root.join("config/config.toml"),
        "[agent]\napproval_timeout_secs = 30\n",
    )
    .unwrap();
    let child = env
        .command(&["run", "--agent", "selftest", "queued"])
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(20);
    let id = loop {
        let out = text(&env.jarvs(&["approvals"])).0;
        if let Some(line) = out.lines().find(|l| l.contains("write_file")) {
            break line.split_whitespace().next().unwrap().to_string();
        }
        assert!(Instant::now() < deadline, "no pending approval appeared");
        std::thread::sleep(Duration::from_millis(100));
    };
    assert!(text(&env.jarvs(&["approve", &id])).0.contains("approved"));
    let out = child.wait_with_output().unwrap();
    assert!(out.status.success(), "{:?}", text(&out));
    assert_eq!(
        fs::read_to_string(env.workspace().join("selftest-out.txt")).unwrap(),
        "queued"
    );
    assert!(
        text(&env.jarvs(&["approvals"]))
            .0
            .contains("no pending approvals")
    );
    assert!(!env.jarvs(&["approve", "../../x"]).status.success());
}

#[cfg(unix)]
#[test]
fn ctrl_c_cancels_the_task_cleanly() {
    let env = Env::new("sigint");
    let mut child = env
        .command(&[
            "run",
            "--agent",
            "selftest",
            "--allow",
            "write_file",
            "sleep",
        ])
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    // The selftest writes its file, then loops until cancelled.
    let deadline = Instant::now() + Duration::from_secs(20);
    while !env.workspace().join("selftest-out.txt").exists() {
        assert!(Instant::now() < deadline, "task never started");
        std::thread::sleep(Duration::from_millis(50));
    }
    std::thread::sleep(Duration::from_millis(500));
    let status = Command::new("kill")
        .args(["-INT", &child.id().to_string()])
        .status()
        .unwrap();
    assert!(status.success());
    let start = Instant::now();
    while child.try_wait().unwrap().is_none() {
        assert!(
            start.elapsed() < Duration::from_secs(10),
            "did not stop after Ctrl-C"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
    let out = child.wait_with_output().unwrap();
    assert_eq!(out.status.code(), Some(130), "{:?}", text(&out));
    assert!(text(&out).1.contains("cancelled"));
}
