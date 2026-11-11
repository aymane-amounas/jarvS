"""WhatsApp channel: one Baileys process owns the session and outbox sender."""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import pathlib
import queue
import subprocess
import threading
import time
import uuid
from typing import Callable

from dotenv import load_dotenv
from dotenv import load_dotenv
import sys # Add this import

ROOT = pathlib.Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

sys.path.append(str(ROOT)) # Add this line to expose the parent directory
import memory  # noqa: E402

log = logging.getLogger("jarvs.whatsapp")
BRIDGE_DIR = pathlib.Path(__file__).with_name("bridge")
MAX_AUDIO_BYTES = 16 * 1024 * 1024
SEND_TIMEOUT = 30


def parse_jids(value: str | None) -> frozenset[str]:
    """Parse the explicit allowlist; unset/empty means nobody is authorized."""
    return frozenset(item.strip() for item in (value or "").split(",") if item.strip())


def is_direct_jid(jid: str) -> bool:
    return jid.endswith("@s.whatsapp.net") or jid.endswith("@lid")


def authorized_jid(jid: str, allowed: frozenset[str], owner: str = "") -> bool:
    return is_direct_jid(jid) and (jid in allowed or (owner and jid == owner))


def _transcribe(audio_b64: str, mimetype: str) -> str:
    try:
        audio = base64.b64decode(audio_b64, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("invalid audio payload") from None
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise ValueError("audio is empty or exceeds the 16 MB limit")

    import tempfile
    from faster_whisper import WhisperModel

    global _WHISPER
    try:
        model = _WHISPER
    except NameError:
        model_name = os.environ.get("WHISPER_MODEL", "base")
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        _WHISPER = model

    suffix = ".ogg" if "ogg" in mimetype else ".webm"
    audio_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as audio_file:
            audio_file.write(audio)
            audio_path = audio_file.name
        segments, _ = model.transcribe(audio_path, vad_filter=True)
        return " ".join(segment.text.strip() for segment in segments).strip()
    finally:
        if audio_path:
            pathlib.Path(audio_path).unlink(missing_ok=True)


def handle_message(
    event: dict,
    send_text: Callable[[str, str], bool],
    allowed: frozenset[str],
    owner: str = "",
) -> str:
    """Authorize and route one bridge event; returns a non-sensitive status."""
    if event.get("from_me"):
        return "ignored self message"
    jid = str(event.get("jid", ""))
    if not authorized_jid(jid, allowed, owner):
        if os.environ.get("JARVIS_LOG_UNKNOWN_JIDS", "0") == "1":
            log.warning("ignored unauthorized sender jid=%s", jid)
        else:
            log.warning("ignored message from unauthorized sender")
        return "ignored unauthorized sender"

    text = str(event.get("text", "")).strip()
    if event.get("audio_b64"):
        try:
            text = _transcribe(event["audio_b64"], str(event.get("mimetype", "audio/ogg")))
        except Exception as exc:  # noqa: BLE001
            log.warning("voice transcription failed: %s", exc)
            send_text(jid, "I couldn't transcribe that voice note. Please try again or send text.")
            return "voice transcription failed"
        if not text:
            send_text(jid, "I couldn't detect speech in that voice note.")
            return "empty transcription"

    if not text:
        if event.get("unsupported_media"):
            send_text(jid, "I can process text and voice notes, but image/video recognition is not configured.")
            return "unsupported media"
        return "empty message"

    command = text.casefold().strip()
    if command == "ping":
        send_text(jid, "pong - jarvS channel is active")
        return "ping replied"

    if owner and jid == owner and (
        command == "pending" or command.startswith(("approve ", "reject "))
    ):
        import tools

        if command == "pending":
            names = tools.list_pending()
            reply = "Pending tools: " + (", ".join(names) if names else "none")
        elif command.startswith("approve "):
            reply = tools.approve_tool(command.partition(" ")[2].strip())
        else:
            reply = tools.reject_tool(command.partition(" ")[2].strip())
        send_text(jid, reply[:4000])
        return "owner command handled"

    memory.enqueue(text, source="whatsapp", reply_to=jid)
    return "queued"


class BridgeClient:
    """JSON-lines bridge client with a reader thread for messages and receipts."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None
        self.events: queue.Queue[dict] = queue.Queue()
        self.pending: dict[str, queue.Queue[dict]] = {}
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        node = os.environ.get("NODE_BINARY", "node")
        entry = BRIDGE_DIR / "src" / "bridge.mjs"
        if not entry.is_file():
            raise FileNotFoundError(f"WhatsApp bridge not found: {entry}")
        self.process = subprocess.Popen(
            [node, str(entry)], cwd=BRIDGE_DIR, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_events, daemon=True)
        self._reader.start()

    def _read_events(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log.warning("ignored malformed bridge event")
                continue
            if event.get("type") == "send_result":
                with self._pending_lock:
                    waiter = self.pending.get(str(event.get("request_id", "")))
                if waiter:
                    waiter.put(event)
            else:
                self.events.put(event)
        self.events.put({"type": "bridge_exit"})

    def send_text(self, jid: str, text: str) -> bool:
        if not self.process or not self.process.stdin or self.process.poll() is not None:
            return False
        request_id = uuid.uuid4().hex
        waiter: queue.Queue[dict] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self.pending[request_id] = waiter
        payload = {"type": "send_text", "request_id": request_id, "jid": jid, "text": text[:4000]}
        try:
            with self._write_lock:
                self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
            result = waiter.get(timeout=SEND_TIMEOUT)
            return bool(result.get("ok"))
        except (OSError, queue.Empty):
            return False
        finally:
            with self._pending_lock:
                self.pending.pop(request_id, None)

    def close(self) -> None:
        if not self.process:
            return
        if self.process.poll() is None and self.process.stdin:
            try:
                self.process.stdin.write('{"type":"shutdown"}\n')
                self.process.stdin.flush()
                self.process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.process.terminate()
        if self.process.poll() is None:
            self.process.kill()
        self.process = None


def drain_outbox(client: BridgeClient, allowed: frozenset[str], owner: str) -> None:
    for item in memory.take_outbox(limit=10, lease_seconds=60):
        jid = item["reply_to"]
        if not authorized_jid(jid, allowed, owner):
            log.warning("discarded outbound message for unauthorized recipient")
            memory.acknowledge_outbox(item["id"])
            continue
        if client.send_text(jid, item["text"]):
            memory.acknowledge_outbox(item["id"])
        else:
            memory.release_outbox(item["id"])
            break


def run() -> None:
    allowed = parse_jids(os.environ.get("JARVIS_ALLOWED"))
    owner = os.environ.get("JARVIS_OWNER_JID", "").strip()
    recipients = allowed | ({owner} if owner else set())
    if not recipients:
        log.warning("JARVIS_ALLOWED and JARVIS_OWNER_JID are empty; inbound messages are blocked")

    retry_delay = 2
    while True:
        client = BridgeClient()
        try:
            client.start()
            retry_delay = 2
            log.info("WhatsApp bridge started; scan its QR if this is first pairing")
            while client.process and client.process.poll() is None:
                try:
                    event = client.events.get(timeout=1)
                except queue.Empty:
                    event = None
                if event:
                    if event.get("type") == "message":
                        status = handle_message(event, client.send_text, allowed, owner)
                        log.info("inbound message: %s", status)
                    elif event.get("type") == "connection":
                        log.info("WhatsApp connection: %s", event.get("state", "unknown"))
                    elif event.get("type") == "bridge_error":
                        log.error("WhatsApp bridge error: %s", event.get("message", "unknown"))
                    elif event.get("type") == "bridge_exit":
                        break
                drain_outbox(client, allowed, owner)
        except KeyboardInterrupt:
            log.info("WhatsApp channel stopping")
            return
        except Exception:  # noqa: BLE001
            log.exception("WhatsApp bridge stopped")
        finally:
            client.close()
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 60)


def main() -> None:
    logging.basicConfig(level=os.environ.get("JARVIS_LOG_LEVEL", "INFO"))
    run()


if __name__ == "__main__":
    main()
