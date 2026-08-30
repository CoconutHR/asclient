"""Dependency-free client for AScript's local iOS device service."""

from importlib.metadata import PackageNotFoundError, version as _package_version

from .client import AScriptClient, DeviceAddress, ImageMatch, LogEntry, OcrItem, OcrResult
from .automation import Device, Selector, UiCollection, UiObject, UiSnapshot, SnapshotNode, SnapshotCollection, WatchRule, Watcher
from .vision import PixelColor, ScreenFrame
from .run import Run
from .tunnel import AScriptTunnel, IProxyTunnel
from .errors import AScriptError, DeviceConnectionError, DeviceLockTimeoutError, DeviceOperationError, DeviceResponseError, IProxyNotFoundError, ProtocolError, TunnelError

try:
    __version__ = _package_version("asclient")
except PackageNotFoundError:  # running from a source checkout without installation
    __version__ = "0.0.0+unknown"


def connect(address: str, *, password: str = "", timeout: float = 15.0, retries: int = 1, lock_id: str | None = None, lock_timeout: float | None = None) -> Device:
    """Connect to an AScript device using a uiautomator2-like entry point."""
    return Device(AScriptClient(address, password=password, timeout=timeout, retries=retries, lock_id=lock_id, lock_timeout=lock_timeout))

__all__ = ["AScriptClient", "DeviceAddress", "ImageMatch", "LogEntry", "OcrItem", "OcrResult", "PixelColor", "ScreenFrame", "Device", "Selector", "UiCollection", "UiObject", "UiSnapshot", "SnapshotNode", "SnapshotCollection", "WatchRule", "Watcher", "Run", "AScriptTunnel", "IProxyTunnel", "connect", "AScriptError", "DeviceConnectionError", "DeviceLockTimeoutError", "DeviceOperationError", "DeviceResponseError", "ProtocolError", "TunnelError", "IProxyNotFoundError"]
