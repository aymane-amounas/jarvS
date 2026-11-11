# Implementation status

Only what was actually run is recorded as tested.

## v0.2 runtime hardening (in progress)

### Implemented and locally tested

| Area | Result |
|---|---|
| Custom Python tools | macOS Seatbelt and Linux Bubblewrap backends; custom-tool execution fails closed if no backend is available. Windows remains disabled. |
| macOS adversarial smoke test | Local test confirms denial of reads from a protected host path, writes outside the work directory, inherited environment secrets, network operations, and subprocess creation. This is not a sandbox escape assessment. |
| IPC resource limits | Rust and Python cap individual protocol lines; Rust output buffering and Python pending commands are bounded. Python malformed-input and oversized-line tests, Rust malformed corpus and bounded-reader tests pass. |
| Approval replay | Decisions use exclusive creation and consumed requests cannot be approved again; replay regression test passes. |
| Mission queue recovery | Lease-expiry retry and acknowledgement tests pass. Exactly-once external side effects are not guaranteed. |

### Not complete

- Linux CI installed Bubblewrap but the runner denied network namespace setup (`Failed RTM_NEWADDR: Operation not permitted`); sandbox execution was therefore not exercised there.
- Python CI on Ubuntu/macOS/Windows also reported a Ruff import-order issue in the adversarial sandbox test; that import block is now corrected and passes the CI Ruff command locally.
- Windows has no OS isolation backend; custom Python tool execution fails closed.
- No independent macOS sandbox escape assessment or kernel-level adversarial review has been performed.
- Mission checkpointing/idempotency for work interrupted after external side effects remains unimplemented.
- The latest macOS CI attempt failed during dependency/action downloads because hosted runners could not resolve PyPI and GitHub archive hosts; macOS CI remains unverified.

## Milestone 2: gaps closed, license, brand (2026-11-08)

### Works and is tested

| Feature | Where | Tested by |
|---|---|---|
| Native `fetch` (http/https only, private/loopback/metadata IPs blocked on every redirect hop, 1 MB cap) | `jarvs-core/web.rs` | unit tests (local redirect server, address classes) + live test |
| Native `search_web`, `search_news` (DuckDuckGo) | `web.rs` | parser unit tests + live test (`cargo test -- --ignored live_search`) |
| Owner approvals: terminal prompt, or a queue answered with `jarvs approvals/approve/deny` | `approvals.rs`, CLI | unit tests + `approval_queue_lets_owner_approve_from_another_process` |
| Ctrl-C / SIGTERM cancel the task over the protocol (exit 130); a second Ctrl-C forces exit | CLI, `worker.rs`, `ipc_worker.py` | `ctrl_c_cancels_the_task_cleanly` (Unix) |
| `jarvs daemon`: queue/mission loop through the gate, worker restart with backoff (1 s, doubling to 60 s) | CLI, `ipc_worker.py` `queue` agent, `worker.run_next` | manual smoke test: queued job ran, `get_system_state` executed by Rust, SIGKILLed worker restarted, SIGTERM clean exit, no orphan |
| `worker.py` refuses to run the agent unmediated | `worker.py` | manual run (exit 2 with guidance) |
| systemd unit and installers run `jarvs daemon` | `systemd/`, `install.sh`, `deploy.sh` | `bash -n` only (no Linux host run) |
| Initial Apache-2.0 license, later replaced by the Aymane Amounas Noncommercial Attribution License 1.0; `NOTICE` and license metadata | repo root, `Cargo.toml`, `package.json` | n/a |
| Third-party license review | `docs/THIRD_PARTY.md` | `cargo metadata`, Python package metadata |
| Brand mark | `assets/logo.svg` + 512/192/32 px PNG | rendered; dashboard header and favicon checked in a browser |

### Exact results (macOS 27.2, arm64)

```
cargo test --all                    41 passed, 1 ignored (live network)
cargo test -- --ignored live_search 1 passed
cargo clippy --all-targets -D warnings  clean
cargo fmt --check                   clean
pytest -q tests                     23 passed
```

### Still open
- Windows/Linux: CI runs on push; see the GitHub Actions results.
- Approval-only delegated tools (custom tools, package install, project checks) run in Python with full rights once approved; real OS isolation is P3.
- The approval queue has no push notification yet; the owner has to check `jarvs approvals` (the daemon logs each request).
- DNS rebinding: an address is checked before the request, and ureq resolves again when it connects.


## Milestone 1: P0 fixes, Rust foundation, Python bridge (2026-11-08)

### Works and is tested

| Feature | Where | Tested by |
|---|---|---|
| File tools confined to home (Python, legacy path) | `tools/__init__.py` | `tests/test_foundation.py`: valid, missing, traversal, symlink |
| Shell off by default | `tools/__init__.py` | `test_foundation.py` |
| Cross-platform paths/RAM (Python) | `runtime.py` | `test_foundation.py` |
| OS application directories | `jarvs-platform/paths.rs` | unit tests |
| Hardware detection + model-size suggestion | `jarvs-platform/hardware.rs` | unit tests (tiers, GPU, nvidia-smi parsing), live run |
| TOML config with env overrides | `jarvs-core/config.rs` | unit tests |
| Permission policy (allow/approval/deny; shell locked) | `jarvs-core/permissions.rs` | unit tests |
| Workspace guard (traversal, absolute, symlink, sensitive names) | `jarvs-core/workspace.rs` | unit tests |
| Native tools: read/write file, system state | `jarvs-core/tools.rs` | unit tests |
| Audit log (no argument values) | `jarvs-core/audit.rs` | unit + CLI tests |
| IPC v1 types | `jarvs-core/ipc.rs` | unit tests |
| Worker supervisor: handshake, ping, task, timeout→cancel→kill, crash | `jarvs-core/worker.rs` | `jarvs-cli/tests/cli.rs` (real binary + real Python) |
| Python worker: routing, cancel, errors, shutdown | `ipc_worker.py` | `tests/test_ipc_worker.py` |
| `jarvs` CLI: status, hardware, paths, config, tools, worker, run | `jarvs-cli` | `cli.rs` + manual runs |
| LangGraph agent under Rust with a mediated tool call | `ipc_worker.py` `graph` agent | manual run against a fake Ollama server |

### Exact results (macOS 27.2, arm64, Rust 1.99.1, Python 3.14.3)

```
cargo test --all                          29 passed, 0 failed
cargo clippy --all-targets -- -D warnings clean
cargo fmt --all --check                   clean
pytest -q tests  (requirements-dev venv)  23 passed
pytest -q tests  (system Python, no LangGraph) 22 passed, 1 skipped
ruff check runtime.py ipc_worker.py tests clean
python -m compileall -q .                 clean
jarvs run "What OS am I on?" (fake Ollama) status Ok, 1 tool call executed by Rust
```

**Not verified:** Windows and Linux (CI is written but has not run), and real Ollama inference.

### Known gaps (as of milestone 1; most were closed in milestone 2)
- `install.sh`/`deploy.sh`/systemd remain Linux-only; there is no Rust installer yet.

### Next step
P2 remainder, then P3: long-lived supervisor with restart and backoff, a Ctrl-C → `task.cancel` handler,
then port `fetch`/`search_web`/`search_news` to native Rust so research stops running delegated.
