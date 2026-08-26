"""JSON configuration loading with small, explicit validation."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


DEFAULT_CONFIG_NAME = "asclient.json"


def config_path(path: str | Path | None = None) -> Path:
    return Path(path) if path else Path.cwd() / DEFAULT_CONFIG_NAME


def load_config(path: str | Path | None = None, *, required: bool = False) -> dict[str, Any]:
    """Load a JSON object, returning an empty config when the default is absent."""
    target = config_path(path)
    if not target.is_file():
        if required: raise FileNotFoundError(target)
        return {}
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON configuration {target}: {exc}") from exc
    if not isinstance(value, dict): raise ValueError(f"configuration {target} must be a JSON object")
    return value


def save_config(config: Mapping[str, Any], path: str | Path | None = None) -> Path:
    """Atomically save a JSON configuration object without losing Unicode data."""
    target = config_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(dict(config), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target.resolve()


def language_option(config: Mapping[str, Any]) -> str | None:
    """Return the optional command-output language from a loaded config."""
    value = config.get("language")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("configuration key 'language' must be a string")
    return value


def device_options(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return validated device options from a loaded config object."""
    value = config.get("device", {})
    if not isinstance(value, Mapping): raise ValueError("configuration key 'device' must be an object")
    result = dict(value)
    if "address" in result and not isinstance(result["address"], str): raise ValueError("device.address must be a string")
    if "password" in result and not isinstance(result["password"], str): raise ValueError("device.password must be a string")
    for key in ("timeout", "retries"):
        if key in result and not isinstance(result[key], (int, float)): raise ValueError(f"device.{key} must be numeric")
    return result


def tunnel_options(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return validated optional iproxy settings from a config object."""
    value = config.get("tunnel", {})
    if not isinstance(value, Mapping): raise ValueError("configuration key 'tunnel' must be an object")
    result = dict(value)
    for key in ("iproxy", "udid", "local_host"):
        if key in result and not isinstance(result[key], str): raise ValueError(f"tunnel.{key} must be a string")
    for key in ("local_port", "remote_port", "local_log_port", "remote_log_port", "startup_timeout"):
        if key in result and not isinstance(result[key], (int, float)): raise ValueError(f"tunnel.{key} must be numeric")
    if "forward_logs" in result and not isinstance(result["forward_logs"], bool): raise ValueError("tunnel.forward_logs must be a boolean")
    return result
