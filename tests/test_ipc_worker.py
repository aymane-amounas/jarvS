import io
import json
import queue
import random
import threading
import time
import unittest
from unittest.mock import patch

import ipc_worker


class Pipe(io.TextIOBase):
    """Line queue usable as the worker's stdin (iterable) and stdout (write)."""

    def __init__(self):
        self.lines: queue.Queue = queue.Queue()
        self._pending = ""

    def write(self, text):
        for line in text.splitlines():
            self.lines.put(line)
        return len(text)

    def flush(self):
        pass

    def __iter__(self):
        while True:
            line = self.lines.get()
            if line is None:
                return
            yield line + "\n"

    def readline(self, size=-1):
        while "\n" not in self._pending:
            line = self.lines.get()
            if line is None:
                return ""
            self._pending += line + "\n"
        line, self._pending = self._pending.split("\n", 1)
        line += "\n"
        if size >= 0 and len(line) > size:
            self._pending = line[size:] + self._pending
            return line[:size]
        return line

    def close_input(self):
        self.lines.put(None)


class Harness:
    def __init__(self, answers=None):
        self.to_worker = Pipe()
        self.from_worker = Pipe()
        self.answers = answers or {}
        self.channel = ipc_worker.Channel(self.to_worker, self.from_worker)
        self.thread = threading.Thread(target=ipc_worker.serve, args=(self.channel,), daemon=True)
        self.thread.start()

    def send(self, **message):
        self.to_worker.write(json.dumps({"v": 1, **message}) + "\n")

    def recv(self, timeout=5):
        return json.loads(self.from_worker.lines.get(timeout=timeout))

    def run_task(self, agent, task, decide):
        self.send(type="task.run", id="t1", agent=agent, task=task, want="short")
        seen = []
        while True:
            msg = self.recv()
            seen.append(msg)
            if msg["type"] == "tool.request":
                outcome, output = decide(msg["tool"], msg["args"])
                self.send(type="tool.response", call_id=msg["call_id"], outcome=outcome, output=output)
            elif msg["type"] == "task.result":
                return msg, seen


class IpcWorkerTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        ready = self.h.recv()
        self.assertEqual((ready["type"], ready["v"], ready["protocol"]), ("ready", 1, 1))
        self.assertIn("graph", ready["agents"])
        self.addCleanup(self.h.to_worker.close_input)

    def test_ping_and_bad_input(self):
        self.h.send(type="ping", nonce="abc")
        self.assertEqual(self.h.recv(), {"v": 1, "type": "pong", "nonce": "abc"})
        self.h.to_worker.write("not json\n")
        self.assertEqual(self.h.recv()["code"], "bad_json")
        self.h.to_worker.write(json.dumps({"v": 9, "type": "ping"}) + "\n")
        self.assertEqual(self.h.recv()["code"], "bad_version")

    def test_oversized_messages_are_rejected(self):
        self.h.to_worker.write(json.dumps({"v": 1, "type": "ping", "nonce": "x" * (2 * 1024 * 1024)}) + "\n")
        self.assertEqual(self.h.recv()["code"], "bad_size")
        self.h.send(type="ping", nonce="after")
        self.assertEqual(self.h.recv()["nonce"], "after")

    def test_malformed_message_fuzz_does_not_crash_worker(self):
        rng = random.Random(7)
        malformed = ["[]", "null", "true", "{}", '{"v":1}', '{"v":1,"type":[]}']
        malformed.extend(
            json.dumps({"v": 1, "type": rng.choice([None, 4, [], {}]), "extra": rng.random()})
            for _ in range(100)
        )
        for raw in malformed:
            self.h.to_worker.write(raw + "\n")
        for _ in malformed:
            self.assertEqual(self.h.recv()["type"], "error")
        self.h.send(type="ping", nonce="still-alive")
        self.assertEqual(self.h.recv()["nonce"], "still-alive")

    def test_every_tool_call_goes_through_runtime(self):
        asked = []

        def decide(tool, args):
            asked.append(tool)
            return ("denied", "no") if tool == "shell" else ("executed", f"ran {tool}")

        result, seen = self.h.run_task("selftest", "hello", decide)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(asked, ["get_system_state", "read_file", "write_file", "read_file", "shell"])
        self.assertIn("shell: denied by jarvS policy: no", result["answer"])
        self.assertTrue(any(m["type"] == "task.progress" for m in seen))

    def test_delegate_uses_local_implementation_only_when_told(self):
        calls = []
        ctx = ipc_worker.TaskContext(self.h.channel, "t9", local_tool=lambda n, a: calls.append(n) or "local")

        def respond(outcome):
            while (msg := self.h.recv())["type"] != "tool.request":
                pass
            self.h.send(type="tool.response", call_id=msg["call_id"], outcome=outcome, output="why")

        for outcome, expected in (("delegate", "local"), ("denied", "denied by jarvS policy: why")):
            responder = threading.Thread(target=respond, args=(outcome,))
            responder.start()
            self.assertEqual(ctx.tool("search_web", {"query": "x"}), expected)
            responder.join()
        self.assertEqual(calls, ["search_web"])

    def test_cancel_stops_task(self):
        def decide(tool, args):
            return "executed", ""

        self.h.send(type="task.run", id="t1", agent="selftest", task="sleep", want="")
        while True:
            msg = self.h.recv()
            if msg["type"] == "tool.request":
                self.h.send(type="tool.response", call_id=msg["call_id"], outcome="executed", output="")
                if msg["tool"] == "shell":
                    break
        time.sleep(0.1)
        self.h.send(type="task.cancel", id="t1")
        while (msg := self.h.recv())["type"] != "task.result":
            pass
        self.assertEqual(msg["status"], "cancelled")
        self.h.send(type="ping", nonce="after")
        self.assertEqual(self.h.recv()["nonce"], "after")

    def test_unknown_agent_and_agent_exception_are_reported(self):
        result, _ = self.h.run_task("nope", "x", lambda t, a: ("denied", ""))
        self.assertEqual(result["status"], "error")
        with patch.dict(ipc_worker.AGENTS, {"boom": lambda ctx, t, w: 1 / 0}):
            result, _ = self.h.run_task("boom", "x", lambda t, a: ("denied", ""))
        self.assertEqual(result["status"], "error")
        self.assertIn("ZeroDivisionError", result["answer"])

    def test_shutdown_ends_serve(self):
        self.h.send(type="shutdown")
        self.h.thread.join(timeout=5)
        self.assertFalse(self.h.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
