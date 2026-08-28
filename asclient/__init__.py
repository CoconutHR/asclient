"""Dependency-free client for AScript's local iOS device service."""

from .client import AScriptClient, DeviceAddress, ImageMatch, LogEntry, OcrItem, OcrResult
from .automation import Device, Selector, UiCollection, UiObject, UiSnapshot, SnapshotNode, SnapshotCollection, WatchRule, Watcher
from .vision import PixelColor, ScreenFrame
from .run import Run
from .tunnel import AScriptTunnel, IProxyTunnel
from .errors import AScriptError, DeviceConnectionError, DeviceOperationError, DeviceResponseError, IProxyNotFoundError, TunnelError


def connect(address: str, *, password: str = "", timeout: float = 15.0, retries: int = 1) -> Device:
    """Connect to an AScript device using a uiautomator2-like entry point."""
    return Device(AScriptClient(address, password=password, timeout=timeout, retries=retries))

__all__ = ["AScriptClient", "DeviceAddress", "ImageMatch", "LogEntry", "OcrItem", "OcrResult", "PixelColor", "ScreenFrame", "Device", "Selector", "UiCollection", "UiObject", "UiSnapshot", "SnapshotNode", "SnapshotCollection", "WatchRule", "Watcher", "Run", "AScriptTunnel", "IProxyTunnel", "connect", "AScriptError", "DeviceConnectionError", "DeviceOperationError", "DeviceResponseError", "TunnelError", "IProxyNotFoundError"]
