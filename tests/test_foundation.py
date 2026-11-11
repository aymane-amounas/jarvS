import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import runtime
import tools


class FileToolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        patcher = patch.object(tools, "HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_within_home_returns_resolved_path(self):
        self.assertEqual(tools._within_home("notes/a.txt"), self.home / "notes" / "a.txt")

    def test_write_then_read_valid_file(self):
        self.assertTrue(tools.write_file("notes/a.txt", "hello").startswith("wrote 5"))
        self.assertEqual(tools.read_file("notes/a.txt"), "hello")
        self.assertEqual(tools.read_file(str(self.home / "notes" / "a.txt")), "hello")

    def test_missing_file_is_reported(self):
        self.assertTrue(tools.read_file("missing.txt").startswith("read failed"))

    def test_path_traversal_is_denied(self):
        (self.root / "secret.txt").write_text("x")
        self.assertIn("Access Denied", tools.read_file("../secret.txt"))
        self.assertIn("Access Denied", tools.write_file("../evil.txt", "x"))
        self.assertFalse((self.root / "evil.txt").exists())

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_symlink_escape_is_denied(self):
        (self.root / "secret.txt").write_text("x")
        (self.home / "link").symlink_to(self.root / "secret.txt")
        (self.home / "dirlink").symlink_to(self.root)
        self.assertIn("Access Denied", tools.read_file("link"))
        self.assertIn("Access Denied", tools.write_file("dirlink/evil.txt", "x"))
        self.assertFalse((self.root / "evil.txt").exists())


class ShellToolTests(unittest.TestCase):
    def test_shell_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(tools.subprocess, "run") as run:
            result = tools.run_tool("shell", {"cmd": "echo hi"})
        run.assert_not_called()
        self.assertIn("disabled", result)
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn("DISABLED", tools.tool_help())

    def test_shell_opt_in_via_legacy_or_new_variable(self):
        for name in ("JARVIS_ALLOW_SHELL", "JARVS_ALLOW_SHELL"):
            with patch.dict(os.environ, {name: "1"}, clear=True):
                self.assertTrue(tools.shell_enabled())


class RuntimeTests(unittest.TestCase):
    def test_env_prefers_new_prefix_and_falls_back(self):
        with patch.dict(os.environ, {"JARVIS_X": "old"}, clear=True):
            self.assertEqual(runtime.env("X"), "old")
        with patch.dict(os.environ, {"JARVIS_X": "old", "JARVS_X": "new"}, clear=True):
            self.assertEqual(runtime.env("X"), "new")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(runtime.env("X", "d"), "d")

    def test_app_home_resolves_relative_to_project(self):
        with patch.dict(os.environ, {"JARVIS_HOME": "."}, clear=True):
            self.assertEqual(runtime.app_home(), runtime.PROJECT_ROOT)

    def test_venv_bin_layout_per_os(self):
        with patch.object(runtime, "IS_WINDOWS", True):
            self.assertEqual(runtime.venv_bin("python").parts[-2:], ("Scripts", "python.exe"))
        with patch.object(runtime, "IS_WINDOWS", False):
            self.assertEqual(runtime.venv_bin("python").parts[-2:], ("bin", "python"))

    def test_memory_detection_on_this_host(self):
        total, available = runtime.memory_bytes()
        self.assertIsNotNone(total)
        self.assertGreater(total, 0)
        if available is not None:
            self.assertLessEqual(available, total)

    def test_memory_detection_falls_back_when_reader_fails(self):
        with patch.object(runtime.platform, "system", return_value="Linux"), patch(
                "builtins.open", side_effect=OSError("no /proc")), patch.object(
                runtime.os, "sysconf", side_effect=ValueError, create=True):
            self.assertEqual(runtime.memory_bytes(), (None, None))

    def test_system_state_reports_host_without_ubuntu_assumption(self):
        state = json.loads(tools.get_system_state())
        self.assertEqual(state["os_version"], runtime.os_description())
        self.assertIn("shell_tool_enabled", state)
        self.assertIsNotNone(state["memory_total_bytes"])


class CustomToolHardeningTests(unittest.TestCase):
    def test_custom_tools_reject_unsafe_runtime_access(self):
        import tools.dynamic

        source = '''
HELP = "bad tool"
def run():
    return __import__("os").system("echo hi")
'''
        result = tools.dynamic.create_python_tool("evil_tool", source, [{"args": {}, "expected": 0}])
        self.assertIn("rejected: unsafe code rejected", result)

        source_two = '''
HELP = "more bad"
def run():
    import os
    return os.getenv("HOME")
'''
        result_two = tools.dynamic.create_python_tool("evil_tool_2", source_two, [{"args": {}, "expected": ""}])
        self.assertIn("rejected: unsafe code rejected", result_two)

    def test_os_sandbox_blocks_environment_network_and_subprocess_abuse(self):
        import shutil
        import sys

        import tools.dynamic

        supported = (
            (sys.platform == "darwin" and shutil.which("sandbox-exec"))
            or (sys.platform == "linux" and shutil.which("bwrap"))
        )
        if not supported:
            self.skipTest("no supported OS sandbox backend is installed")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workdir = root / "work"
            workdir.mkdir()
            secret = root / "host-secret"
            secret.write_text("must-not-be-readable", encoding="utf-8")
            output = root / "host-output"
            code = f'''import json, os, socket, subprocess
results = {{}}
try:
    open({str(secret)!r}, encoding="utf-8").read()
    results["host_read"] = "allowed"
except OSError:
    results["host_read"] = "blocked"
try:
    open({str(output)!r}, "w", encoding="utf-8").write("escaped")
    results["host_write"] = "allowed"
except OSError:
    results["host_write"] = "blocked"
results["secret_env"] = "present" if os.environ.get("JARVS_TEST_SECRET") else "absent"
try:
    socket.socket().bind(("127.0.0.1", 0))
    results["network"] = "allowed"
except OSError:
    results["network"] = "blocked"
try:
    subprocess.run(["/bin/echo", "escaped"], check=True, capture_output=True)
    results["process"] = "allowed"
except OSError:
    results["process"] = "blocked"
print(json.dumps(results))
'''
            command, env, profile = tools.dynamic._os_sandbox_command(
                runtime.project_python(), workdir, code, denied_paths=(root,)
            )
            self.assertIsNotNone(command)
            try:
                with patch.dict(os.environ, {"JARVS_TEST_SECRET": "host-secret"}):
                    result = tools.subprocess.run(
                        command,
                        cwd=workdir,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
            finally:
                profile.unlink(missing_ok=True)

            if "Failed RTM_NEWADDR: Operation not permitted" in result.stderr:
                self.skipTest("runner does not permit Bubblewrap network namespace setup")
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads(result.stdout)
            self.assertEqual(observed, {
                "host_read": "blocked",
                "host_write": "blocked",
                "secret_env": "absent",
                "network": "blocked",
                "process": "blocked",
            })
            self.assertFalse(output.exists())

    def test_custom_tools_fail_closed_without_os_sandbox(self):
        import tools.dynamic

        with patch.object(tools.dynamic, "_os_sandbox_command", return_value=None):
            result = tools.dynamic._run_sandboxed("pass", "run")
        self.assertFalse(result["ok"])
        self.assertIn("execution is disabled", result["error"])

    def test_windows_custom_tool_backend_is_unavailable(self):
        import tools.dynamic

        with patch.object(tools.dynamic.sys, "platform", "win32"):
            self.assertIsNone(tools.dynamic._os_sandbox_command(Path("python"), Path("work")))


class PromptTests(unittest.TestCase):
    def test_environment_rules_have_no_hardcoded_host(self):
        try:
            import graph
        except ImportError as exc:
            self.skipTest(f"graph dependencies missing: {exc}")
        rules = graph.environment_rules()
        self.assertNotIn("/home/jarvis", rules)
        self.assertNotIn(".venv/bin", rules)
        self.assertIn(str(graph.HOME), rules)


if __name__ == "__main__":
    unittest.main()
