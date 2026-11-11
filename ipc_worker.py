"""jarvS Python worker: runs the existing LangGraph agent under the Rust runtime.

Speaks the line-delimited JSON protocol in docs/IPC_PROTOCOL.md on stdin/stdout.
Every agent tool call is sent to the runtime, which decides whether it runs;
this process never executes a tool the runtime has not approved.
"""
from __future__ import annotations

import json
import queue
import signal
import sys
import threading
import traceback
from collections.abc import Callable
from typing import Any

PROTOCOL = 1
WORKER = "jarvs-python/1"
TOOL_RESPONSE_TIMEOUT = 300
MAX_MESSAGE_BYTES = 1_000_000
MAX_PENDING_COMMANDS = 128


class Cancelled(BaseException):
    """Raised inside a task when the runtime cancels it.

    BaseException so the agent's broad `except Exception` handlers cannot
    swallow it and keep working after a cancel.
    """


class Channel:
    """Owns the protocol stream. A reader thread answers pings and routes
    cancels and tool responses immediately, even while a task is running."""

    def __init__(self, inp, out):
        self._out = out
        self._lock = threading.Lock()
        self.commands: queue.Queue[dict | None] = queue.Queue(maxsize=MAX_PENDING_COMMANDS)
        self._responses: dict[str, queue.Queue[dict]] = {}
        self._responses_lock = threading.Lock()
        self.cancelled: set[str] = set()
        self._reader = threading.Thread(target=self._read, args=(inp,), daemon=True)

    def start(self) -> None:
        self._reader.start()

    def send(self, message: dict) -> None:
        line = json.dumps({"v": PROTOCOL, **message}, ensure_ascii=False)
        with self._lock:
            self._out.write(line + "\n")
            self._out.flush()

    def _read(self, inp) -> None:
        while True:
            raw = inp.readline(MAX_MESSAGE_BYTES + 2)
            if not raw:
                break
            oversized = len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES
            if not raw.endswith("\n") and len(raw) >= MAX_MESSAGE_BYTES + 2:
                while raw and not raw.endswith("\n"):
                    raw = inp.readline(MAX_MESSAGE_BYTES + 2)
                oversized = True
            if oversized:
                self.send({"type": "error", "id": None, "code": "bad_size",
                           "message": f"message exceeds {MAX_MESSAGE_BYTES} bytes"})
                continue
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except ValueError as exc:
                message = f"invalid JSON: {exc}"
            if not isinstance(message, dict):
                self.send({"type": "error", "id": None, "code": "bad_json",
                           "message": message if isinstance(message, str) else "message must be an object"})
                continue
            if message.get("v") != PROTOCOL:
                self.send({"type": "error", "id": None, "code": "bad_version",
                           "message": f"expected protocol {PROTOCOL}"})
                continue
            kind = message.get("type")
            if kind == "ping":
                self.send({"type": "pong", "nonce": message.get("nonce", "")})
            elif kind == "task.cancel":
                self.cancelled.add(str(message.get("id")))
            elif kind == "shutdown":
                self.commands.put_nowait(message)
            elif kind == "tool.response":
                with self._responses_lock:
                    waiter = self._responses.get(str(message.get("call_id")))
                if waiter is not None:
                    try:
                        waiter.put_nowait(message)
                    except queue.Full:
                        continue
            else:
                if self.commands.qsize() >= MAX_PENDING_COMMANDS - 1:
                    self.send({"type": "error", "id": None, "code": "command_overflow",
                               "message": "worker command queue is full"})
                    continue
                try:
                    self.commands.put_nowait(message)
                except queue.Full:
                    self.send({"type": "error", "id": None, "code": "command_overflow",
                               "message": "worker command queue is full"})
        self.cancelled.add("*")  # runtime is gone: stop whatever is running
        self.commands.put(None)

    def request_tool(self, task_id: str, call_id: str, tool: str, args: dict) -> dict:
        waiter: queue.Queue[dict] = queue.Queue(maxsize=1)
        with self._responses_lock:
            self._responses[call_id] = waiter
        try:
            self.send({"type": "tool.request", "id": task_id, "call_id": call_id,
                       "tool": tool, "args": args})
            try:
                return waiter.get(timeout=TOOL_RESPONSE_TIMEOUT)
            except queue.Empty:
                return {"outcome": "denied", "output": "runtime did not answer the tool request"}
        finally:
            with self._responses_lock:
                self._responses.pop(call_id, None)


class TaskContext:
    def __init__(self, channel: Channel, task_id: str,
                 local_tool: Callable[[str, dict], str] | None = None):
        self.channel = channel
        self.task_id = task_id
        self._calls = 0
        self._local_tool = local_tool

    def check_cancelled(self) -> None:
        if self.task_id in self.channel.cancelled or "*" in self.channel.cancelled:
            raise Cancelled()

    def progress(self, stage: str, message: str = "") -> None:
        self.channel.send({"type": "task.progress", "id": self.task_id,
                           "stage": stage, "message": message[:500]})

    def tool(self, name: str, args: Any) -> str:
        """The only path from the agent to a tool."""
        self.check_cancelled()
        self._calls += 1
        args = args if isinstance(args, dict) else {}
        self.progress("tool", name)
        reply = self.channel.request_tool(self.task_id, f"{self.task_id}-{self._calls}",
                                          str(name), args)
        self.check_cancelled()
        outcome = reply.get("outcome")
        output = str(reply.get("output", ""))
        if outcome == "executed":
            return output
        if outcome == "delegate":
            if self._local_tool is None:
                return "error: no local implementation for delegated tool"
            return self._local_tool(name, args)
        return f"denied by jarvS policy: {output}"


# ------------------------------------------------------------------ agents

def run_selftest(ctx: TaskContext, task: str, want: str) -> tuple[str, str]:
    """Exercises the bridge without an LLM or third-party packages."""
    ctx.progress("start", "selftest")
    lines = []
    for tool, args in (
        ("get_system_state", {}),
        ("read_file", {"path": "selftest.txt"}),
        ("write_file", {"path": "selftest-out.txt", "content": task}),
        ("read_file", {"path": "../outside.txt"}),
        ("shell", {"cmd": "echo should-not-run"}),
    ):
        out = ctx.tool(tool, args)
        lines.append(f"{tool}: {out[:120]}")
    if task == "sleep":
        while True:  # waits for cancel; exercises timeout handling
            ctx.check_cancelled()
            threading.Event().wait(0.05)
    if task == "crash":
        sys.exit(3)
    return "ok", "\n".join(lines)


_APP = None


def _graph_app():
    global _APP
    if _APP is None:
        from graph import compiled
        _APP = compiled()
    return _APP


def _mediated(ctx: TaskContext):
    """Route every tools.run_tool call (graph.act resolves it at call time) through the runtime."""
    import tools

    class _Swap:
        def __enter__(self):
            self.local = tools.run_tool
            ctx._local_tool = self.local
            tools.run_tool = ctx.tool

        def __exit__(self, *exc):
            tools.run_tool = self.local

    return _Swap()


def run_queue(ctx: TaskContext, task: str, want: str) -> tuple[str, str]:
    """One step of the legacy scheduler (queued WhatsApp jobs, then missions)."""
    import worker

    with _mediated(ctx):
        return "ok", worker.run_next(_graph_app())


def run_graph(ctx: TaskContext, task: str, want: str) -> tuple[str, str]:
    """The existing LangGraph agent, with every tool call routed via the runtime."""
    import memory
    import worker

    with _mediated(ctx):
        try:
            ctx.progress("start", "graph")
            answer = worker.run_job(_graph_app(), task, "jarvs", want=want or "short")
        except Cancelled:
            memory.finish_latest_run("jarvs", "cancelled", "cancelled by runtime")
            raise
    latest = memory.last_run("adhoc")  # (started, status, summary) of the run just finished
    status = str(latest[1]) if latest else "error"
    return (status if status in ("ok", "incomplete", "error") else "error"), answer


AGENTS: dict[str, Callable[[TaskContext, str, str], tuple[str, str]]] = {
    "graph": run_graph,
    "queue": run_queue,
    "selftest": run_selftest,
}


def serve(channel: Channel) -> int:
    channel.start()
    channel.send({"type": "ready", "protocol": PROTOCOL, "worker": WORKER,
                  "agents": sorted(AGENTS)})
    while True:
        message = channel.commands.get()
        if message is None or message.get("type") == "shutdown":
            return 0
        if message.get("type") != "task.run":
            channel.send({"type": "error", "id": message.get("id"), "code": "unknown_type",
                          "message": f"unsupported message type: {message.get('type')}"})
            continue
        task_id = str(message.get("id", ""))
        agent = AGENTS.get(str(message.get("agent", "graph")))
        if agent is None:
            channel.send({"type": "task.result", "id": task_id, "status": "error",
                          "answer": f"unknown agent: {message.get('agent')}"})
            continue
        ctx = TaskContext(channel, task_id)
        try:
            status, answer = agent(ctx, str(message.get("task", "")), str(message.get("want", "")))
        except Cancelled:
            status, answer = "cancelled", "task cancelled"
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            status, answer = "error", f"{type(exc).__name__}: {exc}"
        channel.cancelled.discard(task_id)
        channel.send({"type": "task.result", "id": task_id, "status": status, "answer": answer})


def main() -> int:
    # Ctrl-C belongs to the runtime, which cancels tasks over the protocol.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    protocol_out = sys.stdout
    # Anything the agent prints must not corrupt the protocol stream.
    sys.stdout = sys.stderr
    return serve(Channel(sys.stdin, protocol_out))


if __name__ == "__main__":
    sys.exit(main())
