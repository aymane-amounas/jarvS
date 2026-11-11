import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import memory
import rss
import tools


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        memory.DB = root / "state.db"
        memory.JOURNAL = root / "journal"

    def test_insert_ids_and_bounded_journal_tail(self):
        self.assertGreater(memory.enqueue("test", source="test"), 0)
        self.assertGreater(memory.start_run("test", "test", "test"), 0)
        memory.journal_append("sample", "x" * 5000)
        self.assertEqual(len(memory.journal_tail("sample", limit=1200)), 1200)
        self.assertEqual(len(memory.recent_journal_entries("sample", limit=1)), 1)

    def test_connections_are_closed(self):
        with memory.conn() as c:
            c.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            c.execute("SELECT 1")
        # A failed transaction rolls back and still closes.
        with self.assertRaises(RuntimeError), memory.conn() as c:
            c.execute("INSERT INTO queue(task, source) VALUES ('x', 'test')")
            raise RuntimeError("boom")
        with memory.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM queue").fetchone()[0], 0)
        # Windows cannot delete a file that still has an open handle.
        memory.DB.unlink()

    def test_running_run_is_closed_without_duplicate_completion(self):
        run_id = memory.start_run("mission", "agent", "task")
        self.assertIsInstance(run_id, int)
        result = memory.finish_latest_run("agent", "cancelled", "interrupted")
        self.assertIsNotNone(result)
        with memory.conn() as c:
            row = c.execute(
                "SELECT status, summary FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
        self.assertEqual(row[0], "cancelled")
        self.assertIn("interrupted", row[1])

    def test_interrupted_queue_lease_can_resume_once_and_acknowledge(self):
        job_id = memory.enqueue("mission work", source="mission")
        first = memory.take_next(lease_seconds=60)
        self.assertIsNotNone(first)
        self.assertEqual(first["id"], job_id)

        with memory.conn() as c:
            c.execute("UPDATE queue SET lease_until=0 WHERE id=?", (job_id,))
        resumed = memory.take_next(lease_seconds=60)
        self.assertEqual(resumed, first)

        memory.acknowledge(job_id)
        self.assertIsNone(memory.take_next(lease_seconds=60))


class RssTests(unittest.TestCase):
    def test_atom_namespace_and_non_http_rejection(self):
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>News</title><summary>Summary</summary></entry></feed>'
        output = io.StringIO()
        with patch("rss.urllib.request.urlopen", return_value=io.BytesIO(atom)), \
                contextlib.redirect_stdout(output):
            rss.fetch_rss("https://example.test/feed")
        self.assertIn("News", output.getvalue())
        self.assertIn("Summary", output.getvalue())

        output = io.StringIO()
        with patch("rss.urllib.request.urlopen") as urlopen, contextlib.redirect_stdout(output):
            rss.fetch_rss("file:///etc/passwd")
        urlopen.assert_not_called()
        self.assertIn("http or https", output.getvalue())


class ProjectChecksTests(unittest.TestCase):
    def test_runs_only_selected_virtualenv_check(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            completed = Mock(returncode=0, stdout="ok", stderr="")
            with patch.object(tools, "PROJECT_ROOT", root), patch.object(
                    tools.runtime, "project_python", return_value=python), patch.object(
                    tools.subprocess, "run", return_value=completed) as run:
                result = tools.run_project_checks({"check": "syntax"})
            self.assertIn("syntax: passed", result)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][0], str(python))


if __name__ == "__main__":
    unittest.main()