"""Managed local USB port forwarding through the external ``iproxy`` tool."""
from __future__ import annotations

import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Sequence

from .errors import IProxyNotFoundError, TunnelError


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
            raise IProxyNotFoundError(f"iproxy executable not found: {self.executable!r}; install libimobiledevice and configure tunnel.iproxy") from exc
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
