"""Persistent state: what ran, when, and what it learned."""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import pathlib
import re
import sqlite3
import time
from collections.abc import Iterator

import runtime

HOME = runtime.app_home()
DB = HOME / "state.db"
JOURNAL = HOME / "journal"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mission TEXT, source TEXT, task TEXT,
  started TEXT, finished TEXT, status TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task TEXT, source TEXT, reply_to TEXT,
    created TEXT, taken INTEGER DEFAULT 0, lease_until REAL
);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reply_to TEXT, text TEXT,
    created TEXT, sent INTEGER DEFAULT 0, lease_until REAL
);
"""


@contextlib.contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    """One transaction on a fresh connection, always closed afterwards.

    sqlite3's own context manager commits but never closes, which leaks a file
    handle per call (Windows then cannot delete or replace the database).
    """
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    try:
        with c:
            _migrate(c)
            yield c
    finally:
        c.close()


def _migrate(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)
    columns = {row[1] for row in c.execute("PRAGMA table_info(queue)")}
    if "lease_until" not in columns:
        c.execute("ALTER TABLE queue ADD COLUMN lease_until REAL")
        c.execute("UPDATE queue SET taken=2 WHERE taken=1")
    outbox_columns = {row[1] for row in c.execute("PRAGMA table_info(outbox)")}
    if "lease_until" not in outbox_columns:
        c.execute("ALTER TABLE outbox ADD COLUMN lease_until REAL")


def _insert_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not return an inserted row id")
    return cursor.lastrowid


# ---------- queue (WhatsApp messages jump ahead of missions) ----------

def enqueue(task: str, source: str = "whatsapp", reply_to: str = "") -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO queue(task,source,reply_to,created) VALUES(?,?,?,?)",
            (task, source, reply_to, dt.datetime.now().isoformat(timespec="seconds")),
        )
        return _insert_id(cur)


def take_next(lease_seconds: int = 3600):
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        now = time.time()
        row = c.execute(
            "SELECT id,task,source,reply_to FROM queue "
            "WHERE taken=0 OR (taken=1 AND lease_until<=?) ORDER BY id LIMIT 1",
            (now,),
        ).fetchone()
        if not row:
            return None
        c.execute("UPDATE queue SET taken=1,lease_until=? WHERE id=?",
                  (now + max(1, lease_seconds), row[0]))
        return {"id": row[0], "task": row[1], "source": row[2], "reply_to": row[3]}


def acknowledge(job_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE queue SET taken=2,lease_until=NULL WHERE id=?", (job_id,))


def release(job_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE queue SET taken=0,lease_until=NULL WHERE id=? AND taken=1",
                  (job_id,))


# ---------- outbox: worker writes replies, wa.py is the ONLY sender ----------
# Two processes must never both own the WhatsApp bridge -- only one login
# session exists. worker.py drops replies here; wa.py polls and sends them.

def enqueue_outbox(reply_to: str, text: str) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO outbox(reply_to,text,created) VALUES(?,?,?)",
            (reply_to, text, dt.datetime.now().isoformat(timespec="seconds")),
        )
        return _insert_id(cur)


def take_outbox(limit: int = 20, lease_seconds: int = 60):
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        now = time.time()
        rows = c.execute(
            "SELECT id,reply_to,text FROM outbox "
            "WHERE sent=0 OR (sent=2 AND lease_until<=?) ORDER BY id LIMIT ?",
            (now, limit),
        ).fetchall()
        if rows:
            c.executemany(
                "UPDATE outbox SET sent=2,lease_until=? WHERE id=?",
                [(now + max(1, lease_seconds), r[0]) for r in rows],
            )
        return [{"id": r[0], "reply_to": r[1], "text": r[2]} for r in rows]


def acknowledge_outbox(message_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE outbox SET sent=1,lease_until=NULL WHERE id=?", (message_id,))


def release_outbox(message_id: int) -> None:
    with conn() as c:
        c.execute("UPDATE outbox SET sent=0,lease_until=NULL WHERE id=? AND sent=2",
                  (message_id,))


# ---------- run history ----------

def start_run(mission: str, source: str, task: str) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO runs(mission,source,task,started,status) VALUES(?,?,?,?,'running')",
            (mission, source, task, dt.datetime.now().isoformat(timespec="seconds")),
        )
        return _insert_id(cur)


def finish_run(run_id: int, status: str, summary: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE runs SET finished=?,status=?,summary=? WHERE id=?",
            (dt.datetime.now().isoformat(timespec="seconds"), status, summary[:4000], run_id),
        )


def finish_latest_run(source: str, status: str, summary: str):
    """Close the newest interrupted run for a worker source, if one exists."""
    with conn() as c:
        row = c.execute(
            "SELECT id,mission,task FROM runs WHERE source=? AND status='running' "
            "ORDER BY id DESC LIMIT 1", (source,),
        ).fetchone()
        if not row:
            return None
        c.execute(
            "UPDATE runs SET finished=?,status=?,summary=? WHERE id=?",
            (dt.datetime.now().isoformat(timespec="seconds"), status, summary[:4000], row[0]),
        )
        return {"id": row[0], "mission": row[1], "task": row[2]}


def last_run(mission: str):
    with conn() as c:
        return c.execute(
            "SELECT started,status,summary FROM runs WHERE mission=? AND status!='running'"
            " ORDER BY id DESC LIMIT 1", (mission,)
        ).fetchone()


def hours_since(mission: str) -> float:
    row = last_run(mission)
    if not row or not row[0]:
        return 9e9
    started = dt.datetime.fromisoformat(row[0])
    return (dt.datetime.now() - started).total_seconds() / 3600


def recent(limit: int = 8):
    with conn() as c:
        return c.execute(
            "SELECT mission,started,status,substr(summary,1,160) FROM runs"
            " WHERE status!='running' ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


# ---------- journal: how tomorrow's run knows what today found ----------

def journal_append(mission: str, text: str) -> None:
    JOURNAL.mkdir(parents=True, exist_ok=True)
    f = JOURNAL / f"{mission}.md"
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    with f.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## {stamp}\n{text.strip()}\n")


def _read_tail(path: pathlib.Path, max_bytes: int) -> str:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - max_bytes))
        text = stream.read().decode("utf-8", errors="replace")
        if size > max_bytes and "\n" in text:
            remainder = text.split("\n", 1)[1]
            if remainder:
                text = remainder
        return text


def journal_tail(mission: str, limit: int = 1200) -> str:
    if limit <= 0:
        return ""
    f = JOURNAL / f"{mission}.md"
    if not f.exists():
        return "(no previous notes)"
    return _read_tail(f, max(4096, limit * 4))[-limit:]


def recent_journal_entries(query: str = "", mission: str = "", limit: int = 5) -> list[str]:
    """Return relevant recent Markdown entries, preferring the current mission."""
    if limit <= 0 or not JOURNAL.is_dir():
        return []
    terms = set(re.findall(r"[a-z0-9][a-z0-9._+-]{2,}", query.lower()))
    ranked: list[tuple[int, dt.datetime, int, str]] = []
    sequence = 0
    for path in sorted(JOURNAL.glob("*.md")):
        try:
            text = _read_tail(path, 128 * 1024)
            modified = dt.datetime.fromtimestamp(path.stat().st_mtime)
        except (OSError, UnicodeError):
            continue
        headings = list(re.finditer(r"(?m)^##\s+(.+)$", text))
        if not headings and text.strip():
            sections = [("older notes", text.strip(), modified)]
        else:
            sections = []
            for index, heading in enumerate(headings):
                end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
                content = text[heading.end():end].strip()
                if not content:
                    continue
                try:
                    timestamp = dt.datetime.fromisoformat(heading.group(1).strip())
                except ValueError:
                    timestamp = modified
                sections.append((heading.group(1).strip(), content, timestamp))
        for heading, content, timestamp in sections:
            sequence += 1
            overlap = len(terms.intersection(set(re.findall(
                r"[a-z0-9][a-z0-9._+-]{2,}", content.lower())
            )))
            same_mission = bool(mission and path.stem == mission)
            relevance = overlap + (len(terms) + 1 if same_mission else 0)
            entry = f"[{path.stem} | {heading}]\n{content[:1600]}"
            ranked.append((relevance, timestamp, sequence, entry))

    relevant = [entry for entry in ranked if entry[0] > 0]
    selected = sorted(relevant, key=lambda item: (item[0], item[1], item[2]), reverse=True)[:limit]
    if len(selected) < limit:
        selected_ids = {item[2] for item in selected}
        recent = sorted(
            (item for item in ranked if item[2] not in selected_ids),
            key=lambda item: (item[1], item[2]), reverse=True,
        )
        selected.extend(recent[:limit - len(selected)])
    return [entry for _, _, _, entry in selected]
