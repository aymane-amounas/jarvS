"""Restricted extension tools with test-gated activation and legacy review support."""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

import runtime

from . import _TOOLS, HOME, OUTPUT_LIMIT

PENDING_DIR = HOME / "tools_pending"
CUSTOM_DIR = HOME / "tools_custom"
_CUSTOM: dict[str, tuple[str, str]] = {}
_SAFE_BUILTINS = {
    "abs": abs,
    "bool": bool,
    "float": float,
    "int": int,
    "len": len,
    "max": max,
    "min": min,
    "round": round,
    "str": str,
}
# Resource limits are best-effort; execution also requires a platform OS sandbox.
_SANDBOX_RUNNER = r'''import json, sys
try:
    import resource
except ImportError:
    resource = None
if resource is not None:
    for _limit, _value in (("RLIMIT_CPU", 2), ("RLIMIT_AS", 256 * 1024 * 1024),
                           ("RLIMIT_FSIZE", 0), ("RLIMIT_CORE", 0)):
        try:
            resource.setrlimit(getattr(resource, _limit), (_value, _value))
        except (AttributeError, ValueError, OSError):
            pass
payload = json.load(sys.stdin)
safe = {"abs": abs, "bool": bool, "float": float, "int": int, "len": len,
        "max": max, "min": min, "round": round, "str": str}
namespace = {"__builtins__": safe}
try:
    exec(compile(payload["source"], "<jarvs-tool>", "exec"), namespace)
    if payload["mode"] == "test":
        for index, case in enumerate(payload["tests"], 1):
            actual = namespace["run"](**case["args"])
            if actual != case["expected"]:
                raise ValueError("test case " + str(index) + " returned an unexpected result")
        result = {"ok": True, "message": "all test cases passed"}
    else:
        value = namespace["run"](**payload["args"])
        result = {"ok": True, "result": str(value)[:20000]}
except Exception as exc:
    result = {"ok": False, "error": str(exc)[:1000]}
print(json.dumps(result))
'''
_ALLOWED_NODES = {
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Assign, ast.Name,
    ast.Load, ast.Store, ast.Constant, ast.Return, ast.BinOp, ast.Add, ast.Sub,
    ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.UnaryOp, ast.UAdd, ast.USub,
    ast.Not, ast.If, ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt,
    ast.GtE, ast.BoolOp, ast.And, ast.Or, ast.Call, ast.keyword, ast.List,
    ast.Tuple, ast.Dict, ast.IfExp,
}
_DANGEROUS_SOURCE_PATTERNS = (
    re.compile(r"__import__\s*\("),
    re.compile(r"\b(?:eval|exec|compile|open|getattr|setattr|globals|locals|vars)\s*\("),
    re.compile(r"\b(?:os|subprocess|pathlib|socket|shutil|tempfile|resource)\s*\."),
    re.compile(
        r"\b(?:import\s+(?:os|subprocess|pathlib|socket|shutil|tempfile|resource)\b|from\s+"
        r"(?:os|subprocess|pathlib|socket|shutil|tempfile|resource)\s+import\s+)"
    ),
)


def _reject_unsafe_source(source: str) -> None:
    if not isinstance(source, str):
        raise ValueError("source must be text")
    if not source.strip():
        raise ValueError("source cannot be empty")
    for pattern in _DANGEROUS_SOURCE_PATTERNS:
        if pattern.search(source):
            raise ValueError("unsafe code rejected: imports or runtime access are forbidden")


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name):
        raise ValueError("bad tool name")
    if name in _TOOLS or name in _CUSTOM:
        raise ValueError("reserved or active tool name")


def _validate(name: str, source: str) -> tuple[ast.Module, str]:
    _validate_name(name)
    _reject_unsafe_source(source)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError(f"bad syntax: {exc.msg}") from exc
    if any(type(node) not in _ALLOWED_NODES for node in ast.walk(tree)):
        raise ValueError("unsupported syntax; imports, attributes, loops, and classes are rejected")

    help_text = ""
    run_functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == "run"]
    help_assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                        and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                        and node.targets[0].id == "HELP"]
    if len(tree.body) != 2 or len(run_functions) != 1 or len(help_assignments) != 1:
        raise ValueError("tool must define only HELP and one run() function")
    help_value = help_assignments[0].value
    if not isinstance(help_value, ast.Constant) or not isinstance(help_value.value, str):
        raise ValueError("HELP must be a string constant")
    help_text = help_value.value[:300]

    function = run_functions[0]
    if function.decorator_list or function.args.posonlyargs or function.args.kwonlyargs:
        raise ValueError("run() may not use decorators or advanced arguments")
    if function.args.vararg:
        raise ValueError("run() may not use *args")
    if any(default and not isinstance(default, ast.Constant)
           for default in function.args.defaults):
        raise ValueError("run() defaults must be constants")
    parameters = {arg.arg for arg in function.args.args}
    if function.args.kwarg:
        parameters.add(function.args.kwarg.arg)
    locals_found = {node.id for node in ast.walk(function)
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    valid_names = parameters | locals_found | set(_SAFE_BUILTINS) | {"run"}
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in valid_names:
                raise ValueError(f"unsupported name: {node.id}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _SAFE_BUILTINS:
                raise ValueError("only approved built-in functions may be called")
    compile(tree, f"<tool:{name}>", "exec")
    return tree, help_text


def create_tool(name: str, source: str) -> str:
    if not isinstance(source, str) or len(source.encode("utf-8")) > 20000:
        return "rejected: source must be text under 20000 bytes"
    try:
        _validate(name, source)
    except ValueError as exc:
        return f"rejected: {exc}"
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    (PENDING_DIR / f"{name}.py").write_text(source, encoding="utf-8")
    return f"tool {name} saved for review; NOT active"


def _python_runtime_paths(python: Path) -> tuple[Path, ...]:
    paths = [python.resolve()]
    for key in ("stdlib", "platstdlib"):
        value = sysconfig.get_paths().get(key)
        if value:
            paths.append(Path(value).resolve())
    for key in ("LIBDIR", "LIBPL", "DESTSHARED"):
        value = sysconfig.get_config_var(key)
        if value:
            paths.append(Path(value).resolve())
    return tuple(dict.fromkeys(paths))


def _os_sandbox_command(
    python: Path,
    workdir: Path,
    code: str = _SANDBOX_RUNNER,
    denied_paths: tuple[Path, ...] | None = None,
) -> tuple[list[str], dict[str, str], Path] | None:
    python = python.resolve()
    workdir = workdir.resolve()
    env = {
        "PATH": "",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "",
        "HOME": str(workdir),
        "TMPDIR": str(workdir),
        "PWD": str(workdir),
        "LANG": "C",
    }

    if sys.platform == "darwin" and (sandbox_exec := shutil.which("sandbox-exec")):
        denied = denied_paths or (
            Path.home(),
            HOME,
            runtime.PROJECT_ROOT,
            Path("/Library/Keychains"),
        )
        read_rules = [
            "(allow file-read*)",
            *(
                f'(deny file-read* (subpath "{path.resolve()}"))'
                for path in denied
            ),
        ]
        profile_text = "\n".join([
            "(version 1)",
            "(deny default)",
            *read_rules,
            f'(allow file-write* (subpath "{workdir}"))',
            "(allow process-exec)",
            "(deny network*)",
        ])
        profile = workdir / "profile.sb"
        profile.write_text(profile_text + "\n", encoding="utf-8")
        command = [
            sandbox_exec, "-f", str(profile), "--",
            str(python), "-I", "-S", "-c", code,
        ]
        return command, env, profile

    if sys.platform == "linux" and (bwrap := shutil.which("bwrap")):
        runtime_paths = _python_runtime_paths(python)
        mounts: list[tuple[Path, bool]] = [
            (path, path.is_dir()) for path in runtime_paths
        ]
        mounts.extend(
            (Path(path).resolve(), True)
            for path in ("/usr/lib", "/lib", "/lib64")
            if Path(path).exists()
        )
        if Path("/etc/ld.so.cache").exists():
            mounts.append((Path("/etc/ld.so.cache"), False))
        mounts = list(dict.fromkeys(mounts))
        arguments = [
            bwrap,
            "--unshare-all",
            "--new-session",
            "--die-with-parent",
            "--cap-drop", "ALL",
        ]
        directories = {"/tmp", "/tmp/work", "/etc"}
        for source, is_dir in mounts:
            target = source
            for parent in target.parents:
                if parent == Path("/"):
                    break
                directories.add(str(parent))
            if is_dir:
                directories.add(str(target))
        for directory in sorted(directories, key=lambda value: (value.count("/"), value)):
            arguments.extend(["--dir", directory])
        for source, is_dir in mounts:
            arguments.extend([
                "--ro-bind",
                str(source),
                str(source),
            ])
        arguments.extend([
            "--bind", str(workdir), "/tmp/work",
            "--chdir", "/tmp/work",
            str(python), "-I", "-S", "-c", code,
        ])
        env.update({"HOME": "/tmp/work", "TMPDIR": "/tmp/work", "PWD": "/tmp/work"})
        return arguments, env, workdir / ".unused-profile"

    return None


def _run_sandboxed(source: str, mode: str, args: dict | None = None,
                   tests: list[dict] | None = None) -> dict:
    python = runtime.project_python()
    payload = {"source": source, "mode": mode, "args": args or {}, "tests": tests or []}
    try:
        encoded = json.dumps(payload)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": f"sandbox input must be JSON data: {exc}"}
    if len(encoded.encode("utf-8")) > 40000:
        return {"ok": False, "error": "sandbox input exceeds 40000 bytes"}
    try:
        with tempfile.TemporaryDirectory(prefix="jarvs-tool-test-") as workdir:
            sandbox = _os_sandbox_command(python, Path(workdir))
            if sandbox is None:
                return {
                    "ok": False,
                    "error": (
                        f"OS-level custom-tool isolation is unavailable on {sys.platform}; "
                        "execution is disabled"
                    ),
                }
            command, env, profile = sandbox
            result = subprocess.run(
                command,
                input=encoded,
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            profile.unlink(missing_ok=True)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "sandbox execution exceeded 5 seconds"}
    except OSError as exc:
        return {"ok": False, "error": f"could not start sandbox: {exc}"}
    try:
        response = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
        if result.returncode and (
            "sandbox-exec" in detail or "Operation not permitted" in detail
        ):
            return {"ok": False, "error": "sandbox blocked the tool: operation not permitted"}
        return {"ok": False, "error": detail[:1000]}
    if result.returncode:
        response["ok"] = False
        response.setdefault("error", f"sandbox exited with code {result.returncode}")
    if not response.get("ok") and result.returncode and response.get("error") in (None, ""):
        response["error"] = "sandbox blocked the tool: operation not permitted"
    return response


def create_python_tool(name: str, source: str, tests: list[dict]) -> str:
    """Test a restricted tool in a resource-limited process, then activate it."""
    if not isinstance(source, str) or len(source.encode("utf-8")) > 20000:
        return "rejected: source must be text under 20000 bytes"
    if not isinstance(tests, list) or not tests or len(tests) > 10:
        return "rejected: provide 1-10 test cases with args and expected values"
    try:
        _validate(name, source)
    except ValueError as exc:
        return f"rejected: {exc}"
    destination = CUSTOM_DIR / f"{name}.py"
    if destination.is_file():
        return f"rejected: active tool already exists: {name}"
    for index, case in enumerate(tests, 1):
        if (not isinstance(case, dict) or not isinstance(case.get("args"), dict)
                or "expected" not in case):
            return f"rejected: test case {index} must contain args and expected"
    result = _run_sandboxed(source, "test", tests=tests)
    if not result.get("ok"):
        return f"rejected: sandbox tests failed: {result.get('error', 'unknown failure')}"

    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=CUSTOM_DIR,
                prefix=f".{name}.", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(source)
        os.replace(temporary, destination)
    except OSError as exc:
        if temporary and temporary.exists():
            temporary.unlink()
        return f"rejected: could not activate tested tool: {exc}"
    reload_custom_tools()
    return f"created and activated sandbox-tested tool: {name}"


def list_pending() -> list[str]:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(path.stem for path in PENDING_DIR.glob("*.py") if path.is_file())


def approve_tool(name: str) -> str:
    try:
        _validate_name(name)
    except ValueError:
        return f"no pending tool: {name}"
    pending = PENDING_DIR / f"{name}.py"
    if not pending.is_file():
        return f"no pending tool: {name}"
    try:
        _validate(name, pending.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return f"rejected: {exc}"
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    destination = CUSTOM_DIR / pending.name
    os.replace(pending, destination)
    reload_custom_tools()
    return f"approved tool: {name}"


def reject_tool(name: str) -> str:
    try:
        _validate_name(name)
    except ValueError:
        return f"no pending tool: {name}"
    pending = PENDING_DIR / f"{name}.py"
    if not pending.is_file():
        return f"no pending tool: {name}"
    pending.unlink()
    return f"tool {name} rejected and deleted"


def reload_custom_tools() -> None:
    _CUSTOM.clear()
    if not CUSTOM_DIR.is_dir():
        return
    for path in sorted(CUSTOM_DIR.glob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            _, help_text = _validate(path.stem, source)
            _CUSTOM[path.stem] = (help_text, source)
        except (OSError, UnicodeError, ValueError, SyntaxError):
            continue


def custom_help() -> str:
    reload_custom_tools()
    return "\n".join(f"- {name}: {help_text}" for name, (help_text, _) in _CUSTOM.items())


def run_custom(name: str, args: dict) -> str | None:
    reload_custom_tools()
    entry = _CUSTOM.get(name)
    if entry is None:
        return None
    result = _run_sandboxed(entry[1], "run", args=args)
    if not result.get("ok"):
        return f"custom tool failed in sandbox: {result.get('error', 'unknown failure')}"
    return str(result.get("result", ""))[:OUTPUT_LIMIT]