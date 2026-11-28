"""Shared opaque-box E2E test harness helpers (Python 3.12 stdlib only)."""

from __future__ import annotations

import ctypes
import gc
import http.client
import json
import os
import queue
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

_LISTEN_RE = re.compile(r"Listening on https?://[^:\s]+:(\d+)")

_AGSYNC_ENV_KEYS = (
    "AGSYNC_SERVER",
    "AGSYNC_AGENT",
    "AGSYNC_TOKEN",
    "AGSYNC_TOKEN_FILE",
)

_V1_BASE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    machine_name TEXT NOT NULL,
    role TEXT NOT NULL,
    branch TEXT NOT NULL,
    status TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    registered_at REAL NOT NULL,
    last_heartbeat REAL NOT NULL,
    ttl_sec INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    msg_type TEXT NOT NULL,
    body TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_recipient
    ON messages(recipient_id, id);

CREATE TABLE IF NOT EXISTS agent_cursors (
    agent_id TEXT PRIMARY KEY,
    last_read_msg_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    scope_paths_json TEXT NOT NULL,
    status TEXT NOT NULL,
    owner_id TEXT,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    result_summary TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
    lease_id TEXT PRIMARY KEY,
    resource TEXT NOT NULL,
    resource_norm TEXT NOT NULL,
    scope TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leases_expires
    ON leases(expires_at);

CREATE TABLE IF NOT EXISTS context_kv (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    val INTEGER NOT NULL
);
INSERT OR IGNORE INTO counters(name, val) VALUES ('fencing_seq', 0);
INSERT OR IGNORE INTO counters(name, val) VALUES ('task_seq', 0);
"""


def _is_junction(path: Path | os.DirEntry[str]) -> bool:
    """Return True if path is a Windows Directory Junction (or reparse mount)."""
    fn = getattr(os.path, "isjunction", None)
    if fn is not None:
        try:
            if fn(path):
                return True
        except OSError:
            pass
    return False


def unlink_junctions_tree(root: Path) -> None:
    """Top-down removal of directory junctions and symlinks before rmtree.

    Critical on Windows: never recurse into a junction or call shutil.rmtree on
    a tree containing junctions, so shared junction targets are never touched.
    """
    if not root.exists():
        return
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            p = Path(entry.path)
            if _is_junction(p):
                try:
                    os.rmdir(p)
                except OSError:
                    pass
                continue
            try:
                is_link = entry.is_symlink()
            except OSError:
                is_link = False
            if is_link:
                try:
                    os.unlink(p)
                except OSError:
                    try:
                        os.rmdir(p)
                    except OSError:
                        pass
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(p)
            except OSError:
                continue


def robust_rmtree(root: Path, *, max_attempts: int = 8) -> None:
    """Windows-safe rmtree clearing read-only bits (e.g. .git/objects) with backoff."""
    if not root.exists():
        return

    def _onexc(func: Any, path: str, exc: BaseException) -> None:
        if isinstance(exc, PermissionError):
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
            func(path)
        else:
            raise exc

    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        unlink_junctions_tree(root)
        try:
            shutil.rmtree(root, onexc=_onexc)
            return
        except FileNotFoundError:
            return
        except (PermissionError, OSError) as exc:
            last_exc = exc
            gc.collect()
            time.sleep(0.05 * (attempt + 1))

    if root.exists() and last_exc is not None:
        raise last_exc


def close_process_pipes(proc: subprocess.Popen[Any]) -> None:
    """Close any open subprocess pipes cleanly."""
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def terminate_process_cleanly(proc: subprocess.Popen[Any], wait_sec: float = 5.0) -> None:
    """Terminate, wait, fallback-kill, and close pipes for a subprocess."""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=wait_sec)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=wait_sec)
                except subprocess.TimeoutExpired:
                    pass
    finally:
        close_process_pipes(proc)


def is_pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID is still running."""
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong(0)
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            if not ok:
                return False
            return exit_code.value == STILL_ACTIVE
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


def build_isolated_env(
    home_dir: Path,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return subprocess environment with isolated HOME/USERPROFILE and clean AGSYNC_*."""
    env = dict(os.environ)
    for key in _AGSYNC_ENV_KEYS:
        env.pop(key, None)
    home_str = str(home_dir)
    env["HOME"] = home_str
    env["USERPROFILE"] = home_str
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONUTF8"] = "1"
    env.setdefault("GIT_AUTHOR_NAME", "E2E Agent")
    env.setdefault("GIT_AUTHOR_EMAIL", "e2e@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "E2E Agent")
    env.setdefault("GIT_COMMITTER_EMAIL", "e2e@example.com")
    if extra:
        for k, v in extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = str(v)
    return env


class TempWorkspace:
    """Isolated temporary directory with nested ws/home/db and Windows-safe teardown."""

    def __init__(self, prefix: str = "agsync_e2e_") -> None:
        self.root = Path(tempfile.mkdtemp(prefix=prefix))
        self.home_dir = self.root / "home"
        self.ws_dir = self.root / "ws"
        self.db_dir = self.root / "db"
        self.home_dir.mkdir(parents=True, exist_ok=True)
        self.ws_dir.mkdir(parents=True, exist_ok=True)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self._hubs: list[EphemeralHub] = []
        self._procs: list[subprocess.Popen[Any]] = []
        self._cleanups: list[Any] = []

    def isolated_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return build_isolated_env(self.home_dir, extra=extra)

    def register_hub(self, hub: "EphemeralHub") -> "EphemeralHub":
        self._hubs.append(hub)
        return hub

    def register_proc(self, proc: subprocess.Popen[Any]) -> subprocess.Popen[Any]:
        self._procs.append(proc)
        return proc

    def register_cleanup(self, fn: Any) -> None:
        self._cleanups.append(fn)

    def create_git_repo(
        self,
        name: str = "repo",
        *,
        files: dict[str, str] | None = None,
    ) -> Path:
        return create_git_repo(self.ws_dir, name=name, files=files)

    def start_hub(self, **kwargs: Any) -> "EphemeralHub":
        kwargs.setdefault("db_path", self.db_dir / f"hub_{len(self._hubs) + 1}.db")
        kwargs.setdefault("home_dir", self.home_dir)
        hub = EphemeralHub(**kwargs)
        hub.start()
        self._hubs.append(hub)
        return hub

    def run_cli(
        self,
        *args: str,
        cwd: Path | None = None,
        env_overrides: dict[str, str] | None = None,
        stdin_text: str | None = None,
        timeout: float = 20.0,
    ) -> subprocess.CompletedProcess[str]:
        return run_cli(
            *args,
            cwd=cwd,
            env_overrides=env_overrides,
            home_dir=self.home_dir,
            stdin_text=stdin_text,
            timeout=timeout,
        )

    def cleanup(self) -> None:
        for fn in reversed(self._cleanups):
            try:
                fn()
            except Exception:
                pass
        for proc in reversed(self._procs):
            try:
                terminate_process_cleanly(proc)
            except Exception:
                pass
        for hub in reversed(self._hubs):
            try:
                hub.stop()
            except Exception:
                pass
        gc.collect()
        robust_rmtree(self.root)

    def __enter__(self) -> "TempWorkspace":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.cleanup()


def create_git_repo(
    ws_dir: Path,
    name: str = "repo",
    *,
    files: dict[str, str] | None = None,
) -> Path:
    """Initialize a temp Git repository under ws_dir with an initial commit."""
    ws_dir.mkdir(parents=True, exist_ok=True)
    repo = ws_dir / name
    repo.mkdir(parents=True, exist_ok=True)

    def _git(*gargs: str) -> subprocess.CompletedProcess[str]:
        res = subprocess.run(
            ["git", *gargs],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(gargs)} failed ({res.returncode}): {res.stderr or res.stdout}"
            )
        return res

    _git("init")
    _git("config", "user.name", "E2E Agent")
    _git("config", "user.email", "e2e@example.com")

    seed_files = files if files is not None else {
        "README.md": "# Sample Repo\n",
        "src/app.py": "def main():\n    return 'ok'\n",
        "src/locked.py": "VALUE = 1\n",
        "src/api.py": "API_VERSION = 1\n",
        "src/free.py": "FREE = True\n",
        "src/mine.py": "MINE = True\n",
    }
    for rel, content in seed_files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    _git("add", "-A")
    _git("commit", "-m", "chore: initial seed commit")
    return repo


def run_cli(
    *args: str,
    cwd: Path | None = None,
    env_overrides: dict[str, str] | None = None,
    home_dir: Path | None = None,
    stdin_text: str | None = None,
    timeout: float = 20.0,
) -> subprocess.CompletedProcess[str]:
    """Run `python -m agsync.cli ...` in an isolated environment."""
    temp_home: Path | None = None
    if home_dir is None:
        temp_home = Path(tempfile.mkdtemp(prefix="agsync_cli_home_"))
        eff_home = temp_home
    else:
        eff_home = home_dir
    try:
        env = build_isolated_env(eff_home, extra=env_overrides)
        return subprocess.run(
            [sys.executable, "-m", "agsync.cli", *args],
            cwd=str(cwd or REPO_ROOT),
            env=env,
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    finally:
        if temp_home is not None:
            robust_rmtree(temp_home)


def _default_direct_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_request(
    method: str,
    url: str,
    *,
    body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Perform an HTTP request using urllib without raising on 4xx/5xx responses."""
    hdrs: dict[str, str] = {}
    if headers:
        hdrs.update(headers)

    data: bytes | None = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            if not any(k.lower() == "content-type" for k in hdrs):
                hdrs["Content-Type"] = "application/json"
        elif isinstance(body, str):
            data = body.encode("utf-8")
        elif isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        else:
            raise TypeError(f"Unsupported body type: {type(body)!r}")

    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
    eff_opener = opener if opener is not None else _default_direct_opener()
    try:
        with eff_opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return int(resp.status), resp_headers, raw
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
            resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
            return int(exc.code), resp_headers, raw
        finally:
            exc.close()


def http_json(
    method: str,
    url: str,
    *,
    body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, dict[str, str], Any]:
    """Perform an HTTP request and parse JSON response body when valid."""
    status, resp_headers, raw = http_request(
        method,
        url,
        body=body,
        headers=headers,
        timeout=timeout,
        opener=opener,
    )
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return status, resp_headers, None
    try:
        return status, resp_headers, json.loads(text)
    except json.JSONDecodeError:
        return status, resp_headers, text


def auth_headers(
    token: str | None = None,
    *,
    agent_id: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build standard Authorization / X-Agent-Id headers."""
    hdrs: dict[str, str] = {}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if agent_id:
        hdrs["X-Agent-Id"] = agent_id
    if extra:
        hdrs.update(extra)
    return hdrs


def mcp_rpc(
    base_url: str,
    payload: dict[str, Any],
    *,
    token: str | None = None,
    agent_id: str | None = None,
    timeout: float = 25.0,
    query: str = "",
) -> tuple[int, dict[str, Any]]:
    """Send a JSON-RPC 2.0 request to Streamable HTTP POST /mcp."""
    url = f"{base_url.rstrip('/')}/mcp"
    if query:
        url = f"{url}?{query.lstrip('?')}"
    hdrs = auth_headers(token=token, agent_id=agent_id, extra={"Content-Type": "application/json"})
    status, _, parsed = http_json("POST", url, body=payload, headers=hdrs, timeout=timeout)
    if isinstance(parsed, dict):
        return status, parsed
    return status, {"raw": parsed}


def mcp_initialize(
    base_url: str,
    *,
    token: str | None = None,
    agent_id: str | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Perform MCP initialize + notifications/initialized handshake."""
    status, resp = mcp_rpc(
        base_url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "agsync-e2e", "version": "1.0"},
            },
        },
        token=token,
        agent_id=agent_id,
        timeout=timeout,
    )
    if status != 200 or "result" not in resp:
        raise AssertionError(f"MCP initialize failed: status={status} resp={resp}")
    http_json(
        "POST",
        f"{base_url.rstrip('/')}/mcp",
        body={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=auth_headers(token=token, agent_id=agent_id, extra={"Content-Type": "application/json"}),
        timeout=timeout,
    )
    return resp


def mcp_list_tools(
    base_url: str,
    *,
    token: str | None = None,
    agent_id: str | None = None,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """Call MCP tools/list and return list of tool descriptors."""
    status, resp = mcp_rpc(
        base_url,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        token=token,
        agent_id=agent_id,
        timeout=timeout,
    )
    if status != 200 or "result" not in resp:
        raise AssertionError(f"MCP tools/list failed: status={status} resp={resp}")
    tools = resp["result"].get("tools")
    if not isinstance(tools, list):
        raise AssertionError(f"MCP tools/list missing tools array: {resp}")
    return tools


def mcp_call(
    base_url: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    token: str | None = None,
    agent_id: str | None = None,
    timeout: float = 25.0,
    req_id: int = 3,
    query: str = "",
) -> tuple[int, dict[str, Any]]:
    """Call MCP tools/call and return (status, parsed_jsonrpc_response)."""
    return mcp_rpc(
        base_url,
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        },
        token=token,
        agent_id=agent_id,
        timeout=timeout,
        query=query,
    )


class EphemeralHub:
    """Subprocess runner for `agsync serve --port 0` with Windows-safe stderr port discovery."""

    def __init__(
        self,
        *,
        db_path: Path,
        home_dir: Path,
        host: str = "127.0.0.1",
        token: str | None = None,
        base_url: str | None = None,
        allowed_hosts: list[str] | None = None,
        extra_args: list[str] | None = None,
        env_overrides: dict[str, str] | None = None,
        startup_timeout: float = 15.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.home_dir = Path(home_dir)
        self.host = host
        self.token = token
        self.base_url_flag = base_url
        self.allowed_hosts = list(allowed_hosts or [])
        self.extra_args = list(extra_args or [])
        self.env_overrides = dict(env_overrides or {})
        self.startup_timeout = startup_timeout

        self.proc: subprocess.Popen[str] | None = None
        self.port: int | None = None
        self._stderr_lines: list[str] = []
        self._stdout_lines: list[str] = []
        self._lock = threading.Lock()
        self._port_event = threading.Event()
        self._stderr_thread: threading.Thread | None = None
        self._stdout_thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise RuntimeError("Hub has not bound a port yet")
        connect_host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{connect_host}:{self.port}"

    @property
    def stderr_text(self) -> str:
        with self._lock:
            return "".join(self._stderr_lines)

    def _build_cmd(self) -> list[str]:
        cmd = [
            sys.executable,
            "-u",
            "-m",
            "agsync.cli",
            "serve",
            "--host",
            self.host,
            "--port",
            "0",
            "--db",
            str(self.db_path),
            "--no-discover",
        ]
        if self.token is not None:
            cmd.extend(["--token", self.token])
        if self.base_url_flag is not None:
            cmd.extend(["--base-url", self.base_url_flag])
        for ah in self.allowed_hosts:
            cmd.extend(["--allowed-host", ah])
        cmd.extend(self.extra_args)
        return cmd

    def _drain_stderr(self, pipe: Any) -> None:
        try:
            for line in iter(pipe.readline, ""):
                with self._lock:
                    self._stderr_lines.append(line)
                m = _LISTEN_RE.search(line)
                if m and self.port is None:
                    self.port = int(m.group(1))
                    self._port_event.set()
        except Exception:
            pass

    def _drain_stdout(self, pipe: Any) -> None:
        try:
            for line in iter(pipe.readline, ""):
                with self._lock:
                    self._stdout_lines.append(line)
        except Exception:
            pass

    def start(self) -> "EphemeralHub":
        if self.proc is not None and self.proc.poll() is None:
            return self
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.port = None
        self._port_event.clear()
        with self._lock:
            self._stderr_lines.clear()
            self._stdout_lines.clear()

        env = build_isolated_env(self.home_dir, extra=self.env_overrides)
        self.proc = subprocess.Popen(
            self._build_cmd(),
            cwd=str(REPO_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(self.proc.stderr,), daemon=True
        )
        self._stdout_thread = threading.Thread(
            target=self._drain_stdout, args=(self.proc.stdout,), daemon=True
        )
        self._stderr_thread.start()
        self._stdout_thread.start()

        deadline = time.monotonic() + self.startup_timeout
        while not self._port_event.wait(timeout=0.05):
            if self.proc.poll() is not None:
                self._join_readers()
                raise RuntimeError(
                    f"agsync serve exited early ({self.proc.returncode}):\n{self.stderr_text}"
                )
            if time.monotonic() >= deadline:
                stderr_dump = self.stderr_text
                self.stop()
                raise RuntimeError(
                    f"Timed out waiting for Listening banner from agsync serve:\n{stderr_dump}"
                )

        health_url = f"{self.base_url}/health"
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self._join_readers()
                raise RuntimeError(
                    f"agsync serve exited before /health became ready ({self.proc.returncode}):\n{self.stderr_text}"
                )
            try:
                status, _, _ = http_json("GET", health_url, timeout=2.0)
                if status == 200:
                    return self
            except Exception:
                pass
            time.sleep(0.05)

        stderr_dump = self.stderr_text
        self.stop()
        raise RuntimeError(f"Timed out waiting for GET /health == 200:\n{stderr_dump}")

    def _join_readers(self) -> None:
        for t in (self._stderr_thread, self._stdout_thread):
            if t is not None and t.is_alive():
                t.join(timeout=1.0)

    def stop(self) -> None:
        if self.proc is not None:
            terminate_process_cleanly(self.proc, wait_sec=5.0)
            self._join_readers()
            self.proc = None

    def restart(self, *, extra_args: list[str] | None = None) -> "EphemeralHub":
        self.stop()
        if extra_args is not None:
            self.extra_args = list(extra_args)
        time.sleep(0.05)
        return self.start()

    def __enter__(self) -> "EphemeralHub":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()


def create_v1_sqlite_db(
    db_path: Path,
    *,
    seed_sample_data: bool = False,
) -> None:
    """Create a Phase 1 SQLite database (PRAGMA user_version = 1) and close it."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.executescript(_V1_BASE_SCHEMA_SQL)
        conn.execute("PRAGMA user_version = 1;")
        if seed_sample_data:
            now = time.time()
            conn.execute(
                """
                INSERT INTO agents(agent_id, machine_name, role, branch, status,
                                   capabilities_json, registered_at, last_heartbeat, ttl_sec)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("legacy-agent", "host-1", "agent", "main", "online", "[]", now, now, 120),
            )
            conn.execute(
                """
                INSERT INTO tasks(task_id, title, description, scope_paths_json, status,
                                  owner_id, created_by, created_at, updated_at, result_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("T-0001", "Legacy Task", "From v1", '["src/app.py"]', "open", None, "legacy-agent", now, now, ""),
            )
            conn.execute("UPDATE counters SET val = 1 WHERE name = 'task_seq';")
            conn.execute(
                """
                INSERT INTO leases(lease_id, resource, resource_norm, scope, owner_id,
                                   reason, fencing_token, acquired_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("L-v1", "src/app.py", "src/app.py", "file", "legacy-agent", "v1 lease", 1, now, now + 600),
            )
            conn.execute("UPDATE counters SET val = 1 WHERE name = 'fencing_seq';")
            conn.execute(
                """
                INSERT INTO messages(sender_id, recipient_id, msg_type, body, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("legacy-agent", "*", "info", "hello from v1", "{}", now),
            )
            conn.execute(
                """
                INSERT INTO context_kv(key, value_json, updated_by, version, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("v1_key", '{"migrated": true}', "legacy-agent", 1, now),
            )
    finally:
        conn.close()


def inspect_sqlite(
    db_path: Path,
    query: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    """Run a read-only SQLite query and return list of row dicts, closing connection."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def inspect_sqlite_schema(db_path: Path) -> dict[str, Any]:
    """Return user_version, table names, index names, and column metadata."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        ver_row = conn.execute("PRAGMA user_version;").fetchone()
        user_version = int(ver_row[0]) if ver_row else 0
        master_rows = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE type IN ('table', 'index');"
        ).fetchall()
        tables: dict[str, list[dict[str, Any]]] = {}
        indexes: dict[str, dict[str, Any]] = {}
        for r in master_rows:
            if r["type"] == "table" and not str(r["name"]).startswith("sqlite_"):
                tname = str(r["name"])
                cols = [dict(c) for c in conn.execute(f"PRAGMA table_info({tname});").fetchall()]
                tables[tname] = cols
            elif r["type"] == "index" and not str(r["name"]).startswith("sqlite_autoindex"):
                indexes[str(r["name"])] = {
                    "table": r["tbl_name"],
                    "sql": r["sql"] or "",
                }
        return {
            "user_version": user_version,
            "tables": tables,
            "indexes": indexes,
        }
    finally:
        conn.close()


def send_persistent_put_batch(
    host: str,
    port: int,
    count: int,
    *,
    token: str | None = None,
    agent_id: str = "burst-agent",
    key_prefix: str = "burst_",
) -> list[int]:
    """Send `count` rapid PUT /context/<key> requests over one keep-alive HTTPConnection."""
    conn = http.client.HTTPConnection(host, port, timeout=15.0)
    statuses: list[int] = []
    try:
        for idx in range(count):
            payload = json.dumps({"value": idx, "updated_by": agent_id}).encode("utf-8")
            hdrs = {
                "Host": f"{host}:{port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
                "Connection": "keep-alive",
            }
            if token:
                hdrs["Authorization"] = f"Bearer {token}"
            conn.request("PUT", f"/context/{key_prefix}{idx}", body=payload, headers=hdrs)
            resp = conn.getresponse()
            resp.read()
            statuses.append(int(resp.status))
    finally:
        conn.close()
    return statuses


def drain_http_events(
    base_url: str,
    *,
    since_id: int = 0,
    token: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Fetch all available events via GET /events pagination."""
    collected: list[dict[str, Any]] = []
    cursor = since_id
    while True:
        hdrs = auth_headers(token=token)
        status, _, body = http_json(
            "GET",
            f"{base_url.rstrip('/')}/events?since_id={cursor}&wait=0&limit={limit}",
            headers=hdrs,
            timeout=15.0,
        )
        if status != 200 or not isinstance(body, dict):
            raise AssertionError(f"GET /events failed: status={status} body={body}")
        batch = body.get("events") or []
        if not batch:
            break
        collected.extend(batch)
        next_cursor = int(batch[-1]["id"])
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < limit:
            break
    return collected


class ManagedRepoTestMixin:
    """Optional convenience base setup for unittest.TestCase subclasses."""

    ws: TempWorkspace

    def setUp(self) -> None:
        self.ws = TempWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()
