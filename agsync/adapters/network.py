"""Network, proxy-bypass, port/PID locking, and Windows reliability helpers."""

from __future__ import annotations

import ctypes
import ipaddress
import json
import os
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def is_loopback_host(host: str) -> bool:
    """Return True if host is strictly a loopback address or localhost."""
    cleaned = host.strip().strip("[]").lower()
    if cleaned in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return False


def is_ip_literal(host: str) -> bool:
    """Return True if host is a valid IPv4 or IPv6 literal."""
    cleaned = host.strip().strip("[]")
    try:
        ipaddress.ip_address(cleaned)
        return True
    except ValueError:
        return False


def is_local_or_private_host(host: str) -> bool:
    """Return True for loopback, private LAN (RFC1918/ULA), link-local, or .local hosts."""
    cleaned = host.strip().strip("[]").lower()
    if not cleaned:
        return False
    if cleaned == "localhost" or cleaned.endswith(".local"):
        return True
    try:
        addr = ipaddress.ip_address(cleaned)
        return bool(addr.is_loopback or addr.is_private or addr.is_link_local)
    except ValueError:
        return False


def is_local_or_private_url(url: str) -> bool:
    """Return True if URL targets loopback or a private LAN host."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    return is_local_or_private_host(host)


def build_opener_for_url(url: str) -> urllib.request.OpenerDirector:
    """Return a urllib opener that bypasses system/VPN proxies for local and private LAN URLs."""
    if is_local_or_private_url(url):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def tcp_check(host: str, port: int, timeout: float = 2.0) -> tuple[bool, str]:
    """Attempt a raw TCP connect to (host, port) and return (ok, message)."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, f"TCP {host}:{port} reachable"
    except OSError as exc:
        return False, str(exc)


def validate_port(raw_port: Any) -> int:
    """Validate port strictly as an integer in 1..65535."""
    if isinstance(raw_port, bool):
        raise ValueError(f"Invalid port: {raw_port!r}")
    if isinstance(raw_port, str):
        stripped = raw_port.strip()
        if not stripped or not stripped.isdigit():
            raise ValueError(f"Port must be an integer between 1 and 65535, got {raw_port!r}")
        port = int(stripped)
    elif isinstance(raw_port, int):
        port = raw_port
    else:
        raise ValueError(f"Port must be an integer between 1 and 65535, got {raw_port!r}")
    if port < 1 or port > 65535:
        raise ValueError(f"Port out of range (1..65535): {port}")
    return port


def is_pid_alive(pid: int) -> bool:
    """Best-effort cross-platform check whether a process ID is alive."""
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            err = ctypes.GetLastError()
            # ERROR_ACCESS_DENIED (5) means process exists
            return err == 5
        try:
            exit_code = ctypes.c_ulong(0)
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class PidLockFile:
    """OS-locked PID file placed next to the SQLite DB to prevent dual zombie hubs."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self.lock_path = Path(f"{self.db_path}.pid") if self.db_path != ":memory:" else None
        self._fh: Any = None

    def acquire(self, *, host: str = "", port: int = 0) -> None:
        if self.lock_path is None:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            existing = ""
            try:
                fh.seek(0)
                existing = fh.read().strip()
            except OSError:
                pass
            fh.close()
            raise RuntimeError(
                f"Another agsync server already holds lockfile '{self.lock_path}' ({existing or exc})"
            ) from exc

        fh.seek(0)
        fh.truncate(0)
        payload = {
            "pid": os.getpid(),
            "host": host,
            "port": port,
            "started_at": time.time(),
        }
        fh.write(json.dumps(payload))
        fh.flush()
        self._fh = fh

    def update_port(self, *, host: str, port: int) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            self._fh.truncate(0)
            payload = {
                "pid": os.getpid(),
                "host": host,
                "port": port,
                "started_at": time.time(),
            }
            self._fh.write(json.dumps(payload))
            self._fh.flush()
        except OSError:
            pass

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is not None:
            try:
                if os.name == "nt":
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                fh.close()
            except OSError:
                pass
        if self.lock_path is not None:
            try:
                self.lock_path.unlink(missing_ok=True)
            except OSError:
                pass


def inspect_pid_lockfile(db_path: str | Path) -> dict[str, Any]:
    """Read a DB PID lockfile and determine if its owner process is alive or stale."""
    p = Path(f"{db_path}.pid")
    if not p.is_file():
        return {"exists": False, "path": str(p), "alive": False, "data": None}
    try:
        raw = p.read_text(encoding="utf-8-sig").strip()
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}
    pid = data.get("pid") if isinstance(data, dict) else None
    alive = isinstance(pid, int) and is_pid_alive(pid)
    return {"exists": True, "path": str(p), "alive": alive, "data": data}


def inspect_system_proxy() -> dict[str, Any]:
    """Inspect environment and Windows Internet Settings proxy configuration."""
    env_proxies = {
        k: v
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy")
        if (v := os.environ.get(k))
    }
    win_proxy: dict[str, Any] = {}
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as key:
                try:
                    win_proxy["ProxyEnable"] = bool(winreg.QueryValueEx(key, "ProxyEnable")[0])
                except OSError:
                    win_proxy["ProxyEnable"] = False
                try:
                    win_proxy["ProxyServer"] = str(winreg.QueryValueEx(key, "ProxyServer")[0])
                except OSError:
                    win_proxy["ProxyServer"] = ""
                try:
                    win_proxy["ProxyOverride"] = str(winreg.QueryValueEx(key, "ProxyOverride")[0])
                except OSError:
                    win_proxy["ProxyOverride"] = ""
        except Exception:
            pass
    return {"env": env_proxies, "windows": win_proxy}


def inspect_windows_network_profiles() -> list[dict[str, Any]]:
    """Return active Windows network connection profiles (Private/Public/Domain)."""
    if os.name != "nt":
        return []
    ps_cmd = (
        "Get-NetConnectionProfile | "
        "Select-Object Name,InterfaceAlias,NetworkCategory | ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        raw = proc.stdout.strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    except Exception:
        pass
    return []


class WindowsJobObject:
    """Windows Job Object configured with KILL_ON_JOB_CLOSE to prevent orphan child processes."""

    def __init__(self) -> None:
        self._handle: int | None = None
        if os.name != "nt":
            return
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return

            class _IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64),
                ]

            class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32),
                ]

            class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
            JobObjectExtendedLimitInformation = 9
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if ok:
                self._handle = handle
            else:
                kernel32.CloseHandle(handle)
        except Exception:
            self._handle = None

    def assign_process(self, proc: subprocess.Popen[Any]) -> bool:
        if os.name != "nt" or not self._handle:
            return False
        try:
            proc_handle = getattr(proc, "_handle", None)
            if not proc_handle:
                return False
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            return bool(kernel32.AssignProcessToJobObject(self._handle, int(proc_handle)))
        except Exception:
            return False

    def close(self) -> None:
        if os.name == "nt" and self._handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)  # type: ignore[attr-defined]
            except Exception:
                pass
            self._handle = None
