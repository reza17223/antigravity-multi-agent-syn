"""Build standalone Windows .exe binaries for agsync CLI and Control Panel using PyInstaller."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build_binaries(*, dry_run: bool = False) -> list[list[str]]:
    pyinstaller = shutil.which("pyinstaller") or sys.executable
    prefix = [pyinstaller] if shutil.which("pyinstaller") else [sys.executable, "-m", "PyInstaller"]

    cmds = [
        [
            *prefix,
            "--onefile",
            "--name",
            "agsync",
            str(ROOT / "agsync" / "cli.py"),
        ],
        [
            *prefix,
            "--onefile",
            "--windowed",
            "--name",
            "agsync-panel",
            str(ROOT / "scripts" / "control_panel.py"),
        ],
    ]
    if dry_run:
        return cmds
    for cmd in cmds:
        subprocess.run(cmd, cwd=str(ROOT), check=True)
    return cmds


if __name__ == "__main__":
    build_binaries(dry_run="--dry-run" in sys.argv)
