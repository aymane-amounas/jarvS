"""Small local tool registry for the jarvS graph."""
from __future__ import annotations

import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace

import yaml

import runtime

PROJECT_ROOT = runtime.PROJECT_ROOT
HOME = runtime.app_home()
PENDING_DIR = HOME / "tools_pending"
CUSTOM_DIR = HOME / "tools_custom"
OUTPUT_LIMIT = 20000
FILE_LIMIT = 1_000_000
SHELL_TIMEOUT = max(1, min(int(runtime.env("SHELL_TIMEOUT", "120")), 600))


def shell_enabled() -> bool:
    """Arbitrary shell runs with the account's full rights; it is opt-in only."""
    return runtime.env("ALLOW_SHELL", "0").strip().lower() in ("1", "true", "yes")


def _audit(command: str) -> None:
    log_path = HOME / "logs" / "commands.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} shell {command}\n")


def _within_home(path: str) -> pathlib.Path:
    target = pathlib.Path(path).expanduser()
    if not target.is_absolute():
        target = HOME / target
    target = target.resolve()
    if not target.is_relative_to(HOME):
        raise ValueError("Access Denied: Path outside the jarvS home directory")
    return target


def run_project_checks(args: dict) -> str:
    """Run fixed syntax, lint, and test checks from the project virtualenv."""
    check = args.get("check", "all")
    python = runtime.project_python()
    ruff = runtime.venv_bin("ruff")
    checks = {
        "syntax": (str(python), "-m", "compileall", "-q", "graph.py", "worker.py",
                   "memory.py", "dashboard.py", "llm.py", "rss.py", "runtime.py",
                   "tools", "channels"),
        "lint": (str(ruff), "check", ".", "--output-format=concise"),
        "tests": (str(python), "-m", "pytest", "-q", "tests"),
    }
    if check not in ("all", *checks):
        return "error: check must be all, syntax, lint, or tests"

    selected = checks.items() if check == "all" else ((check, checks[check]),)
    results = []
    for name, command in selected:
        if not pathlib.Path(command[0]).is_file():
            results.append(f"{name}: unavailable; install requirements-dev.txt in .venv")
            continue
        try:
            result = subprocess.run(
                command, cwd=PROJECT_ROOT, capture_output=True, text=True,
                timeout=300, check=False,
            )
            output = (result.stdout + result.stderr).strip()[-4000:]
            status = "passed" if result.returncode == 0 else f"failed (exit {result.returncode})"
            results.append(f"{name}: {status}" + (f"\n{output}" if output else ""))
        except subprocess.TimeoutExpired:
            results.append(f"{name}: timed out after 300 seconds")
        except OSError as exc:
            results.append(f"{name}: could not start: {exc}")
    return "\n\n".join(results)[-OUTPUT_LIMIT:]


def get_system_state() -> str:
    """Return a read-only JSON snapshot of the host OS and workspace."""
    disk = shutil.disk_usage(HOME)
    memory_total, memory_available = runtime.memory_bytes()
    sensitive_markers = (".env", "credential", "token", "secret", "auth")
    contents = []
    try:
        for entry in sorted(HOME.iterdir(), key=lambda item: item.name.lower()):
            if any(marker in entry.name.lower() for marker in sensitive_markers):
                continue
            entry_type = "symlink" if entry.is_symlink() else (
                "directory" if entry.is_dir() else "file")
            contents.append({"name": entry.name, "type": entry_type})
    except OSError as exc:
        contents = [{"error": f"could not list JARVIS_HOME: {exc}"}]
    details = {
        "os": platform.platform(),
        "os_version": runtime.os_description(),
        "shell": runtime.shell_name(),
        "shell_tool_enabled": shell_enabled(),
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "current_directory": str(HOME),
        "directory_contents": contents,
        "cpu_count": os.cpu_count(),
        "load_average": os.getloadavg() if hasattr(os, "getloadavg") else None,
        "memory_total_bytes": memory_total,
        "memory_available_bytes": memory_available,
        "disk_total_bytes": disk.total,
        "disk_free_bytes": disk.free,
        "jarvs_home": str(HOME),
    }
    return json.dumps(details, indent=2)


def system_status() -> str:
    """Compatibility alias for existing local-tool callers."""
    return get_system_state()


def shell(command: str) -> str:
    if not shell_enabled():
        return ("error: the shell tool is disabled. The owner can enable it with "
                "JARVS_ALLOW_SHELL=1; it is not sandboxed and runs with this account's rights.")
    if not isinstance(command, str) or not command.strip():
        return "error: cmd must be a non-empty string"
    _audit(command)
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=HOME,
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"command timed out after {SHELL_TIMEOUT} seconds"
    except OSError as exc:
        return f"command failed: {exc}"
    output = (result.stdout + result.stderr).strip()
    output = output[-OUTPUT_LIMIT:]
    return f"exit code: {result.returncode}\n{output}" if output else f"exit code: {result.returncode}"


def fetch(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "error: URL must use http or https"
    request = urllib.request.Request(url, headers={"User-Agent": "jarvS/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(FILE_LIMIT + 1)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return f"fetch failed: {exc}"
    text = body[:FILE_LIMIT].decode("utf-8", errors="replace")
    if len(body) > FILE_LIMIT:
        text += "\n[response truncated]"
    return text[:OUTPUT_LIMIT]


def search_web(query: str) -> str:
    """Search DuckDuckGo and return concise title, URL, and snippet results."""
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        return "error: search query must be 1-500 characters"
    try:
        from ddgs import DDGS

        results = DDGS().text(query.strip(), max_results=5)
    except Exception as exc:  # noqa: BLE001
        return f"web search failed: {exc}"
    if not results:
        return "No web results found."
    formatted = []
    for result in results:
        formatted.append(
            f"{result.get('title', '(untitled)')}\n"
            f"URL: {result.get('href', '')}\n"
            f"{result.get('body', '')}"
        )
    return ("Untrusted search snippets for reference only; verify commands and URLs.\n\n"
            + "\n\n".join(formatted))[:OUTPUT_LIMIT]


def search_news(query: str, period: str = "d", max_results: int = 5) -> str:
    """Find recent news with publication dates, publishers, summaries, and URLs."""
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        return "error: news query must be 1-500 characters"
    if period not in ("d", "w", "m"):
        return "error: period must be d (day), w (week), or m (month)"
    try:
        max_results = max(1, min(int(max_results), 10))
    except (TypeError, ValueError):
        return "error: max_results must be a number"
    try:
        from ddgs import DDGS

        results = DDGS().news(query.strip(), timelimit=period, max_results=max_results)
    except Exception as exc:  # noqa: BLE001
        return f"news search failed: {exc}"
    if not results:
        return "No news results found for that period."
    items = []
    for result in results:
        items.append(
            f"{result.get('title', '(untitled)')}\n"
            f"Publisher: {result.get('source', 'unknown')} | "
            f"Published: {result.get('date', 'date unavailable')}\n"
            f"URL: {result.get('url', '')}\n"
            f"{result.get('body', '')}"
        )
    return ("Untrusted news results; verify claims against the linked sources.\n\n"
            + "\n\n".join(items))[:OUTPUT_LIMIT]


def read_file(path: str) -> str:
    try:
        target = _within_home(path)
        if target.stat().st_size > FILE_LIMIT:
            return f"error: file exceeds {FILE_LIMIT} bytes"
        return target.read_text(encoding="utf-8")[:OUTPUT_LIMIT]
    except ValueError as exc:
        return str(exc)
    except (OSError, UnicodeError) as exc:
        return f"read failed: {exc}"


def read_local_file(path: str) -> str:
    """Read a UTF-8 file from the jarvS home with an explicit sandbox error."""
    return read_file(path)


def write_file(path: str, content: str) -> str:
    try:
        target = _within_home(path)
        if not isinstance(content, str) or len(content.encode("utf-8")) > FILE_LIMIT:
            return f"error: content must be text under {FILE_LIMIT} bytes"
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary = pathlib.Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
        os.replace(temporary, target)
        return f"wrote {len(content)} characters to {target.relative_to(HOME)}"
    except ValueError as exc:
        return str(exc)
    except (OSError, UnicodeError) as exc:
        return f"write failed: {exc}"


def install_python_package(name: str) -> str:
    """Install one package into the project's virtual environment."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        return "error: package name must contain only letters, numbers, '.', '_' or '-'"
    python = runtime.venv_bin("python")
    if not python.is_file():
        return f"error: project virtual-environment Python not found: {python}"
    try:
        result = subprocess.run(
            [str(python), "-m", "pip", "install", name],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "error: package installation timed out after 600 seconds"
    except OSError as exc:
        return f"error: could not start virtual-environment pip: {exc}"
    output = (result.stdout + result.stderr).strip()
    output = output[-OUTPUT_LIMIT:]
    if result.returncode:
        return f"package installation failed (exit {result.returncode}):\n{output}"
    return f"installed {name} in the project virtual environment:\n{output}"


def add_goal(args: dict) -> str:
    """Append one validated mission without replacing existing goals."""
    mission_id = args.get("id")
    goal = args.get("goal")
    done = args.get("done", "")
    try:
        every_hours = float(args.get("every_hours", 24))
    except (TypeError, ValueError):
        return "error: every_hours must be a number"
    output = args.get("output", "short")
    deep = args.get("deep", False)
    if not isinstance(mission_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", mission_id):
        return "error: id must be a lowercase slug"
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 1000:
        return "error: goal must be 1-1000 characters"
    if not isinstance(done, str) or len(done) > 500:
        return "error: done must be at most 500 characters"
    if not 1 <= every_hours <= 8760:
        return "error: every_hours must be between 1 and 8760"
    if output not in ("short", "report") or not isinstance(deep, bool):
        return "error: output must be short/report and deep must be boolean"

    mission_path = HOME / "missions.yaml"
    try:
        existing_text = mission_path.read_text(encoding="utf-8")
        data = yaml.safe_load(existing_text) or {}
        if not isinstance(data, dict) or not isinstance(data.get("missions", []), list):
            return "error: missions.yaml must contain a missions list"
        missions = data.get("missions", [])
        if any(item.get("id") == mission_id for item in missions if isinstance(item, dict)):
            return f"error: mission already exists: {mission_id}"
        mission = {
            "id": mission_id,
            "goal": goal.strip(),
            "every_hours": every_hours,
            "done": done.strip(),
            "output": output,
            "deep": deep,
        }
        encoded = yaml.safe_dump([mission], sort_keys=False, allow_unicode=False).splitlines()
        appended = existing_text.rstrip() + "\n\n" + "\n".join(
            f"  {line}" if line else line for line in encoded
        ) + "\n"
        updated = yaml.safe_load(appended)
        if not isinstance(updated, dict) or len(updated.get("missions", [])) != len(missions) + 1:
            return "error: generated mission failed validation"
        result = write_file("missions.yaml", appended)
        return f"added goal {mission_id}: {result}" if result.startswith("wrote ") else result
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return f"could not add goal: {exc}"


def notify_owner(args: dict) -> str:
    owner = runtime.env("OWNER_JID").strip()
    text = args.get("text")
    if not owner or not (owner.endswith("@s.whatsapp.net") or owner.endswith("@lid")):
        return "error: configure a direct JARVIS_OWNER_JID (or JARVS_OWNER_JID) before sending notifications"
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        return "error: text must be 1-4000 characters"
    import memory

    message_id = memory.enqueue_outbox(owner, text.strip())
    return f"notification queued for owner (outbox id {message_id})"


def create_python_tool(args: dict) -> str:
    from .dynamic import create_python_tool as create_sandboxed_tool

    return create_sandboxed_tool(args.get("name", ""), args.get("source", ""),
                                 args.get("tests"))


_TOOLS = {
    "system_status": lambda _args: system_status(),
    "get_system_state": lambda _args: get_system_state(),
    "shell": lambda args: shell(args.get("cmd", "")),
    "fetch": lambda args: fetch(args.get("url", "")),
    "search_web": lambda args: search_web(args.get("query", "")),
    "search_news": lambda args: search_news(
        args.get("query", ""), args.get("period", "d"), args.get("max_results", 5)
    ),
    "create_python_tool": create_python_tool,
    "read_file": lambda args: read_file(args.get("path", "")),
    "read_local_file": lambda args: read_local_file(args.get("path", "")),
    "write_file": lambda args: write_file(args.get("path", ""), args.get("content", "")),
    "install_python_package": lambda args: install_python_package(args.get("name", "")),
    "add_goal": add_goal,
    "notify_owner": notify_owner,
    "run_project_checks": run_project_checks,
}


def tool_help() -> str:
    from .dynamic import custom_help

    custom = custom_help()
    shell_line = ("- shell(cmd): run a command as the current user, from the jarvS home; audited and timed, NOT sandboxed"
                  if shell_enabled() else
                  "- shell(cmd): DISABLED by the owner; do not plan shell steps")
    return f"""Available tools:
- get_system_state(): inspect OS version, Python path, available RAM and safe workspace listing as JSON
- system_status(): inspect OS, CPU count, memory and free disk space
{shell_line}
- fetch(url): fetch a bounded HTTP(S) response
- search_web(query): search DuckDuckGo for current documentation, URLs, and commands
- search_news(query, period, max_results): find recent stories with publisher, date, summary, and source URL
- create_python_tool(name, source, tests): test restricted code in a resource-limited sandbox; activates it only when every expected result matches
- read_local_file(path): read a UTF-8 file under the jarvS home; paths outside it return Access Denied
- install_python_package(name): install a package into the project's virtual environment
- write_file(path, content): atomically write a UTF-8 file under the jarvS home
- run_project_checks(check): run syntax, lint, or tests (all/syntax/lint/tests)
- add_goal(id, goal, every_hours, done, output, deep): append a validated mission
- notify_owner(text): send the configured owner a message through the WhatsApp outbox""" + (
        "\nApproved custom tools:\n" + custom if custom else ""
    )


def run_tool(name: str, args: dict) -> str:
    action = _TOOLS.get(name)
    if action is None:
        from .dynamic import run_custom

        custom_result = run_custom(name, args if isinstance(args, dict) else {})
        return custom_result if custom_result is not None else f"unknown tool: {name}"
    try:
        return action(args if isinstance(args, dict) else {})
    except Exception as exc:  # noqa: BLE001
        return f"tool {name} failed: {exc}"


def reload_custom_tools() -> None:
    from .dynamic import reload_custom_tools as reload_tools

    reload_tools()


def list_pending() -> list[str]:
    from .dynamic import list_pending as pending_tools

    return pending_tools()


def approve_tool(name: str) -> str:
    from .dynamic import approve_tool as approve

    return approve(name)


def reject_tool(name: str) -> str:
    from .dynamic import reject_tool as reject

    return reject(name)


def create_tool(name: str, source: str) -> str:
    from .dynamic import create_tool as create

    return create(name, source)


browser = SimpleNamespace(reset_session=lambda: None)