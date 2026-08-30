"""Minimal RFC 6455 client transport used by the log stream."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import socket
import struct
import threading
import time
from collections.abc import Mapping

from .errors import ProtocolError


_MAX_HANDSHAKE_SIZE = 64 * 1024
_MAX_MESSAGE_SIZE = 8 * 1024 * 1024
_OPCODES = {0x0, 0x1, 0x2, 0x8, 0x9, 0xA}


class WebSocket:
    """A synchronous, dependency-free WebSocket client connection."""

    def __init__(self, sock: socket.socket, buffered: bytes = b"", *, deadline: float | None = None, stop_event: threading.Event | None = None) -> None:
        self._sock = sock
        self._buffer = buffered
        self._deadline = deadline
        self._stop_event = stop_event
        self._fragment_opcode: int | None = None
        self._fragments: list[bytes] = []
        self._fragment_size = 0
        self.closed = False
        self.close_code: int | None = None

    @classmethod
    def connect(cls, host: str, port: int, path: str, *, timeout: float, headers: Mapping[str, str] | None = None, deadline: float | None = None, stop_event: threading.Event | None = None) -> "WebSocket":
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            request = [
                f"GET {path} HTTP/1.1",
                f"Host: {host}:{port}",
                "Upgrade: websocket",
                "Connection: Upgrade",
                f"Sec-WebSocket-Key: {key}",
                "Sec-WebSocket-Version: 13",
            ]
            request.extend(f"{name}: {value}" for name, value in (headers or {}).items())
            sock.sendall(("\r\n".join(request) + "\r\n\r\n").encode("ascii"))
            response, buffered = cls._read_headers(sock, deadline=deadline, stop_event=stop_event)
            cls._validate_handshake(response, key)
            return cls(sock, buffered, deadline=deadline, stop_event=stop_event)
        except Exception:
            sock.close()
            raise

    @staticmethod
    def _read_headers(sock: socket.socket, *, deadline: float | None = None, stop_event: threading.Event | None = None) -> tuple[bytes, bytes]:
        data = b""
        while b"\r\n\r\n" not in data:
            if stop_event is not None and stop_event.is_set():
                raise TimeoutError("WebSocket handshake stopped")
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("WebSocket handshake timed out")
                sock.settimeout(min(0.25, remaining))
            try:
                part = sock.recv(4096)
            except TimeoutError:
                if deadline is not None or stop_event is not None:
                    continue
                raise
            if not part:
                raise ProtocolError("unexpected EOF during WebSocket handshake")
            data += part
            if len(data) > _MAX_HANDSHAKE_SIZE:
                raise ProtocolError("oversized WebSocket handshake")
        return data.split(b"\r\n\r\n", 1)

    @staticmethod
    def _validate_handshake(response: bytes, key: str) -> None:
        try:
            lines = response.decode("iso-8859-1").split("\r\n")
            version, status, _reason = lines[0].split(" ", 2)
        except (UnicodeDecodeError, IndexError, ValueError) as exc:
            raise ProtocolError("malformed WebSocket handshake response") from exc
        if version != "HTTP/1.1" or status != "101":
            raise ProtocolError("WebSocket upgrade rejected")
        headers: dict[str, list[str]] = {}
        for line in lines[1:]:
            if not line or ":" not in line:
                raise ProtocolError("malformed WebSocket handshake header")
            name, value = line.split(":", 1)
            name, value = name.strip().lower(), value.strip()
            if not name:
                raise ProtocolError("malformed WebSocket handshake header")
            headers.setdefault(name, []).append(value)
        upgrade = {item.strip().lower() for value in headers.get("upgrade", []) for item in value.split(",")}
        if upgrade != {"websocket"}:
            raise ProtocolError("missing WebSocket Upgrade response header")
        connection = {item.strip().lower() for value in headers.get("connection", []) for item in value.split(",")}
        if "upgrade" not in connection:
            raise ProtocolError("missing WebSocket Connection response header")
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
        accept = headers.get("sec-websocket-accept")
        if accept is None or len(accept) != 1 or not hmac.compare_digest(accept[0], expected):
            raise ProtocolError("invalid Sec-WebSocket-Accept response header")
        if "sec-websocket-protocol" in headers or "sec-websocket-extensions" in headers:
            raise ProtocolError("unsupported WebSocket protocol or extension")

    def settimeout(self, timeout: float | None) -> None:
        """Set the timeout used by subsequent reads."""
        self._sock.settimeout(timeout)

    def _recv_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            if self._stop_event is not None and self._stop_event.is_set():
                raise TimeoutError("WebSocket receive stopped")
            if self._deadline is not None:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("WebSocket receive timed out")
                self._sock.settimeout(min(0.25, remaining))
            try:
                part = self._sock.recv(max(4096, size - len(self._buffer)))
            except TimeoutError:
                if self._deadline is not None or self._stop_event is not None:
                    continue
                raise
            if not part:
                raise self._protocol_error("unexpected EOF during WebSocket frame")
            self._buffer += part
        result, self._buffer = self._buffer[:size], self._buffer[size:]
        return result

    def _read_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._recv_exact(2)
        final, rsv, opcode, masked, size = bool(first & 0x80), first & 0x70, first & 0x0F, bool(second & 0x80), second & 0x7F
        if rsv or opcode not in _OPCODES:
            raise self._protocol_error("invalid WebSocket frame flags or opcode")
        if masked:
            raise self._protocol_error("server WebSocket frames must not be masked")
        if size == 126:
            size = struct.unpack(">H", self._recv_exact(2))[0]
            if size < 126:
                raise self._protocol_error("non-minimal WebSocket frame length")
        elif size == 127:
            size = struct.unpack(">Q", self._recv_exact(8))[0]
            if size & (1 << 63):
                raise self._protocol_error("invalid WebSocket frame length")
            if size < 65536:
                raise self._protocol_error("non-minimal WebSocket frame length")
        if size > _MAX_MESSAGE_SIZE:
            raise self._protocol_error("oversized WebSocket frame")
        if opcode >= 0x8 and (not final or size > 125):
            raise self._protocol_error("invalid WebSocket control frame")
        return final, opcode, self._recv_exact(size)

    def receive(self) -> tuple[int, bytes] | None:
        """Return the next complete text/binary message, or ``None`` on close."""
        while not self.closed:
            final, opcode, payload = self._read_frame()
            if opcode == 0x8:
                if len(payload) == 1:
                    raise self._protocol_error("invalid WebSocket close payload")
                if len(payload) >= 2:
                    code = struct.unpack(">H", payload[:2])[0]
                    if code < 1000 or code in {1004, 1005, 1006, 1015} or 1016 <= code <= 2999 or code >= 5000:
                        raise self._protocol_error("invalid WebSocket close code")
                    try:
                        payload[2:].decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise self._protocol_error("invalid WebSocket close reason", 1007) from exc
                    self.close_code = code
                self._send_frame(0x8, payload)
                self.closed = True
                return None
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x0:
                if self._fragment_opcode is None:
                    raise self._protocol_error("unexpected WebSocket continuation frame")
                self._append_fragment(payload)
                if final:
                    opcode, payload = self._fragment_opcode, b"".join(self._fragments)
                    self._fragment_opcode, self._fragments, self._fragment_size = None, [], 0
                    return self._complete_message(opcode, payload)
                continue
            if self._fragment_opcode is not None:
                raise self._protocol_error("new WebSocket data frame during fragmentation")
            if final:
                return self._complete_message(opcode, payload)
            self._fragment_opcode = opcode
            self._fragments, self._fragment_size = [payload], len(payload)
            if self._fragment_size > _MAX_MESSAGE_SIZE:
                raise self._protocol_error("oversized WebSocket message")
        return None

    def _complete_message(self, opcode: int, payload: bytes) -> tuple[int, bytes]:
        if opcode == 0x1:
            try:
                payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise self._protocol_error("invalid UTF-8 WebSocket text message", 1007) from exc
        return opcode, payload

    def _protocol_error(self, message: str, close_code: int = 1002) -> ProtocolError:
        if not self.closed:
            try:
                self._send_frame(0x8, struct.pack(">H", close_code))
            except OSError:
                pass
            self.closed = True
        return ProtocolError(message)

    def _append_fragment(self, payload: bytes) -> None:
        self._fragment_size += len(payload)
        if self._fragment_size > _MAX_MESSAGE_SIZE:
            raise self._protocol_error("oversized WebSocket message")
        self._fragments.append(payload)

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        mask = secrets.token_bytes(4)
        size = len(payload)
        if size < 126:
            header = bytes([0x80 | opcode, 0x80 | size])
        elif size < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", size)
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", size)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._sock.sendall(header + mask + masked)

    def close(self) -> None:
        if not self.closed:
            try:
                self._send_frame(0x8, struct.pack(">H", 1000))
            except OSError:
                pass
            self.closed = True
        self._sock.close()
