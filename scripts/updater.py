"""Self-updater for agsync: pulls git updates or downloads latest GitHub release zip."""

from __future__ import annotations

import json
import os
import shutil
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


def get_current_version() -> str:
    try:
        from agsync import __version__
        return __version__
    except Exception:
        return "0.0.0"


def check_github_update() -> dict[str, Any]:
    # 1. If inside a Git clone, check via git first (uses git's own proxy/network config)
    if (ROOT_DIR / ".git").exists():
        try:
            res = subprocess.run(
                ["git", "ls-remote", "--tags", "origin"],
                cwd=str(ROOT_DIR),
                capture_output=True,
                text=True,
                timeout=15.0,
            )
            if res.returncode == 0 and res.stdout.strip():
                tags = []
                for line in res.stdout.splitlines():
                    if "refs/tags/" in line and not line.endswith("^{}"):
                        tag_name = line.split("refs/tags/")[-1].strip()
                        tags.append(tag_name)
                if tags:
                    def _tup(t: str) -> tuple[int, ...]:
                        p = t.lstrip("v").split(".")
                        res_p = []
                        for x in p:
                            try:
                                res_p.append(int(x))
                            except ValueError:
                                res_p.append(0)
                        return tuple(res_p)

                    latest_tag = max(tags, key=_tup)
                    tag_clean = latest_tag.lstrip("v").strip()
                    cur_clean = get_current_version().lstrip("v").strip()
                    has_update = _tup(tag_clean) > _tup(cur_clean)
                    return {
                        "has_update": has_update,
                        "latest_version": tag_clean,
                        "current_version": cur_clean,
                        "zip_url": f"https://github.com/{GITHUB_REPO}/releases/download/{latest_tag}/antigravity-multi-agent-sync-{latest_tag}.zip",
                        "tag_name": latest_tag,
                        "via_git": True,
                    }
        except Exception:
            pass

    # 2. HTTP check with system proxy support & higher timeout
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": "agsync-updater"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler())
    try:
        with opener.open(req, timeout=25.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"error": str(exc), "has_update": False}

    tag = str(data.get("tag_name", "")).lstrip("v").strip()
    cur = get_current_version().lstrip("v").strip()

    # Compare version tuples
    def _parse(v: str) -> tuple[int, ...]:
        parts = []
        for p in v.split("."):
            try:
                parts.append(int(p))
            except ValueError:
                parts.append(0)
        return tuple(parts)

    has_update = _parse(tag) > _parse(cur)

    zip_url = ""
    for asset in data.get("assets", []):
        if str(asset.get("name", "")).endswith(".zip"):
            zip_url = str(asset.get("browser_download_url", ""))
            break
    if not zip_url:
        zip_url = str(data.get("zipball_url", ""))

    return {
        "has_update": has_update,
        "latest_version": tag,
        "current_version": cur,
        "zip_url": zip_url,
        "notes": str(data.get("body", "")),
        "tag_name": str(data.get("tag_name", "")),
    }


def perform_update(
    root_dir: Path | str = ROOT_DIR,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)
        else:
            print(f"[updater] {msg}")

    target = Path(root_dir).resolve()

    # Strategy 1: Git pull if it's a git clone
    if (target / ".git").exists():
        _log(f"Detected Git repository in {target}. Pulling latest changes via Git...")
        res = subprocess.run(["git", "pull", "origin", "main"], cwd=str(target), capture_output=True, text=True)
        if res.returncode == 0:
            out = res.stdout.strip()
            _log(f"Git update successful: {out}")
            return True, f"Git pull complete: {out}"
        else:
            _log(f"Git pull failed ({res.stderr.strip()}), falling back to release download...")

    # Strategy 2: Download release zip
    _log("Checking latest release on GitHub...")
    info = check_github_update()
    zip_url = info.get("zip_url")
    latest_ver = info.get("latest_version")
    if not zip_url:
        _log("API rate-limited or slow connection. Trying direct GitHub archive...")
        zip_url = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip"
        latest_ver = "latest"
    _log(f"Downloading v{latest_ver} from {zip_url}...")

    temp_dir = Path(tempfile.mkdtemp(prefix="agsync_update_"))
    try:
        temp_zip = temp_dir / "update.zip"
        req = urllib.request.Request(zip_url, headers={"User-Agent": "agsync-updater"})
        with urllib.request.urlopen(req, timeout=30.0) as resp, open(temp_zip, "wb") as wf:
            while chunk := resp.read(65536):
                wf.write(chunk)

        _log(f"Downloaded update ({temp_zip.stat().st_size} bytes). Extracting...")
        extract_dir = temp_dir / "extracted"
        extract_dir.mkdir()
        with zipfile.ZipFile(temp_zip, "r") as zf:
            zf.extractall(extract_dir)

        # Handle zipball or prefix folder
        candidates = list(extract_dir.iterdir())
        source_dir = extract_dir
        if len(candidates) == 1 and candidates[0].is_dir():
            source_dir = candidates[0]

        _log(f"Applying updated files into {target}...")
        for root, dirs, files in os.walk(source_dir):
            rel = os.path.relpath(root, source_dir)
            if rel != "." and (rel.startswith(".git") or rel.startswith(".agsync")):
                continue
            dest_folder = target / rel if rel != "." else target
            dest_folder.mkdir(parents=True, exist_ok=True)
            for f in files:
                # Do not overwrite local user configs
                if f in ("config.json", "state.db"):
                    continue
                src_file = Path(root) / f
                dst_file = dest_folder / f
                try:
                    shutil.copy2(src_file, dst_file)
                except OSError:
                    pass

        _log(f"Successfully updated agsync to v{latest_ver}!")
        return True, f"Updated to v{latest_ver} successfully."
    except Exception as exc:
        _log(f"Update failed: {exc}")
        return False, str(exc)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> int:
    print(f"agsync Updater — Current Version: v{get_current_version()}")
    print("-" * 50)
    ok, msg = perform_update()
    print("-" * 50)
    print(f"Result: {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
