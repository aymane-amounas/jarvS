"""Cross-platform paths and host facts shared by every jarvS module."""
from __future__ import annotations

import ctypes
import os
import pathlib
import platform
import shutil
import subprocess
import sys

APP_NAME = "jarvS"
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
IS_WINDOWS = os.name == "nt"


def env(name: str, default: str = "") -> str:
    """Read JARVS_<name>, falling back to the legacy JARVIS_<name>."""
    for prefix in ("JARVS_", "JARVIS_"):
        value = os.environ.get(prefix + name)
        if value is not None:
            return value
    return default


def app_home() -> pathlib.Path:
    setting = pathlib.Path(env("HOME", str(PROJECT_ROOT))).expanduser()
    return (setting if setting.is_absolute() else PROJECT_ROOT / setting).resolve()


def venv_bin(name: str) -> pathlib.Path:
    """Path of an executable inside the project virtualenv on this OS."""
    if IS_WINDOWS:
        return PROJECT_ROOT / ".venv" / "Scripts" / f"{name}.exe"
    return PROJECT_ROOT / ".venv" / "bin" / name


def project_python() -> pathlib.Path:
    """The venv interpreter if present, else the one running jarvS."""
    candidate = venv_bin("python")
    return candidate if candidate.is_file() else pathlib.Path(sys.executable)


def os_description() -> str:
    system = platform.system()
    if system == "Linux":
        try:
            return platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
        except (AttributeError, OSError):
            return "Linux"
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0]}".strip()
    if system == "Windows":
        return f"Windows {platform.release()}"
    return platform.platform()


def shell_name() -> str:
    if IS_WINDOWS:
        return "PowerShell" if shutil.which("powershell") else "cmd.exe"
    return os.path.basename(os.environ.get("SHELL", "/bin/sh"))


def _windows_memory() -> tuple[int | None, int | None]:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        return None, None
    return status.ullTotalPhys, status.ullAvailPhys


def _linux_memory() -> tuple[int | None, int | None]:
    fields = {}
    with open("/proc/meminfo", encoding="ascii") as meminfo:
        for line in meminfo:
            key, _, value = line.partition(":")
            fields[key] = int(value.split()[0]) * 1024
    return fields.get("MemTotal"), fields.get("MemAvailable", fields.get("MemFree"))


def _macos_memory() -> tuple[int | None, int | None]:
    total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                               text=True, timeout=5, check=True).stdout.strip())
    stats = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5,
                           check=True).stdout
    page_size = int(stats.split("page size of ")[1].split()[0])
    pages = {}
    for line in stats.splitlines()[1:]:
        key, _, value = line.partition(":")
        if value.strip().rstrip(".").isdigit():
            pages[key.strip()] = int(value.strip().rstrip("."))
    free = sum(pages.get(k, 0) for k in ("Pages free", "Pages inactive", "Pages speculative"))
    return total, free * page_size


def memory_bytes() -> tuple[int | None, int | None]:
    """(total, available) physical memory in bytes; None where undetectable."""
    readers = {"Windows": _windows_memory, "Linux": _linux_memory, "Darwin": _macos_memory}
    reader = readers.get(platform.system())
    if reader is not None:
        try:
            return reader()
        except (OSError, ValueError, IndexError, AttributeError, subprocess.SubprocessError):
            pass
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return None, None
    return total, None
