//! Supervises one Python worker process: handshake, health checks, task
//! execution with tool mediation, timeouts, cancellation and shutdown.
//! The worker may crash or hang at any point; every wait here is bounded.

use std::fmt;
use std::fs::File;
use std::io::{self, BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::Value;

use crate::ipc::{self, FromWorker, PROTOCOL_VERSION, TaskStatus, ToWorker, ToolOutcome};

const CANCEL_GRACE: Duration = Duration::from_secs(5);
const POLL_SLICE: Duration = Duration::from_millis(200);
const MAX_IPC_LINE_BYTES: usize = 1_000_000;
const MAX_QUEUED_IPC_MESSAGES: usize = 128;

#[derive(Debug, Clone)]
pub struct WorkerSpec {
    pub python: PathBuf,
    pub python_dir: PathBuf,
    /// Value for the worker's legacy `JARVIS_HOME` (its state and missions).
    pub home: PathBuf,
    /// The worker's stderr (its logs) goes here; stdout is the protocol.
    pub log_file: Option<PathBuf>,
    pub startup_timeout: Duration,
}

#[derive(Debug)]
pub enum WorkerError {
    Spawn(std::io::Error),
    StartupTimeout,
    Protocol(String),
    Exited(Option<ExitStatus>),
    Timeout,
    /// Stopped by the owner (Ctrl-C, daemon shutdown).
    Cancelled,
}

impl fmt::Display for WorkerError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Spawn(err) => write!(f, "could not start the Python worker: {err}"),
            Self::StartupTimeout => f.write_str("Python worker did not become ready in time"),
            Self::Protocol(msg) => write!(f, "worker protocol error: {msg}"),
            Self::Exited(Some(status)) => write!(f, "Python worker exited unexpectedly ({status})"),
            Self::Exited(None) => f.write_str("Python worker exited unexpectedly"),
            Self::Timeout => f.write_str("task timed out and was cancelled"),
            Self::Cancelled => f.write_str("task cancelled"),
        }
    }
}

impl std::error::Error for WorkerError {}

enum Line {
    Message(FromWorker),
    Invalid(String),
    Eof,
}

fn read_bounded_line<R: BufRead>(reader: &mut R) -> io::Result<Option<Result<String, ()>>> {
    let mut bytes = Vec::new();
    let mut oversized = false;
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            if bytes.is_empty() && !oversized {
                return Ok(None);
            }
            break;
        }
        let newline = available.iter().position(|byte| *byte == b'\n');
        let consumed = newline.map_or(available.len(), |index| index + 1);
        let content_len = newline.unwrap_or(consumed);
        if !oversized {
            if bytes.len().saturating_add(content_len) > MAX_IPC_LINE_BYTES {
                oversized = true;
            } else {
                bytes.extend_from_slice(&available[..content_len]);
            }
        }
        reader.consume(consumed);
        if newline.is_some() {
            break;
        }
    }
    if oversized {
        return Ok(Some(Err(())));
    }
    if bytes.last() == Some(&b'\r') {
        bytes.pop();
    }
    Ok(Some(String::from_utf8(bytes).map_err(|_| ())))
}

#[derive(Debug, Clone)]
pub struct TaskRequest {
    pub id: String,
    pub agent: String,
    pub task: String,
    pub want: String,
    pub timeout: Duration,
}

#[derive(Debug, Clone)]
pub struct TaskOutcome {
    pub status: TaskStatus,
    pub answer: String,
    pub tool_calls: usize,
}

pub struct Worker {
    child: Child,
    stdin: ChildStdin,
    lines: Receiver<Line>,
    pub agents: Vec<String>,
}

impl Worker {
    pub fn spawn(spec: &WorkerSpec, extra_args: &[&str]) -> Result<Self, WorkerError> {
        let stderr = match &spec.log_file {
            Some(path) => {
                if let Some(dir) = path.parent() {
                    std::fs::create_dir_all(dir).map_err(WorkerError::Spawn)?;
                }
                Stdio::from(
                    File::options()
                        .create(true)
                        .append(true)
                        .open(path)
                        .map_err(WorkerError::Spawn)?,
                )
            }
            None => Stdio::null(),
        };
        let mut child = Command::new(&spec.python)
            .arg("-u")
            .arg(spec.python_dir.join("ipc_worker.py"))
            .args(extra_args)
            .current_dir(&spec.python_dir)
            .env("JARVIS_HOME", &spec.home)
            .env("JARVS_IPC", "1")
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONIOENCODING", "utf-8")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(stderr)
            .spawn()
            .map_err(WorkerError::Spawn)?;
        let stdin = child.stdin.take().expect("stdin piped");
        let stdout = child.stdout.take().expect("stdout piped");
        let (tx, lines) = mpsc::sync_channel(MAX_QUEUED_IPC_MESSAGES);
        thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            loop {
                let line = match read_bounded_line(&mut reader) {
                    Ok(Some(Ok(line))) => line,
                    Ok(Some(Err(()))) => {
                        if tx
                            .send(Line::Invalid(format!(
                                "IPC message exceeds {MAX_IPC_LINE_BYTES} bytes or is not UTF-8"
                            )))
                            .is_err()
                        {
                            return;
                        }
                        continue;
                    }
                    Ok(None) | Err(_) => break,
                };
                if line.trim().is_empty() {
                    continue;
                }
                let parsed = match ipc::decode(&line) {
                    Ok(msg) => Line::Message(msg),
                    Err(err) => Line::Invalid(format!(
                        "{err:?}: {}",
                        line.chars().take(200).collect::<String>()
                    )),
                };
                if tx.send(parsed).is_err() {
                    return;
                }
            }
            let _ = tx.send(Line::Eof);
        });

        let mut worker = Self {
            child,
            stdin,
            lines,
            agents: Vec::new(),
        };
        match worker.next(Instant::now() + spec.startup_timeout)? {
            Some(FromWorker::Ready {
                protocol, agents, ..
            }) if protocol == PROTOCOL_VERSION => {
                worker.agents = agents;
                Ok(worker)
            }
            Some(FromWorker::Ready { protocol, .. }) => Err(WorkerError::Protocol(format!(
                "worker speaks protocol {protocol}, runtime speaks {PROTOCOL_VERSION}"
            ))),
            Some(FromWorker::Error { message, .. }) => Err(WorkerError::Protocol(message)),
            Some(other) => Err(WorkerError::Protocol(format!(
                "expected ready, got {other:?}"
            ))),
            None => Err(WorkerError::StartupTimeout),
        }
    }

    fn send(&mut self, message: &ToWorker) -> Result<(), WorkerError> {
        writeln!(self.stdin, "{}", ipc::encode(message))
            .and_then(|()| self.stdin.flush())
            .map_err(|_| WorkerError::Exited(self.child.try_wait().ok().flatten()))
    }

    /// Next message before `deadline`; `None` on timeout.
    fn next(&mut self, deadline: Instant) -> Result<Option<FromWorker>, WorkerError> {
        let wait = deadline.saturating_duration_since(Instant::now());
        match self.lines.recv_timeout(wait) {
            Ok(Line::Message(msg)) => Ok(Some(msg)),
            Ok(Line::Invalid(text)) => Err(WorkerError::Protocol(text)),
            Ok(Line::Eof) | Err(RecvTimeoutError::Disconnected) => {
                let status = self.child.wait().ok();
                Err(WorkerError::Exited(status))
            }
            Err(RecvTimeoutError::Timeout) => Ok(None),
        }
    }

    pub fn is_alive(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }

    /// Round-trip health check.
    pub fn ping(&mut self, timeout: Duration) -> Result<Duration, WorkerError> {
        let nonce = format!(
            "{:x}",
            Instant::now().elapsed().as_nanos() ^ u128::from(std::process::id())
        );
        let start = Instant::now();
        self.send(&ToWorker::Ping {
            nonce: nonce.clone(),
        })?;
        loop {
            match self.next(start + timeout)? {
                Some(FromWorker::Pong { nonce: got }) if got == nonce => return Ok(start.elapsed()),
                Some(_) => continue,
                None => return Err(WorkerError::Timeout),
            }
        }
    }

    /// Run one task. `on_tool` decides every tool request; `on_progress`
    /// receives (stage, message). On timeout the task is cancelled, and the
    /// worker is killed if it does not stop within a short grace period.
    pub fn run_task(
        &mut self,
        request: &TaskRequest,
        mut on_tool: impl FnMut(&str, &Value) -> (ToolOutcome, String),
        mut on_progress: impl FnMut(&str, &str),
        cancel: &AtomicBool,
    ) -> Result<TaskOutcome, WorkerError> {
        let id = request.id.as_str();
        self.send(&ToWorker::TaskRun {
            id: request.id.clone(),
            agent: request.agent.clone(),
            task: request.task.clone(),
            want: request.want.clone(),
        })?;
        let deadline = Instant::now() + request.timeout;
        let mut tool_calls = 0;
        loop {
            if cancel.load(Ordering::Relaxed) {
                return Err(self.stop_task(id, WorkerError::Cancelled));
            }
            if Instant::now() >= deadline {
                return Err(self.stop_task(id, WorkerError::Timeout));
            }
            // Short slices so a cancel request is noticed promptly.
            let Some(message) = self.next(deadline.min(Instant::now() + POLL_SLICE))? else {
                continue;
            };
            match message {
                FromWorker::ToolRequest {
                    id: task_id,
                    call_id,
                    tool,
                    args,
                } if task_id == id => {
                    tool_calls += 1;
                    let (outcome, output) = on_tool(&tool, &args);
                    self.send(&ToWorker::ToolResponse {
                        call_id,
                        outcome,
                        output,
                    })?;
                }
                FromWorker::TaskProgress {
                    id: task_id,
                    stage,
                    message,
                } if task_id == id => on_progress(&stage, &message),
                FromWorker::TaskResult {
                    id: task_id,
                    status,
                    answer,
                } if task_id == id => {
                    return Ok(TaskOutcome {
                        status,
                        answer,
                        tool_calls,
                    });
                }
                FromWorker::Error {
                    id: Some(task_id),
                    code,
                    message,
                } if task_id == id => {
                    return Ok(TaskOutcome {
                        status: TaskStatus::Error,
                        answer: format!("{code}: {message}"),
                        tool_calls,
                    });
                }
                other => on_progress("ignored", &format!("{other:?}")),
            }
        }
    }

    /// Ask the worker to cancel `id`; kill it if it has not stopped within the
    /// grace period. Returns `reason` for the caller to report.
    fn stop_task(&mut self, id: &str, reason: WorkerError) -> WorkerError {
        if self.send(&ToWorker::TaskCancel { id: id.into() }).is_ok() {
            let grace = Instant::now() + CANCEL_GRACE;
            while let Ok(Some(message)) = self.next(grace) {
                match message {
                    FromWorker::TaskResult { id: task_id, .. } if task_id == id => {
                        return reason;
                    }
                    // A tool request racing the cancel gets a refusal.
                    FromWorker::ToolRequest { call_id, .. } => {
                        let _ = self.send(&ToWorker::ToolResponse {
                            call_id,
                            outcome: ToolOutcome::Denied,
                            output: "task cancelled".into(),
                        });
                    }
                    _ => {}
                }
            }
        }
        self.kill();
        reason
    }

    pub fn kill(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }

    /// Ask the worker to exit; kill it if it does not within `grace`.
    pub fn shutdown(mut self, grace: Duration) {
        if self.send(&ToWorker::Shutdown).is_ok() {
            let deadline = Instant::now() + grace;
            while Instant::now() < deadline {
                if !matches!(self.child.try_wait(), Ok(None)) {
                    return;
                }
                thread::sleep(Duration::from_millis(20));
            }
        }
        self.kill();
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        if matches!(self.child.try_wait(), Ok(None)) {
            self.kill();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn bounded_protocol_reader_discards_oversized_lines_and_resynchronizes() {
        let input = format!("{}\n{{\"v\":1}}\n", "x".repeat(MAX_IPC_LINE_BYTES + 1));
        let mut reader = Cursor::new(input.into_bytes());

        assert_eq!(read_bounded_line(&mut reader).unwrap(), Some(Err(())));
        assert_eq!(
            read_bounded_line(&mut reader).unwrap(),
            Some(Ok("{\"v\":1}".into()))
        );
        assert_eq!(read_bounded_line(&mut reader).unwrap(), None);
    }

    #[test]
    fn bounded_protocol_reader_rejects_invalid_utf8() {
        let mut reader = Cursor::new(vec![b'{', 0xff, b'}', b'\n']);
        assert_eq!(read_bounded_line(&mut reader).unwrap(), Some(Err(())));
    }
}
