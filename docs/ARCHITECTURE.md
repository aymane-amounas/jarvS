# jarvS architecture

jarvS is moving from a Python prototype to a Rust runtime that hosts the existing
Python agent. Rust owns everything sensitive or long-lived. Python keeps only the
agent reasoning it already does well.

```
            ┌──────────────────────── jarvs (Rust) ─────────────────────────┐
 CLI ──────▶│ config ─ platform (paths, hardware) ─ permissions ─ audit log │
 (later:    │                      │                                         │
 API, chat) │              worker supervisor ── ToolGate ── workspace guard  │
            └──────────────────────┼─────────────────────────────────────────┘
                     JSON lines on stdin/stdout (docs/IPC_PROTOCOL.md)
            ┌──────────────────────┼──────── ipc_worker.py (Python) ──────────┐
            │  graph agent (LangGraph) ── every tool call ──▶ tool.request    │
            └─────────────────────────────────────────────────────────────────┘
```

## Workspace layout

| Path | Contents |
|---|---|
| `crates/jarvs-platform` | OS conventions: app directories (`directories`), hardware and model-size suggestion (`sysinfo`, `nvidia-smi`) |
| `crates/jarvs-core` | Config (TOML), permission policy, workspace guard, audit log, IPC types, worker supervisor, native tools |
| `crates/jarvs-cli` | The `jarvs` binary |
| `ipc_worker.py` | Python side of the bridge; hosts the `graph` agent and a dependency-free `selftest` agent |
| other `*.py`, `tools/`, `channels/` | The original Python implementation, preserved |

There are three crates, not the twelve sketched in the vision document. A crate is split
off when a second consumer needs it (the API server, a Tauri shell), not before.

## Ownership during the migration

Every responsibility has exactly one owner.

| Responsibility | Owner now | Target |
|---|---|---|
| Tool permission decisions | **Rust** (`permissions.rs`) | Rust |
| Workspace confinement for file tools | **Rust** (`workspace.rs`) | Rust |
| File read/write, system state | **Rust** (native tools) | Rust |
| Web search, news search, fetch | **Rust** (`web.rs`; local/private addresses blocked on every redirect hop) | Rust |
| Custom tools, package install, project checks, add_goal, notify_owner | Python after approval; custom tools use the platform backend in [SECURITY.md](SECURITY.md), other delegated tools retain account rights | Port or isolate (P3) |
| Owner approvals | **Rust** (`approvals.rs`): terminal prompt, or queue answered with `jarvs approve` | Rust; dashboard/chat later |
| Shell | Nobody: denied, no override | Only with real OS isolation |
| App paths, hardware detection | **Rust** | Rust |
| Agent planning/reasoning (LangGraph) | Python | Python until P9 shows a measurable reason to port |
| LLM calls | Python (`llm.py`) | Rust provider layer (P4) |
| Scheduling loop, worker restarts | **Rust** (`jarvs daemon`) drives each step | Rust |
| Queue/mission selection logic | Python (`worker.run_next`, `memory.py`), always run under the gate | Rust (P6) |
| WhatsApp | Python + Node (Baileys) | Same code behind the Rust channel interface (P5) |

**Delegation is limited to approval-only tools.** Every delegated tool requires the
owner's explicit yes per call. Except for custom tools, approved delegated tools still
run in Python with the account's rights. Custom Python tools use the platform-specific
isolation described in [SECURITY.md](SECURITY.md); execution fails closed where no
backend is available.

**All agent entry points are mediated.** `jarvs run` runs one task; `jarvs daemon`
runs the queue/mission loop that WhatsApp messages feed (`channels/wa.py` only enqueues).
`worker.py` refuses to run the agent itself unless `JARVIS_LEGACY_UNMEDIATED=1` is set
as a deliberate escape hatch.

## Paths

`AppPaths::discover()` follows each OS's conventions:

| | macOS | Linux | Windows |
|---|---|---|---|
| config | `~/Library/Application Support/dev.jarvS.jarvs` | `~/.config/jarvs` | `%APPDATA%\jarvS\jarvs\config` |
| data, logs, workspace | `~/Library/Application Support/dev.jarvS.jarvs` | `~/.local/share/jarvs` | `%LOCALAPPDATA%\jarvS\jarvs\data` |

`JARVS_CONFIG_DIR` and `JARVS_DATA_DIR` override them (portable installs, tests). The agent's
workspace defaults to `<data>/workspace`, separate from jarvS's own files. Legacy agent
state (`state.db`, `missions.yaml`) stays in `JARVIS_HOME` until P6 migrates it.

## Dependencies chosen (and not)

| Crate | Why |
|---|---|
| `clap` | CLI parsing; standard, derive API |
| `ureq` | Blocking HTTP for native web tools; small, rustls by default, no async runtime needed |
| `ctrlc` (with `termination`) | Ctrl-C and SIGTERM handling for cancel and daemon shutdown |
| `serde`, `serde_json`, `toml` | Config and IPC; no alternative worth considering |
| `sysinfo` (features `system`, `disk` only) | Cross-platform RAM/CPU/disk |
| `directories` | OS application directories |

Deferred until a real need exists: **Tokio/Axum** (the CLI is synchronous; added with the
local API in P4/P8), **rusqlite** (with Rust-owned storage in P6), **reqwest** (only if the async provider layer in P4 needs it), **tracing** (the JSON-lines audit log covers current needs),
**keyring** (P3), **Rig** (P9, only if measurably better), **Tauri** (P8, after the API is stable).
