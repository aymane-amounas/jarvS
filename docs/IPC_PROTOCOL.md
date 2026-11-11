# jarvS runtime ↔ Python worker protocol (v1)

Transport: the Rust runtime spawns `python -u ipc_worker.py` and exchanges **one JSON
object per line** on the worker's stdin/stdout, UTF-8. The worker's stderr is its log
(`<data>/logs/worker.log`). The worker redirects `print` output to stderr so nothing
else can reach the protocol stream.

Every message carries `"v": 1` and a `"type"`. A message with another version, or a line
that is not a JSON object, is answered with an `error` and ignored. Unknown fields are
ignored. Unknown types are errors.

Implementations: `crates/jarvs-core/src/ipc.rs` (types), `worker.rs` (runtime side),
`ipc_worker.py` (worker side).

## Lifecycle

1. Runtime spawns the worker with `JARVS_IPC=1`, `JARVIS_HOME=<legacy agent state dir>`.
2. Worker sends `ready`. The runtime waits at most 60 s, then fails with a startup timeout.
3. Any number of `task.run` exchanges, one at a time. `ping` works at any time, even mid-task.
4. Runtime sends `shutdown` and waits 3 s, then kills the process. Closing stdin has the same effect.

The worker is never assumed to be alive. EOF on its stdout, or a failed write, ends any
pending wait with `Exited(status)`.

## Messages

### Worker → runtime

| type | fields | meaning |
|---|---|---|
| `ready` | `protocol: 1`, `worker: str`, `agents: [str]` | Handshake; `agents` lists runnable agents (`graph`, `queue`, `selftest`) |
| `pong` | `nonce` | Reply to `ping` |
| `task.progress` | `id`, `stage`, `message` | Informational (`start`, `tool`) |
| `tool.request` | `id`, `call_id`, `tool`, `args: object` | The agent wants a tool; the worker blocks until `tool.response` |
| `task.result` | `id`, `status: ok\|incomplete\|error\|cancelled`, `answer` | Terminal message for a task |
| `error` | `id: str\|null`, `code`, `message` | Protocol-level problem (`bad_json`, `bad_version`, `unknown_type`) |

### Runtime → worker

| type | fields | meaning |
|---|---|---|
| `ping` | `nonce` | Health check |
| `task.run` | `id`, `agent`, `task`, `want: short\|report` | Start a task. `id` is unique per task |
| `task.cancel` | `id` | Stop the task at its next cancellation point |
| `tool.response` | `call_id`, `outcome: executed\|delegate\|denied`, `output` | Decision for a `tool.request` |
| `shutdown` | | Exit cleanly |

### Tool outcomes

- `executed`: the runtime ran the tool itself. `output` is the result.
- `delegate`: the owner approved it; the worker runs its own implementation.
  Only approval-only tools the policy marks `Delegate` can get this outcome. See ARCHITECTURE.md.
- `denied`: `output` explains why. The agent sees `denied by jarvS policy: <reason>`.

In IPC mode, the worker's **only** path to a tool is `tool.request`: `ipc_worker.py`
replaces `tools.run_tool` with the protocol call for the duration of a task. A worker
that gets no `tool.response` within 300 s treats the call as denied.

## Timeouts and cancellation

- The runtime gives each task a deadline (`agent.task_timeout_secs`, default 1800 s;
  `jarvs run --timeout`). At the deadline it sends `task.cancel`.
- The worker checks for cancellation before and after every tool call. Cancellation raises
  a `BaseException`, so the agent's `except Exception` handlers cannot swallow it.
  The worker then sends `task.result` with `status: cancelled`.
- If no result arrives within 5 s, the runtime kills the process. Tool requests that
  race the cancel are answered `denied`.
- A long LLM call has no cancellation point inside it. That case is covered by the kill.
- Ctrl-C/SIGTERM are handled by the runtime, which cancels over the protocol; the worker
  ignores SIGINT. If the runtime disappears (stdin closes), the worker cancels its task.

## Example

```
← {"v":1,"type":"ready","protocol":1,"worker":"jarvs-python/1","agents":["graph","selftest"]}
→ {"v":1,"type":"task.run","id":"t1","agent":"graph","task":"What OS am I on?","want":"short"}
← {"v":1,"type":"task.progress","id":"t1","stage":"tool","message":"get_system_state"}
← {"v":1,"type":"tool.request","id":"t1","call_id":"t1-1","tool":"get_system_state","args":{}}
→ {"v":1,"type":"tool.response","call_id":"t1-1","outcome":"executed","output":"{...}"}
← {"v":1,"type":"task.result","id":"t1","status":"ok","answer":"You are on macOS 27.2."}
→ {"v":1,"type":"shutdown"}
```

## Versioning

Breaking changes bump `v`. The runtime refuses a `ready` with a different `protocol`
value. Adding optional fields or new message types the other side may ignore is not
breaking.
