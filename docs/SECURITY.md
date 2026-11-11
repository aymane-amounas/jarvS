# Security boundaries

jarvS is under active hardening. The controls below reduce risk; they do not
make a model, worker, or operating system compromise-proof. In particular,
approval is authorization, not isolation.

## Custom Python tools

Custom-tool source is syntax-restricted and each run is placed in a fresh
temporary directory with a small environment. This validation is defense in
depth, not a sandbox boundary.

| Platform | Execution behavior | Current boundary and limitation |
|---|---|---|
| macOS | Uses `sandbox-exec` when present; otherwise custom-tool execution is disabled. | Seatbelt denies network access, restricts writes to the temporary work directory, removes inherited environment variables, and denies reads from the user's home and repository. It is deprecated by Apple. The adversarial test checks representative denied operations; it does not prove resistance to sandbox escapes or kernel vulnerabilities. |
| Linux | Uses Bubblewrap (`bwrap`) when present; otherwise custom-tool execution is disabled. | When the runner permits namespace setup, the process receives a private namespace, no network namespace access, no capabilities, read-only Python/runtime mounts, and a writable temporary work directory. If namespace setup is denied, the tool fails closed. A CI runner with Bubblewrap installed denied loopback setup (`Failed RTM_NEWADDR: Operation not permitted`), so Linux sandbox execution remains unverified. |
| Windows | Custom-tool execution is disabled. | No AppContainer or equivalent Windows isolation backend is implemented yet. |

Tools requiring approval that are not custom Python tools (for example package
installation and project checks) do not run inside this custom-tool sandbox.

## Workspace file access

The Rust workspace guard canonicalizes paths and rejects traversal, sensitive
names, and symlinks that resolve outside the workspace. Writes re-check the
resolved path after creating parents and replace via a temporary file. The
current implementation is not based on directory handles/openat-style
operations, so it is not claimed to eliminate every concurrent filesystem race.

## Runtime IPC

Worker protocol lines are limited to 1,000,000 bytes on the Rust side and
1,000,000 characters on the Python side. Rust bounds the reader queue and the
Python worker bounds queued commands. Oversized Rust-worker output is treated
as a protocol error; oversized Python-worker input is rejected and discarded
through the next newline so the stream can resynchronize. The worker is
supervised and killed on protocol errors.

These limits mitigate memory exhaustion from oversized or queued messages; they
are not a substitute for authenticating the process endpoint. IPC uses local
stdin/stdout pipes between the Rust runtime and the child process.

## Approval integrity

Approval decisions are created once and cannot overwrite an existing decision.
After a waiter consumes a decision, its pending request is removed, so a later
decision attempt for that request is rejected. Approval files are local state;
protection against another process with the same OS account and write access to
the data directory is not provided.

## Verification status

Adversarial tests cover path traversal and symlink escapes, malformed and
oversized IPC, one-time approval decisions, and representative macOS sandbox
denials. A mission-run record recovery test verifies interrupted runs can be
closed, but exactly-once recovery of external side effects is not implemented.
No macOS sandbox escape assessment or independent security audit has been
performed.
