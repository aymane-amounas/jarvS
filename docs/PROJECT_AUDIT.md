# jarvS project audit

Audit date: 2026-11-08. Scope: every tracked file, Git history (1 commit), and the
running code on macOS 27.2 (arm64). Findings were verified against the source, not
taken from earlier reviews.

## Prior review findings: verified

| # | Finding | Verified? | Status |
|---|---|---|---|
| 1 | `_within_home()` had no successful return | Yes. `run_project_checks` was pasted inside it, so `return target` ended up in the wrong function. `read_file`/`write_file` raised `AttributeError` on every valid path. | Fixed; regression tests added |
| 2 | `shell()` used `subprocess.run(shell=True)` with no isolation | Yes | Disabled by default (`JARVIS_ALLOW_SHELL`). The Rust policy denies it outright, with no override |
| 3 | Ubuntu and personal paths in prompts | Yes (`/home/jarvis/Jarvis`, "standard Ubuntu bash") | Prompts are now built from detected OS, home, and shell |
| 4 | `.venv/bin/python`/`pip` hardcoded | Yes (tools, sandbox runner) | `runtime.venv_bin()` resolves per OS |
| 5 | `missions.yaml` declared `fully_autonomous` | Yes | Untracked; `missions.example.yaml` uses `supervised` |
| 6 | Only 3 tests, none on critical paths | Yes | Now 23 Python and 29 Rust tests |
| 7 | Gmail/browser not wired into the worker | Yes. `gmail_setup.py` exists, no Gmail tool is registered, Playwright is installed but unused | Unchanged. Documented as not implemented |
| 8 | Personal/runtime files tracked | Yes: `bookmarks.html`, `checkpoints.db-wal/-shm`, `missions.yaml`, plus `analysis_results.txt` and `mt5_gold_data_fetcher.sh` (agent output) | Untracked and ignored; still present in history (see below) |

Additional findings:

- `run_project_checks` was listed in the agent prompt but missing from the tool registry. Fixed.
- RAM detection reported nothing on macOS: `SC_AVPHYS_PAGES` raised, wiping both values. Fixed.
- Custom-tool source validation is not a sandbox. Custom tools now fail closed unless a platform isolation backend is available: Seatbelt on macOS and Bubblewrap on Linux; Windows execution remains disabled. See [SECURITY.md](SECURITY.md) for the limits and test evidence.
- Several agent safety rules (autonomy policy, "never use sudo") existed only as prompt text. The Rust gate now enforces the tool-level ones.

## Git history and secrets

- Scanned all history for API-key, token, Telegram bot-token, Google key, and Slack token patterns: **0 matches**. The only WhatsApp JID matches are code that checks JID suffixes.
- With the owner's authorization, history was rewritten to a single initial commit without `bookmarks.html`, the personal `missions.yaml`, the SQLite WAL/SHM files, or agent output files. Those local files were moved to the macOS Trash.
- License: none existed. The sole author (Aymane Amounas) chose Apache-2.0.

## Existing capabilities (Python)

| Module | What it does | Keep? |
|---|---|---|
| `graph.py` | LangGraph agent: decide → plan → act ↔ check → reflect → report; SQLite checkpoints | Yes. It is the agent; runs under the Rust bridge |
| `memory.py` | SQLite runs, queue, outbox; markdown journal with keyword recall | Yes for now; storage moves to Rust in P6 |
| `worker.py` | Single-job loop: WhatsApp queue first, then self-chosen missions within a work/rest cycle | Scheduling moves to Rust (P6); job execution reused by `ipc_worker.py` |
| `llm.py` | Ollama chat with plan/fast model profiles | Replaced by the Rust provider layer (P4) |
| `tools/` | Tool registry, file tools, search, fetch, test-gated custom tools | Execution migrates tool by tool; the policy already lives in Rust |
| `channels/wa.py` + `bridge/` | WhatsApp via Baileys (Node), allowlist, voice-note transcription | Keep behind the channel interface (P5) |
| `dashboard.py` | Read-only FastAPI page: runs, pending tools, command log | Superseded by the Rust API and dashboard (P8) |
| `rss.py`, `gmail_setup.py` | Helpers; Gmail OAuth only | Keep; Gmail is not wired up |

## Security weaknesses still open

1. Approval-only delegated tools other than custom Python tools still execute with the account's rights. Custom-tool sandbox escape resistance has not been independently assessed.
2. No approval UI: tools marked "approval" can only be granted per run with `jarvs run --allow`.
3. The legacy `worker.py` loop and `channels/wa.py` still run the agent without the Rust gate. Only `jarvs run` is mediated.
4. Fetched web content goes into prompts unfiltered; the prompt-injection defence is the tool gate alone.
5. Credentials (`.env`, Gmail token, WhatsApp session) are plain files, not in an OS keychain.

## Platform dependencies

- Linux-only: `install.sh`, `deploy.sh` (apt, systemd, `sed -i`), `systemd/*.service`.
- Cross-platform now: `runtime.py`, `tools/`, prompts, and the whole Rust workspace.
- macOS sandbox behavior has local adversarial smoke tests; these do not establish sandbox escape resistance.
- Linux isolation uses Bubblewrap when installed and still needs CI validation. Windows custom-tool execution is disabled pending an AppContainer or equivalent backend.
- The latest macOS CI attempt failed before project tests because the runner could not resolve PyPI and GitHub action archive hosts; the run is not evidence of a source failure or a passing macOS check.

## Test results (2026-11-08, macOS arm64)

| Suite | Result |
|---|---|
| `cargo test --all` | 29 passed (platform 6, core 16, CLI end-to-end 7) |
| `cargo clippy --all-targets -- -D warnings` | clean |
| `cargo fmt --check` | clean |
| `pytest -q tests` (with requirements-dev) | 23 passed |
| Graph agent via `jarvs run` against a fake Ollama server | plan → `get_system_state` executed by Rust → report: passed |
| Graph agent against real Ollama | not run (Ollama not installed on the audit machine) |
