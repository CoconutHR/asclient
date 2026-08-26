"""Managed local USB port forwarding through the external ``iproxy`` tool."""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from .errors import IProxyNotFoundError, TunnelError


def _iproxy_not_found_message(executable: str) -> str:
    """Return an actionable bilingual platform-specific missing-iproxy error."""
    chinese_error = f"未找到 iproxy 可执行文件: {executable!r}."
    english_error = f"iproxy executable not found: {executable!r}."
    if sys.platform == "win32":
        return (
            f"{chinese_error}\n"
            "Windows：请安装包含 iproxy.exe 的可信 libimobiledevice 发行版；然后将其目录加入 PATH 并执行 "
            "'where iproxy' 验证，或在 asclient.json 中设置 "
            '"tunnel.iproxy": "C:\\\\tools\\\\libimobiledevice\\\\iproxy.exe"。\n'
            f"{english_error}\n"
            "Windows: install a trusted libimobiledevice build that includes iproxy.exe; then add its directory "
            "to PATH and verify with 'where iproxy', or configure the absolute executable path in asclient.json."
        )
    if sys.platform == "darwin":
        return f"{chinese_error}\nmacOS：安装 libimobiledevice（例如 'brew install libimobiledevice'）后执行 'which iproxy' 验证，或设置 tunnel.iproxy 为绝对路径。\n{english_error}\nmacOS: install libimobiledevice (for example: 'brew install libimobiledevice') and verify with 'which iproxy', or set tunnel.iproxy to its absolute path."
    return f"{chinese_error}\nLinux：安装发行版提供的 libimobiledevice 包后执行 'command -v iproxy' 验证，或设置 tunnel.iproxy 为绝对路径。\n{english_error}\nLinux: install your distribution's libimobiledevice package and verify with 'command -v iproxy', or set tunnel.iproxy to its absolute path."


@dataclass
class IProxyTunnel:
    """Forward one local TCP port to an iOS device port over USB.

    ``iproxy`` is intentionally an explicit external dependency. It is the
    portable libimobiledevice implementation of the USB multiplexing bridge;
    ASClient owns its process lifetime but does not bundle its binary.
    """

    local_port: int = 9096
    remote_port: int = 9096
    udid: str = ""
    executable: str = "iproxy"
    local_host: str = "127.0.0.1"
    startup_timeout: float = 8.0
    _process: subprocess.Popen[str] | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        for name, value in (("local_port", self.local_port), ("remote_port", self.remote_port)):
            if not isinstance(value, int) or not 1 <= value <= 65535: raise ValueError(f"{name} must be between 1 and 65535")
        if self.local_host not in {"127.0.0.1", "localhost"}: raise ValueError("local_host must be loopback for a USB tunnel")
        if self.startup_timeout <= 0: raise ValueError("startup_timeout must be positive")

    @property
    def address(self) -> str:
        return f"{self.local_host}:{self.local_port}"

    @property
    def command(self) -> list[str]:
        command = [self.executable]
        if self.udid: command += ["-u", self.udid]
        return command + [str(self.local_port), str(self.remote_port)]

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> "IProxyTunnel":
        if self.is_running: return self
        try:
            self._process = subprocess.Popen(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except FileNotFoundError as exc:
            raise IProxyNotFoundError(_iproxy_not_found_message(self.executable)) from exc
        except OSError as exc:
            raise TunnelError(f"cannot start iproxy {self.command!r}: {exc}") from exc
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                detail = (self._process.stderr.read() if self._process.stderr else "").strip()
                self._process = None
                raise TunnelError(f"iproxy exited during startup ({detail or 'no stderr'})")
            try:
                with socket.create_connection((self.local_host, self.local_port), timeout=0.2):
                    return self
            except OSError:
                time.sleep(0.1)
        self.stop()
        raise TunnelError(f"iproxy did not listen on {self.address} within {self.startup_timeout}s")

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None: return
        process.terminate()
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=3)

    def __enter__(self) -> "IProxyTunnel": return self.start()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.stop()
        return False


@dataclass
class AScriptTunnel:
    """Forward the AScript service and optional log stream over USB.

    This is the preferred USB tunnel for AScript: the HTTP service uses port
    ``9096`` and the log WebSocket uses port ``10102``.  ``IProxyTunnel``
    remains available when an application needs an individual custom mapping.
    """

    local_port: int = 9096
    remote_port: int = 9096
    local_log_port: int = 10102
    remote_log_port: int = 10102
    forward_logs: bool = True
    udid: str = ""
    executable: str = "iproxy"
    local_host: str = "127.0.0.1"
    startup_timeout: float = 8.0
    service: IProxyTunnel = field(init=False)
    logs: IProxyTunnel | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if not isinstance(self.forward_logs, bool):
            raise ValueError("forward_logs must be a boolean")
        if self.forward_logs and self.local_port == self.local_log_port:
            raise ValueError("local_port and local_log_port must differ when forwarding logs")
        common = {
            "udid": self.udid,
            "executable": self.executable,
            "local_host": self.local_host,
            "startup_timeout": self.startup_timeout,
        }
        self.service = IProxyTunnel(self.local_port, self.remote_port, **common)
        if self.forward_logs:
            self.logs = IProxyTunnel(self.local_log_port, self.remote_log_port, **common)

    @property
    def address(self) -> str:
        """Local address used for normal AScript HTTP requests."""
        return self.service.address

    @property
    def log_address(self) -> str | None:
        """Local address of the AScript log WebSocket, when enabled."""
        return self.logs.address if self.logs else None

    @property
    def is_running(self) -> bool:
        return self.service.is_running and (self.logs is None or self.logs.is_running)

    def start(self) -> "AScriptTunnel":
        self.service.start()
        try:
            if self.logs:
                self.logs.start()
        except Exception:
            self.service.stop()
            raise
        return self

    def stop(self) -> None:
        if self.logs:
            self.logs.stop()
        self.service.stop()

    def __enter__(self) -> "AScriptTunnel":
        return self.start()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.stop()
        return False
