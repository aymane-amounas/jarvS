# jarvS roadmap

Each phase lists what it depends on and testable acceptance criteria. A phase is
done when its criteria pass in CI on Ubuntu, macOS and Windows, not when its files
exist. Progress lives in [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

| Phase | Depends on | Delivers |
|---|---|---|
| P0 Audit and preservation | n/a | Critical fixes, clean repo, docs |
| P1 Rust foundation | P0 | `jarvs` binary: config, paths, hardware, status |
| P2 Rust/Python bridge | P1 | Python agent runs under Rust; tools mediated |
| P3 Security and platform | P2 | Permission engine, approvals, keychain, emergency stop |
| P4 Providers and models | P1, P3 | Ollama + OpenAI-compatible, model manager, recommendations |
| P5 Messaging | P3 | Channel trait, Telegram, WhatsApp behind it, commands |
| P6 Missions and scheduling | P2, P5 | Rust scheduler, SQLite missions, legacy loop retired |
| P7 Automation engine | P6 | Triggers, actions, workflows, run history |
| P8 Product experience | P4–P7 | Local API, dashboard, onboarding, optional Tauri |
| P9 Native AI migration | P4, P8 | Port agent pieces to Rust where measurably better |
| P10 Multi-agent | P7, P9 | Agent profiles, controlled delegation |
| P11 Public release | all | Installers, upgrade path, security review |

## v0.2: Runtime Hardening
- Hardening milestone focused on trust, not autonomy: every security boundary is verified with an adversarial test.
- [x] Adversarial macOS smoke tests demonstrate representative file, environment, network, and process restrictions under Seatbelt.
- [x] Linux Bubblewrap backend is wired and fails closed if `bwrap` is unavailable; Linux execution still needs CI validation.
- [x] Custom-tool execution fails closed on Windows until a real OS isolation backend exists.
- [x] Workspace traversal and symlink escape tests reject outside paths.
- [x] IPC rejects oversized input, bounds queued messages, and has malformed-input corpus tests.
- [x] Approval decisions are one-time and cannot be replayed after consumption.
- [ ] Assess macOS sandbox escape resistance independently; smoke tests do not establish this.
- [ ] Verify Linux isolation on CI and implement Windows AppContainer or another supported OS boundary.
- [ ] Implement mission checkpoint/idempotency recovery; current coverage only closes interrupted run records.
- [ ] Full CI regression must pass on Ubuntu, macOS, and Windows.

## P0: Audit and preservation
- [x] `read_file`/`write_file` work; tests cover traversal and symlink escape.
- [x] Shell is off by default; no prompt implies it is sandboxed.
- [x] No hardcoded host paths or OS in prompts; per-OS venv paths.
- [x] Personal and runtime files untracked; generic `missions.example.yaml`.
- [x] History rewritten without personal files (authorized by the owner).
- [x] Initial Apache-2.0 LICENSE (later replaced), NOTICE, third-party review.

## P1: Rust foundation
- [x] `jarvs status | hardware | paths | config | tools` work with no config file and no Ollama.
- [x] Paths follow OS conventions and can be overridden.
- [x] Hardware report: OS, arch, CPU, RAM total/available, NVIDIA VRAM, Apple unified memory, disk.
- [x] CI workflow for Rust and Python on 3 OSes.
- [ ] CI has actually run green on all 3 OSes.

## P2: Rust/Python bridge
- [x] Versioned JSON-lines protocol with task IDs, structured errors, progress, ping.
- [x] Every agent tool call goes through the Rust gate; denials reach the agent as text.
- [x] Timeouts cancel, then kill; worker crash is detected and reported.
- [x] End-to-end: the real `graph` agent runs a tool via Rust (verified with a fake Ollama).
- [x] Long-lived supervisor with automatic restart and backoff (`jarvs daemon`).
- [x] Ctrl-C / SIGTERM send `task.cancel` instead of killing.
- [x] Legacy entry points mediated (`worker.py` gated; WhatsApp feeds the daemon's queue).
- [ ] Verified against a real Ollama model.

## P3: Security and platform
- [x] Approval flow: terminal prompt or queue (`jarvs approve`). Chat and dashboard answers: still to do.
- [x] `fetch`, `search_web`, `search_news` native in Rust, with private-network blocking.
- Custom Python tools: run only inside real OS isolation (Linux namespaces/seccomp, macOS sandbox profile, Windows AppContainer/Job objects), or stay approval-only and documented as high risk.
- Keyring-backed secrets; tokens never in `.env` or logs.
- Global emergency stop: a persistent flag the scheduler and gate check before every action.
- Per-run resource budgets (tool calls, wall time, tokens).
- *Accept:* security test suite (traversal, symlink, injection into args, permission bypass, notification-limit bypass) passes on 3 OSes.

## P4: Providers and models
- `Provider` trait: health, list models (`/api/tags`), metadata (`/api/show`), chat, streaming, timeouts, cancellation.
- Ollama and OpenAI-compatible implementations; LM Studio and llama.cpp via the latter.
- Model roles: main, fast, reasoning, embedding. One model for everything stays the default.
- Benchmarks: time to first token, tokens/s, failure rate. Never silently switch models.
- Download and delete only after explicit approval.
- *Accept:* tested against a mock HTTP server in CI; `llm.py` reads its model choice from the Rust config.

## P5: Messaging
- `Channel` trait: start, stop, send, receive, health, normalized messages.
- Telegram Bot API with long polling, allowlist, pairing code, rate limits.
- Existing Baileys bridge wrapped behind the trait. Documented as unofficial, with its policy risks.
- Commands: `/status /missions /pause /resume /limit /model /help /stop`.
- *Accept:* mock Bot API server tests; an unauthorized chat ID cannot run anything.

## P6: Missions and scheduling
- Rust-owned SQLite (rusqlite) with versioned migrations and a backup before each migration.
- Import `missions.yaml` and the legacy `state.db` without data loss.
- Mission CRUD from the CLI and chat; per-mission allowed tools, schedule, budget, notifications.
- Notification policy: levels, daily cap 0–30 counted across channels, quiet hours, digest, dedup.
- Retire the `worker.py` loop and `channels/wa.py` direct agent calls.
- *Accept:* scheduler restart-recovery test; notification cap test across two channels.

## P7–P11
Specified in the product brief. They will be broken into criteria like the above when P6 is done.
