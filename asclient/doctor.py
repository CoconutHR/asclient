"""Non-destructive local and device diagnostics, with explicit config repairs."""
from __future__ import annotations

import json
import shutil
import socket
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .client import AScriptClient
from .config import config_path, save_config, tunnel_options
from .errors import AScriptError
from .i18n import t


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    message: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _port_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((host, port))
        return True
    except OSError:
        return False


def _executable_path(value: str) -> str | None:
    candidate = Path(value)
    if candidate.is_file():
        return str(candidate.resolve())
    return shutil.which(value)


def diagnose(client: AScriptClient, config: Mapping[str, Any]) -> list[DoctorCheck]:
    """Return checks without changing the machine, config, or device."""
    options = tunnel_options(config)
    executable = str(options.get("iproxy", "iproxy"))
    checks: list[DoctorCheck] = []
    found = _executable_path(executable)
    if found:
        checks.append(DoctorCheck("iproxy", "ok", t("doctor_iproxy_found", path=found)))
    elif client.address.host in {"127.0.0.1", "localhost"}:
        # USB 隧道场景（回环地址）确实需要 iproxy，缺失即为错误。
        checks.append(DoctorCheck("iproxy", "error", t("doctor_iproxy_missing"), executable))
    else:
        # 纯 Wi-Fi 场景不需要 iproxy；缺失只提醒，不让 doctor 返回失败退出码。
        checks.append(DoctorCheck("iproxy", "warning", t("doctor_iproxy_missing_optional"), executable))
    host = str(options.get("local_host", "127.0.0.1"))
    ports = [("service_port", int(options.get("local_port", 9096)))]
    if bool(options.get("forward_logs", True)):
        ports.append(("log_port", int(options.get("local_log_port", 10102))))
    probe = AScriptClient(client.address, password=client.password, timeout=min(client.timeout, 3.0), retries=0)
    device_reachable = False
    try:
        platform = probe.ping()
        device_reachable = True
        checks.append(DoctorCheck("device", "ok", t("doctor_device_ok", platform=platform)))
        status = probe.status()
        if "status_api_error" in status:
            checks.append(DoctorCheck("status_api", "warning", t("doctor_status_fallback", detail=status["status_api_error"]), str(status["status_api_error"])))
        else:
            checks.append(DoctorCheck("status_api", "ok", t("doctor_status_ok")))
    except AScriptError as exc:
        checks.append(DoctorCheck("device", "error", t("doctor_device_failed", detail=exc), str(exc)))
    active_local_tunnel = client.address.host in {"127.0.0.1", "localhost"} and device_reachable
    for name, port in ports:
        if _port_available(host, port):
            checks.append(DoctorCheck(name, "ok", t("doctor_port_available", host=host, port=port)))
        elif active_local_tunnel:
            checks.append(DoctorCheck(name, "ok", t("doctor_port_tunnel", host=host, port=port), "active_local_tunnel"))
        else:
            checks.append(DoctorCheck(name, "warning", t("doctor_port_busy", host=host, port=port)))
    try:
        with socket.create_connection((client.address.host, 10102), timeout=min(client.timeout, 3)):
            pass
        checks.append(DoctorCheck("log_service", "ok", t("doctor_log_ok", host=client.address.host)))
    except OSError as exc:
        checks.append(DoctorCheck("log_service", "warning", t("doctor_log_failed", host=client.address.host, detail=exc), str(exc)))
    return checks


def set_iproxy_path(config: Mapping[str, Any], executable: str, *, path: str | Path | None = None) -> Path:
    """Validate and persist an explicit iproxy executable path."""
    candidate = Path(executable).expanduser()
    if not candidate.is_file():
        raise ValueError(t("doctor_fix_invalid", path=candidate))
    updated = dict(config)
    tunnel = dict(updated.get("tunnel") or {})
    tunnel["iproxy"] = str(candidate.resolve())
    updated["tunnel"] = tunnel
    return save_config(updated, path)


def planned_iproxy_fix(config_file: str | Path | None, executable: str) -> str:
    return t("doctor_fix_plan", path=config_path(config_file).resolve(), executable=Path(executable).expanduser())


def save_report(checks: list[DoctorCheck], destination: str | Path, *, client: AScriptClient) -> Path:
    """Write a password-free JSON diagnostic record for incident evidence."""
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "device": str(client.address),
        "checks": [check.as_dict() for check in checks],
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target.resolve()
