use std::io::{BufRead, IsTerminal, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use clap::{Parser, Subcommand};
use jarvs_core::approvals::ApprovalStore;
use jarvs_core::audit::AuditLog;
use jarvs_core::config::Config;
use jarvs_core::ipc::TaskStatus;
use jarvs_core::permissions::{Decision, Policy, TOOLS};
use jarvs_core::tools::{ApprovalRequest, Approver, ToolGate};
use jarvs_core::web::Web;
use jarvs_core::worker::{TaskRequest, Worker, WorkerError, WorkerSpec};
use jarvs_core::workspace::Workspace;
use jarvs_platform::AppPaths;

#[derive(Parser)]
#[command(name = "jarvs", version, about = "jarvS: local-first AI agent runtime")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Show runtime, paths, agent and model-provider status.
    Status,
    /// Detect hardware and suggest a local model size.
    Hardware {
        #[arg(long)]
        json: bool,
    },
    /// Print where jarvS stores config, data, logs and the workspace.
    Paths,
    /// Show or create the configuration file.
    Config {
        #[command(subcommand)]
        action: ConfigAction,
    },
    /// List agent tools and the permission each currently has.
    Tools,
    /// Health-check the Python agent worker.
    Worker,
    /// Run one task through the agent, with tools mediated by jarvS policy.
    Run {
        task: String,
        /// Allow a tool that normally needs approval, for this run only.
        #[arg(long = "allow", value_name = "TOOL")]
        allow: Vec<String>,
        #[arg(long, default_value = "graph")]
        agent: String,
        /// short or report
        #[arg(long, default_value = "short")]
        want: String,
        /// Seconds before the task is cancelled (default from config).
        #[arg(long)]
        timeout: Option<u64>,
    },
    /// Run the agent loop: queued messages (e.g. WhatsApp), then due missions.
    /// Restarts the Python worker if it crashes; Ctrl-C or SIGTERM stops cleanly.
    Daemon,
    /// List tool calls waiting for the owner's approval.
    Approvals,
    /// Approve a pending tool call.
    Approve { id: String },
    /// Deny a pending tool call.
    Deny { id: String },
}

/// Set by Ctrl-C / SIGTERM. A second signal exits immediately.
static STOP: AtomicBool = AtomicBool::new(false);

#[derive(Subcommand)]
enum ConfigAction {
    Show,
    /// Write a default config file if none exists.
    Init,
}

struct Context {
    paths: AppPaths,
    config: Config,
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let _ = ctrlc::set_handler(|| {
        if STOP.swap(true, Ordering::SeqCst) {
            eprintln!("\nforced exit");
            std::process::exit(130);
        }
        eprintln!("\nstopping... (press Ctrl-C again to force)");
    });
    match run(cli) {
        Ok(code) => code,
        Err(message) => {
            eprintln!("error: {message}");
            ExitCode::FAILURE
        }
    }
}

fn run(cli: Cli) -> Result<ExitCode, String> {
    let paths = AppPaths::discover().map_err(|e| e.to_string())?;
    let config = Config::load(&paths.config_file()).map_err(|e| e.to_string())?;
    let ctx = Context { paths, config };
    match cli.command {
        Command::Status => status(&ctx),
        Command::Hardware { json } => hardware(json),
        Command::Paths => {
            println!(
                "{}",
                serde_json::to_string_pretty(&ctx.paths).unwrap_or_default()
            );
            println!("config file: {}", ctx.paths.config_file().display());
            println!(
                "workspace:   {}",
                ctx.config.workspace_dir(&ctx.paths).display()
            );
            Ok(ExitCode::SUCCESS)
        }
        Command::Config {
            action: ConfigAction::Show,
        } => {
            print!("{}", ctx.config.to_toml());
            Ok(ExitCode::SUCCESS)
        }
        Command::Config {
            action: ConfigAction::Init,
        } => config_init(&ctx),
        Command::Tools => {
            let policy = Policy::new(ctx.config.permissions.clone(), Vec::new());
            for spec in TOOLS {
                println!(
                    "{:<24} {:<10} {}",
                    spec.name,
                    label(policy.decide(spec.name)),
                    spec.capability
                );
            }
            let custom = jarvs_core::permissions::UNKNOWN;
            println!(
                "{:<24} {:<10} {}",
                "(custom tools)",
                label(policy.decide(custom.name)),
                custom.capability
            );
            Ok(ExitCode::SUCCESS)
        }
        Command::Worker => {
            let (mut worker, _) = start_worker(&ctx)?;
            let rtt = worker
                .ping(Duration::from_secs(5))
                .map_err(|e| e.to_string())?;
            println!(
                "worker ready; agents: {}; ping {} ms",
                worker.agents.join(", "),
                rtt.as_millis()
            );
            worker.shutdown(Duration::from_secs(3));
            Ok(ExitCode::SUCCESS)
        }
        Command::Run {
            task,
            allow,
            agent,
            want,
            timeout,
        } => run_task(&ctx, &task, allow, &agent, &want, timeout),
        Command::Daemon => daemon(&ctx),
        Command::Approvals => {
            let pending = approvals(&ctx).list();
            if pending.is_empty() {
                println!("no pending approvals");
            }
            for item in pending {
                println!(
                    "{}  {}  ({})  task {}",
                    item.id, item.summary, item.capability, item.task_id
                );
            }
            Ok(ExitCode::SUCCESS)
        }
        Command::Approve { id } => decide(&ctx, &id, true),
        Command::Deny { id } => decide(&ctx, &id, false),
    }
}

fn label(decision: Decision) -> &'static str {
    match decision {
        Decision::Allow => "allow",
        Decision::Approval => "approval",
        Decision::Deny => "deny",
    }
}

fn status(ctx: &Context) -> Result<ExitCode, String> {
    let hw = jarvs_platform::detect();
    println!("jarvS {}", env!("CARGO_PKG_VERSION"));
    println!(
        "os:          {} {} ({})",
        hw.os,
        hw.os_version.as_deref().unwrap_or(""),
        hw.arch
    );
    let config_file = ctx.paths.config_file();
    let config_state = if config_file.is_file() {
        ""
    } else {
        " (not created; using defaults)"
    };
    println!("config:      {}{config_state}", config_file.display());
    println!(
        "workspace:   {}",
        ctx.config.workspace_dir(&ctx.paths).display()
    );
    match ctx.config.python_dir() {
        Some(dir) => println!(
            "agent:       {} (python: {})",
            dir.display(),
            ctx.config.python_interpreter(&dir).display()
        ),
        None => println!(
            "agent:       not found; set agent.python_dir in the config or JARVS_PYTHON_DIR"
        ),
    }
    let host = &ctx.config.providers.ollama_host;
    let reachable = reachable(host);
    println!(
        "ollama:      {host} {}",
        if reachable {
            "reachable"
        } else {
            "not reachable (start Ollama or set providers.ollama_host)"
        }
    );
    Ok(ExitCode::SUCCESS)
}

/// TCP reachability only; model discovery arrives with the provider layer.
fn reachable(url: &str) -> bool {
    let authority = url
        .split("://")
        .last()
        .unwrap_or(url)
        .split('/')
        .next()
        .unwrap_or("");
    let with_port = if authority.contains(':') {
        authority.to_string()
    } else {
        format!("{authority}:80")
    };
    with_port
        .to_socket_addrs()
        .ok()
        .and_then(|mut addrs| addrs.next())
        .is_some_and(|addr| TcpStream::connect_timeout(&addr, Duration::from_millis(500)).is_ok())
}

fn hardware(json: bool) -> Result<ExitCode, String> {
    let hw = jarvs_platform::detect();
    let suggestion = jarvs_platform::suggest_model_size(&hw);
    if json {
        let out = serde_json::json!({"hardware": hw, "suggestion": suggestion});
        println!("{}", serde_json::to_string_pretty(&out).unwrap_or_default());
        return Ok(ExitCode::SUCCESS);
    }
    let gib = |b: u64| b as f64 / (1u64 << 30) as f64;
    println!(
        "os:        {} {} ({})",
        hw.os,
        hw.os_version.as_deref().unwrap_or(""),
        hw.arch
    );
    println!(
        "cpu:       {} ({} logical cores)",
        hw.cpu_brand.as_deref().unwrap_or("unknown"),
        hw.cpu_logical_cores
    );
    let available = hw
        .memory_available_bytes
        .map_or("unknown".into(), |b| format!("{:.1} GiB", gib(b)));
    println!(
        "memory:    {:.1} GiB total, {available} available",
        gib(hw.memory_total_bytes)
    );
    if hw.gpus.is_empty() {
        println!("gpu:       none detected (CPU inference)");
    }
    for gpu in &hw.gpus {
        let mem = gpu
            .memory_bytes
            .map_or(String::new(), |b| format!(", {:.0} GiB", gib(b)));
        println!("gpu:       {}{mem}", gpu.name);
    }
    if let (Some(total), Some(free)) = (hw.disk_total_bytes, hw.disk_available_bytes) {
        println!(
            "disk:      {:.0} GiB free of {:.0} GiB",
            gib(free),
            gib(total)
        );
    }
    println!(
        "\nsuggested model size: {}B-{}B parameters (based on {})",
        suggestion.min_billion_params, suggestion.max_billion_params, suggestion.basis
    );
    println!("{}", suggestion.caveat);
    Ok(ExitCode::SUCCESS)
}

fn config_init(ctx: &Context) -> Result<ExitCode, String> {
    let path = ctx.paths.config_file();
    if path.exists() {
        println!("config already exists: {}", path.display());
        return Ok(ExitCode::SUCCESS);
    }
    std::fs::create_dir_all(&ctx.paths.config_dir).map_err(|e| e.to_string())?;
    std::fs::write(&path, Config::default().to_toml()).map_err(|e| e.to_string())?;
    println!("wrote {}", path.display());
    Ok(ExitCode::SUCCESS)
}

fn approvals(ctx: &Context) -> ApprovalStore {
    ApprovalStore::new(ctx.paths.data_dir.join("approvals"))
}

fn decide(ctx: &Context, id: &str, allow: bool) -> Result<ExitCode, String> {
    let item = approvals(ctx).decide(id, allow)?;
    println!(
        "{} {}",
        if allow { "approved" } else { "denied" },
        item.summary
    );
    Ok(ExitCode::SUCCESS)
}

/// Approval-only tools ask the owner: on the terminal when there is one,
/// otherwise through the approval queue (`jarvs approve <id>`).
fn approver(ctx: &Context, interactive: bool) -> Approver {
    let timeout = Duration::from_secs(ctx.config.agent.approval_timeout_secs);
    if interactive {
        return Box::new(|req: &ApprovalRequest| {
            eprint!("\n  approve {} [{}]? [y/N] ", req.summary, req.capability);
            let _ = std::io::stderr().flush();
            let mut answer = String::new();
            let _ = std::io::stdin().lock().read_line(&mut answer);
            !STOP.load(Ordering::SeqCst) && matches!(answer.trim(), "y" | "Y" | "yes")
        });
    }
    let store = approvals(ctx);
    Box::new(move |req: &ApprovalRequest| {
        match store.submit(req.task_id, req.tool, req.capability, &req.summary) {
            Ok(pending) => {
                eprintln!(
                    "  approval needed: {}  -> `jarvs approve {id}` or `jarvs deny {id}` (waits {}s)",
                    pending.summary,
                    timeout.as_secs(),
                    id = pending.id
                );
                store.wait(&pending.id, timeout, &STOP)
            }
            Err(err) => {
                eprintln!("  approval queue unavailable: {err}");
                false
            }
        }
    })
}

fn build_gate(ctx: &Context, grants: Vec<String>, interactive: bool) -> Result<ToolGate, String> {
    Ok(ToolGate {
        policy: Policy::new(ctx.config.permissions.clone(), grants),
        workspace: Workspace::open(&ctx.config.workspace_dir(&ctx.paths))
            .map_err(|e| e.to_string())?,
        audit: AuditLog::new(ctx.paths.log_dir.join("audit.log")),
        web: Web::new(),
        approver: Some(approver(ctx, interactive)),
    })
}

fn start_worker(ctx: &Context) -> Result<(Worker, PathBuf), String> {
    let python_dir = ctx
        .config
        .python_dir()
        .ok_or("Python agent not found. Set agent.python_dir in the config or JARVS_PYTHON_DIR.")?;
    let spec = WorkerSpec {
        python: ctx.config.python_interpreter(&python_dir),
        // Legacy agent state (state.db, missions.yaml) stays where it was.
        home: std::env::var_os("JARVIS_HOME").map_or_else(|| python_dir.clone(), PathBuf::from),
        log_file: Some(ctx.paths.log_dir.join("worker.log")),
        startup_timeout: Duration::from_secs(60),
        python_dir: python_dir.clone(),
    };
    let worker = Worker::spawn(&spec, &[]).map_err(|e| {
        format!(
            "{e}\nworker log: {}",
            ctx.paths.log_dir.join("worker.log").display()
        )
    })?;
    Ok((worker, python_dir))
}

fn run_task(
    ctx: &Context,
    task: &str,
    allow: Vec<String>,
    agent: &str,
    want: &str,
    timeout: Option<u64>,
) -> Result<ExitCode, String> {
    let interactive = std::io::stdin().is_terminal() && std::io::stderr().is_terminal();
    let gate = build_gate(ctx, allow, interactive)?;
    let (mut worker, _) = start_worker(ctx)?;
    if !worker.agents.iter().any(|a| a == agent) {
        return Err(format!(
            "unknown agent {agent:?}; worker offers: {}",
            worker.agents.join(", ")
        ));
    }
    let id = task_id();
    let request = TaskRequest {
        id: id.clone(),
        agent: agent.into(),
        task: task.into(),
        want: want.into(),
        timeout: Duration::from_secs(timeout.unwrap_or(ctx.config.agent.task_timeout_secs)),
    };
    let outcome = worker.run_task(
        &request,
        |tool, args| {
            let (outcome, output) = gate.handle(&id, tool, args);
            eprintln!("  tool {tool}: {outcome:?}");
            (outcome, output)
        },
        |stage, message| {
            if stage != "tool" {
                eprintln!("  [{stage}] {message}");
            }
        },
        &STOP,
    );
    let result = match outcome {
        Ok(outcome) => {
            println!("{}", outcome.answer);
            eprintln!(
                "status: {:?}, {} tool call(s), task {id}",
                outcome.status, outcome.tool_calls
            );
            Ok(if outcome.status == TaskStatus::Ok {
                ExitCode::SUCCESS
            } else {
                ExitCode::from(2)
            })
        }
        Err(WorkerError::Cancelled) => {
            eprintln!("cancelled");
            Ok(ExitCode::from(130))
        }
        Err(err) => Err(err.to_string()),
    };
    worker.shutdown(Duration::from_secs(3));
    result
}

const DAEMON_IDLE_POLL: Duration = Duration::from_secs(20);
const MAX_BACKOFF: Duration = Duration::from_secs(60);
const STABLE_AFTER: Duration = Duration::from_secs(300);

/// Sleeps up to `duration`; returns false if a stop was requested.
fn pause(duration: Duration) -> bool {
    let end = Instant::now() + duration;
    while Instant::now() < end {
        if STOP.load(Ordering::SeqCst) {
            return false;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    !STOP.load(Ordering::SeqCst)
}

fn log(message: &str) {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    eprintln!("[{secs}] {message}");
}

/// Owns the scheduling loop: each step asks the worker's `queue` agent to run
/// the next queued job or due mission, with every tool call mediated here.
fn daemon(ctx: &Context) -> Result<ExitCode, String> {
    let gate = build_gate(ctx, Vec::new(), false)?;
    let mut backoff = Duration::from_secs(1);
    log("jarvS daemon started");
    while !STOP.load(Ordering::SeqCst) {
        let mut worker = match start_worker(ctx) {
            Ok((worker, _)) => worker,
            Err(err) => {
                log(&format!(
                    "worker failed to start: {err}; retrying in {}s",
                    backoff.as_secs()
                ));
                if !pause(backoff) {
                    break;
                }
                backoff = (backoff * 2).min(MAX_BACKOFF);
                continue;
            }
        };
        let started = Instant::now();
        log("worker ready");
        loop {
            let id = task_id();
            let request = TaskRequest {
                id: id.clone(),
                agent: "queue".into(),
                task: String::new(),
                want: "short".into(),
                timeout: Duration::from_secs(ctx.config.agent.task_timeout_secs),
            };
            let result = worker.run_task(
                &request,
                |tool, args| {
                    let (outcome, output) = gate.handle(&id, tool, args);
                    log(&format!("tool {tool}: {outcome:?}"));
                    (outcome, output)
                },
                |stage, message| {
                    if stage != "tool" {
                        log(&format!("[{stage}] {message}"));
                    }
                },
                &STOP,
            );
            match result {
                Ok(outcome) if matches!(outcome.answer.as_str(), "idle" | "rest") => {
                    if !pause(DAEMON_IDLE_POLL) {
                        break;
                    }
                }
                Ok(outcome) => log(&format!(
                    "step finished: {:?} {}",
                    outcome.status, outcome.answer
                )),
                Err(WorkerError::Cancelled) => break,
                Err(err) => {
                    log(&format!("worker failed: {err}; restarting"));
                    break;
                }
            }
            if STOP.load(Ordering::SeqCst) {
                break;
            }
        }
        worker.shutdown(Duration::from_secs(5));
        if STOP.load(Ordering::SeqCst) {
            break;
        }
        if started.elapsed() > STABLE_AFTER {
            backoff = Duration::from_secs(1);
        }
        log(&format!("restarting worker in {}s", backoff.as_secs()));
        if !pause(backoff) {
            break;
        }
        backoff = (backoff * 2).min(MAX_BACKOFF);
    }
    log("jarvS daemon stopped");
    Ok(ExitCode::SUCCESS)
}

fn task_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos());
    format!("{:x}-{:x}", nanos, std::process::id())
}
