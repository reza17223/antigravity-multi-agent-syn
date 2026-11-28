"""Self-updater for agsync: pulls git updates or downloads latest GitHub release zip.

Includes automatic VPN/proxy detection (v2ray, Clash, Nekoray, WinINET)
and dual-engine network fetching (curl.exe + urllib) to prevent network timeouts.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
GITHUB_REPO = "reza17223/antigravity-multi-agent-syn"

# Common local proxy ports used by VPN / proxy tools in Iran & elsewhere
LOCAL_PROXY_PORTS = [10808, 10809, 7890, 2081, 1080, 10801, 8080, 8888]


def get_current_version() -> str:
    try:
        from agsync import __version__
        return __version__
    except Exception:
        return "0.0.0"


def detect_system_proxy() -> str | None:
    """Detect active system or local proxy (WinINET registry, ENV, or active local ports)."""
    # 1. Environment variables
    for env_var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        val = os.environ.get(env_var)
        if val:
            if not val.startswith("http://") and not val.startswith("https://") and not val.startswith("socks5://"):
                val = f"http://{val}"
            return val

    # 2. Windows Internet Settings Registry
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as key:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
                if enabled:
                    server, _ = winreg.QueryValueEx(key, "ProxyServer")
                    if server:
                        # Might be 'http=127.0.0.1:10808;https=127.0.0.1:10808' or just '127.0.0.1:10808'
                        if ";" in server:
                            for part in server.split(";"):
                                if part.startswith("https="):
                                    server = part.split("=", 1)[1]
                                    break
                                elif part.startswith("http="):
                                    server = part.split("=", 1)[1]
                        if not server.startswith("http://") and not server.startswith("https://"):
                            server = f"http://{server}"
                        return server
        except Exception:
            pass

    # 3. Probe active local proxy listeners (v2rayN, Clash, Nekoray, etc.)
    for port in LOCAL_PROXY_PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.15)
        try:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                s.close()
                return f"http://127.0.0.1:{port}"
            s.close()
        except Exception:
            pass

    return None


def _get_curl_path() -> str | None:
    """Find system curl.exe if available."""
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if curl:
        return curl
    win_curl = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
    if win_curl.exists():
        return str(win_curl)
    return None


def fetch_url_text(url: str, timeout: float = 12.0) -> str:
    """Fetch URL text using curl (with proxy fallback) or urllib."""
    proxy = detect_system_proxy()

    # Try curl first (very fast, uses OS network stack)
    curl = _get_curl_path()
    if curl:
        cmd = [curl, "-sSL", "-f", "--connect-timeout", "6", "--max-time", str(int(timeout))]
        cmd.extend(["-H", "User-Agent: agsync-updater"])
        if proxy:
            cmd.extend(["--proxy", proxy])
        cmd.append(url)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout
        except Exception:
            pass

        # Try curl without explicit proxy if proxy call failed
        if proxy:
            cmd_no_proxy = [curl, "-sSL", "-f", "--connect-timeout", "6", "--max-time", str(int(timeout))]
            cmd_no_proxy.extend(["-H", "User-Agent: agsync-updater", url])
            try:
                proc = subprocess.run(cmd_no_proxy, capture_output=True, text=True, timeout=timeout + 2)
                if proc.returncode == 0 and proc.stdout.strip():
                    return proc.stdout
            except Exception:
                pass

    # Urllib fallback with proxy handler
    handlers: list[Any] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler())
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "agsync-updater"})
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def download_file(
    url: str,
    target_path: Path,
    timeout: float = 60.0,
    log_fn: Callable[[str], None] | None = None,
) -> bool:
    """Download a remote file using curl or urllib with automatic proxy support."""
    proxy = detect_system_proxy()
    curl = _get_curl_path()

    if log_fn and proxy:
        log_fn(f"Using network proxy: {proxy}")

    # Method 1: curl.exe
    if curl:
        cmd = [curl, "-sSL", "-f", "--connect-timeout", "10", "--max-time", str(int(timeout))]
        cmd.extend(["-H", "User-Agent: agsync-updater"])
        if proxy:
            cmd.extend(["--proxy", proxy])
        cmd.extend(["-o", str(target_path), url])
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
            if proc.returncode == 0 and target_path.exists() and target_path.stat().st_size > 0:
                return True
        except Exception as exc:
            if log_fn:
                log_fn(f"curl download notice: {exc}")

    # Method 2: urllib.request
    try:
        handlers: list[Any] = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler())
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, headers={"User-Agent": "agsync-updater"})
        with opener.open(req, timeout=timeout) as resp, open(target_path, "wb") as wf:
            while chunk := resp.read(65536):
                wf.write(chunk)
        return target_path.exists() and target_path.stat().st_size > 0
    except Exception as exc:
        if log_fn:
            log_fn(f"urllib download notice: {exc}")
        return False


def _version_tuple(v: str) -> tuple[int, ...]:
    clean = v.lstrip("v").strip()
    parts = []
    for p in clean.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def check_github_update() -> dict[str, Any]:
    """Check GitHub for newer versions of agsync using git, API, or raw file."""
    cur_ver = get_current_version().lstrip("v").strip()
    proxy = detect_system_proxy()

    # 1. If inside a Git clone, check via git first
    if (ROOT_DIR / ".git").exists():
        try:
            env = os.environ.copy()
            if proxy:
                env.setdefault("HTTP_PROXY", proxy)
                env.setdefault("HTTPS_PROXY", proxy)

            head_proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(ROOT_DIR),
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            local_sha = head_proc.stdout.strip()

            remote_proc = subprocess.run(
                ["git", "ls-remote", "origin", "refs/heads/main"],
                cwd=str(ROOT_DIR),
                capture_output=True,
                text=True,
                env=env,
                timeout=8.0,
            )
            remote_sha = ""
            if remote_proc.returncode == 0 and remote_proc.stdout.strip():
                remote_sha = remote_proc.stdout.split()[0].strip()

            tags_proc = subprocess.run(
                ["git", "ls-remote", "--tags", "origin"],
                cwd=str(ROOT_DIR),
                capture_output=True,
                text=True,
                env=env,
                timeout=8.0,
            )
            tags = []
            if tags_proc.returncode == 0 and tags_proc.stdout.strip():
                for line in tags_proc.stdout.splitlines():
                    if "refs/tags/" in line and not line.endswith("^{}"):
                        tags.append(line.split("refs/tags/")[-1].strip())

            latest_tag = max(tags, key=_version_tuple) if tags else f"v{cur_ver}"
            tag_clean = latest_tag.lstrip("v").strip()

            has_tag_update = _version_tuple(tag_clean) > _version_tuple(cur_ver)
            has_commit_update = bool(local_sha and remote_sha and local_sha != remote_sha)
            has_update = has_tag_update or has_commit_update

            display_latest = tag_clean if has_tag_update else (f"{tag_clean} ({remote_sha[:7]})" if remote_sha else tag_clean)

            return {
                "has_update": has_update,
                "latest_version": display_latest,
                "current_version": cur_ver,
                "zip_url": f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{latest_tag}.zip",
                "tag_name": latest_tag,
                "via_git": True,
            }
        except Exception:
            pass

    # 2. Query GitHub Releases API with proxy & curl fallback
    release_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    try:
        raw_json = fetch_url_text(release_url, timeout=12.0)
        data = json.loads(raw_json)
        tag = str(data.get("tag_name", "")).lstrip("v").strip()
        has_update = _version_tuple(tag) > _version_tuple(cur_ver)

        zip_url = ""
        for asset in data.get("assets", []):
            if str(asset.get("name", "")).endswith(".zip"):
                zip_url = str(asset.get("browser_download_url", ""))
                break
        if not zip_url:
            tag_str = str(data.get("tag_name") or tag)
            if not tag_str.startswith("v"):
                tag_str = f"v{tag_str}"
            zip_url = f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{tag_str}.zip"

        return {
            "has_update": has_update,
            "latest_version": tag,
            "current_version": cur_ver,
            "zip_url": zip_url,
            "notes": str(data.get("body", "")),
            "tag_name": str(data.get("tag_name", "")),
        }
    except Exception as exc_api:
        # 3. Fallback: Query raw __version__ file from GitHub main branch
        raw_file_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/agsync/__init__.py"
        try:
            content = fetch_url_text(raw_file_url, timeout=10.0)
            tag = cur_ver
            for line in content.splitlines():
                if "__version__" in line and "=" in line:
                    tag = line.split("=")[-1].strip().strip("\"'")
                    break
            has_update = _version_tuple(tag) > _version_tuple(cur_ver)
            return {
                "has_update": has_update,
                "latest_version": tag,
                "current_version": cur_ver,
                "zip_url": f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip",
                "tag_name": f"v{tag}",
            }
        except Exception:
            return {
                "error": str(exc_api),
                "has_update": False,
                "latest_version": cur_ver,
                "current_version": cur_ver,
                "zip_url": f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip",
            }


def perform_update(
    root_dir: Path | str = ROOT_DIR,
    force: bool = False,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """Perform self-update via Git pull or zip patch download without losing config.json."""
    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)
        else:
            print(f"[updater] {msg}")

    target = Path(root_dir).resolve()
    proxy = detect_system_proxy()
    if proxy:
        _log(f"Active proxy detected: {proxy}")

    # Strategy 1: Git pull if it's a git clone
    if (target / ".git").exists():
        _log(f"Detected Git repository in {target}. Pulling latest changes via Git...")
        env = os.environ.copy()
        if proxy:
            env.setdefault("HTTP_PROXY", proxy)
            env.setdefault("HTTPS_PROXY", proxy)
        try:
            res = subprocess.run(
                ["git", "pull", "origin", "main"],
                cwd=str(target),
                capture_output=True,
                text=True,
                env=env,
                timeout=30.0,
            )
            if res.returncode == 0:
                out = res.stdout.strip()
                _log(f"Git update successful: {out}")
                return True, f"Git pull complete: {out}"
            else:
                _log(f"Git pull notice ({res.stderr.strip()}), falling back to release download...")
        except Exception as exc:
            _log(f"Git command timed out or failed: {exc}. Trying zip download...")

    # Strategy 2: Download release or main branch zip
    _log("Checking update source on GitHub...")
    info = check_github_update()
    zip_url = info.get("zip_url") or f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip"
    latest_ver = info.get("latest_version") or "latest"

    _log(f"Downloading update (v{latest_ver}) from {zip_url}...")

    temp_dir = Path(tempfile.mkdtemp(prefix="agsync_update_"))
    try:
        temp_zip = temp_dir / "update.zip"
        download_ok = download_file(zip_url, temp_zip, timeout=90.0, log_fn=_log)

        # Fallback to main branch archive if specific tag zip failed
        if not download_ok and "refs/tags" in zip_url:
            fallback_url = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip"
            _log(f"Tag download timed out. Retrying with main branch archive: {fallback_url}...")
            download_ok = download_file(fallback_url, temp_zip, timeout=90.0, log_fn=_log)

        if not download_ok:
            return False, "Failed to download update archive from GitHub. Please check your internet/VPN connection."

        _log(f"Downloaded update archive ({temp_zip.stat().st_size} bytes). Extracting...")
        extract_dir = temp_dir / "extracted"
        extract_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(temp_zip, "r") as zf:
            zf.extractall(extract_dir)

        # Detect extracted root folder (GitHub zips usually have repo-name-version/)
        candidates = list(extract_dir.iterdir())
        source_dir = extract_dir
        if len(candidates) == 1 and candidates[0].is_dir():
            source_dir = candidates[0]

        _log(f"Applying patch into {target} (preserving config.json and state.db)...")
        updated_count = 0
        for root, dirs, files in os.walk(source_dir):
            rel = os.path.relpath(root, source_dir)
            if rel != "." and (rel.startswith(".git") or rel.startswith(".agsync")):
                continue
            dest_folder = target / rel if rel != "." else target
            dest_folder.mkdir(parents=True, exist_ok=True)
            for f in files:
                # Strictly preserve local user configuration and database files!
                if f in ("config.json", "state.db") or f.endswith(".sqlite") or f.endswith(".db"):
                    continue
                src_file = Path(root) / f
                dst_file = dest_folder / f
                try:
                    shutil.copy2(src_file, dst_file)
                    updated_count += 1
                except OSError:
                    pass

        _log(f"Successfully applied {updated_count} updated files to agsync (v{latest_ver})!")
        return True, f"Updated to v{latest_ver} successfully ({updated_count} files patched)."
    except Exception as exc:
        _log(f"Update failed: {exc}")
        return False, str(exc)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> int:
    print(f"agsync Updater — Current Version: v{get_current_version()}")
    proxy = detect_system_proxy()
    if proxy:
        print(f"Detected Proxy: {proxy}")
    print("-" * 50)
    ok, msg = perform_update()
    print("-" * 50)
    print(f"Result: {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
