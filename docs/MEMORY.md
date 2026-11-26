# Memory and stored state

jarvS keeps state in two places: the Rust runtime's data directory and the
Python worker's legacy home. Everything is local files on your machine. Nothing
is sent anywhere. This document lists what is written, where, how long it stays,
and how to read, export, or delete it.

## Where it lives

The Rust runtime uses the OS data-local directory (override with
`JARVS_DATA_DIR`): `jarvs config show` prints the resolved paths.

| Path | Contents | Written by |
|---|---|---|
| `<data>/workspace/` | The only tree the agent's file tools may read or write | `write_file` through the gate |
| `<data>/logs/audit.log` | One JSON line per tool decision: tool name, argument *names*, path, decision, timestamp. No argument values | `audit.rs` |
| `<data>/logs/worker.log` | The Python worker's stderr | worker process |

The Python worker runs with `JARVIS_HOME` set by the runtime (standalone, it
defaults to the project directory, override with `JARVIS_HOME`).

| Path | Contents | Written by |
|---|---|---|
| `<home>/state.db` | SQLite: `runs` (history), `queue` (inbound jobs), `outbox` (replies) | `memory.py` |
| `<home>/journal/<mission>.md` | Per-mission Markdown notes the next run reads back | `memory.py` |
| `<home>/checkpoints.db` | LangGraph step checkpoints for in-flight tasks | `graph.py` |
| `<home>/logs/jarvs.log` | Rotating worker log (5 MB x 3) | `runtime.py` |
| `<home>/logs/commands.log` | Shell-tool command audit (only if the shell tool is enabled) | `tools/__init__.py` |
| `<home>/tools_pending/`, `<home>/tools_custom/` | Self-written tools awaiting review / approved | `tools/dynamic.py` |
| `<home>/missions.yaml` | Standing missions (also edited by `add_goal`) | you / `add_goal` |

## What is stored

- **Run history** (`runs`): mission id, source, task text, start and finish
  times, status, and a summary truncated to 4000 characters.
- **Queue and outbox**: task text and WhatsApp reply destinations, with a
  `taken`/`sent` flag and a lease timestamp for retry after a crash.
- **Journal**: the plan outcome of each run (what worked, what failed, the
  result up to 600 characters), appended under a timestamped heading.
- **Audit log**: decisions only. Argument values, file contents, and message
  bodies are never recorded. See `audit.rs`.

The journal and run summaries can contain task text and model output, so treat
them as you would any notes file.

## Retention and deletion

Nothing expires on its own. Records stay until you delete them.

- `runs`, `queue`, and `outbox` grow until pruned. There is no automatic
  rotation on the SQLite tables yet (planned; see [ROADMAP.md](ROADMAP.md)).
- `jarvs.log` rotates at 5 MB and keeps 3 backups. `audit.log`, `worker.log`,
  and `commands.log` are append-only and do not rotate.
- The journal is append-only Markdown; delete a mission's memory by removing its
  `.md` file.

To wipe everything: stop the daemon, then delete the runtime data directory and
the worker home shown by `jarvs config show`.

## Inspecting and exporting

- `jarvs status` summarizes recent runs and pending approvals.
- The read-only dashboard (`dashboard.py`) shows recent runs, pending tools, and
  the command log.
- Run history and the queue are plain SQLite:
  `sqlite3 <home>/state.db ".dump runs"` exports it.
- The journal and logs are plain text; copy the files directly.

## Trust note

The journal is read back into future prompts. It is the agent's own past
output, not external input, but it is still data: a tool call is still gated by
the Rust runtime regardless of what a journal entry suggests. Mission ids are
reduced to a safe filename before they reach disk, so a stray `..` in
`missions.yaml` cannot redirect a journal write outside the journal directory.
