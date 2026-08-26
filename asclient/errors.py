"""Exceptions raised by the AScript local-device client."""


class AScriptError(Exception):
    """Base class for all library errors."""


class DeviceConnectionError(AScriptError):
    """The device service could not be reached."""


class DeviceResponseError(AScriptError):
    """The device returned an HTTP error or malformed response."""

    def __init__(self, message, *, status=None, body=""):
        super().__init__(message)
        self.status = status
        self.body = body


class DeviceOperationError(AScriptError):
    """The device accepted a request but reported an operation failure."""


class ProtocolError(AScriptError):
    """An invalid WebSocket or AScript response was received."""


class TunnelError(AScriptError):
    """A local USB port-forwarding tunnel could not be started or kept alive."""


class IProxyNotFoundError(TunnelError):
    """The configured iproxy executable is not installed or not executable."""
