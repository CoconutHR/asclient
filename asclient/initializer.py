"""Configuration bootstrap for first-time setup.

`init` is the single write path for creating a full configuration file.
`doctor` stays read-only apart from its existing `--fix-iproxy` repair.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .client import DeviceAddress
from .config import config_path


LOOPBACK_ADDRESS = "127.0.0.1:9096"
DEFAULT_LOG_PORT = 10102


def build_config(*, address: str = LOOPBACK_ADDRESS, iproxy: str | None = None) -> dict[str, Any]:
    """Return a complete configuration object with every key present.

    JSON has no comments, so writing every key with its default is the only
    self-documenting form available: the file itself shows what is tunable.
    """
    DeviceAddress.parse(address)
    return {
        "language": "auto",
        "device": {
            "address": address,
            "password": "",
            "timeout": 15.0,
            "retries": 1,
        },
        "tunnel": {
            "iproxy": iproxy or "iproxy",
            "local_host": "127.0.0.1",
            "local_port": 9096,
            "remote_port": 9096,
            "local_log_port": DEFAULT_LOG_PORT,
            "remote_log_port": DEFAULT_LOG_PORT,
            "forward_logs": True,
            "udid": "",
            "startup_timeout": 8,
        },
    }


def detect_iproxy() -> str | None:
    """Return an absolute iproxy path when one is already on PATH."""
    return shutil.which("iproxy")


def render_config(config: dict[str, Any]) -> str:
    """Serialize a configuration exactly as it would be written to disk."""
    return json.dumps(config, ensure_ascii=False, indent=2) + "\n"


def _git(directory: Path, *arguments: str) -> int | None:
    """Run a quiet git command, returning its exit code or None when unusable."""
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=str(directory),
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.returncode


def is_git_ignored(target: Path) -> bool | None:
    """Return True when git ignores the target, False when it is tracked-visible.

    None means the question does not apply: git is unavailable, or the file
    lives outside a repository. The project forbids committing passwords,
    UDIDs and intranet addresses, so an unignored config inside a repository
    deserves a warning rather than silence.
    """
    directory = target.parent
    ignored = _git(directory, "check-ignore", "-q", str(target))
    if ignored == 0:
        return True
    if ignored != 1:
        return None
    inside_repository = _git(directory, "rev-parse", "--is-inside-work-tree")
    return False if inside_repository == 0 else None


def write_config(config: dict[str, Any], path: str | Path | None = None, *, force: bool = False) -> Path:
    """Write a configuration file, refusing to clobber an existing one.

    An existing file may hold a password or UDID, so overwriting is opt-in.
    """
    target = config_path(path)
    if target.exists() and not force:
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_config(config), encoding="utf-8")
    return target.resolve()
