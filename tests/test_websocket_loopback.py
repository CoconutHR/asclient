import base64
import hashlib
import queue
import socket
import struct
import threading
import time
import unittest
from unittest.mock import patch

from asclient._websocket import WebSocket
from asclient.client import AScriptClient


def websocket_frame(opcode, payload=b""):
    size = len(payload)
    if size < 126:
        header = bytes((0x80 | opcode, size))
    else:
        header = bytes((0x80 | opcode, 126)) + struct.pack(">H", size)
    return header + payload


def read_headers(conn):
    data = b""
    while b"\r\n\r\n" not in data:
        part = conn.recv(4096)
        if not part:
            raise AssertionError("client closed before completing its handshake")
        data += part
    return data


def accept_for(request):
    key = next(
        line.split(b":", 1)[1].strip()
        for line in request.split(b"\r\n")
        if line.lower().startswith(b"sec-websocket-key:")
    )
    value = base64.b64encode(
        hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
    )
    return (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + value + b"\r\n\r\n"
    )


class LoopbackServer:
    """Run a fixed number of short-lived TCP WebSocket peers."""

    def __init__(self, handlers):
        self._handlers = iter(handlers)
        self._errors = queue.Queue()
        self._started = threading.Event()
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self._listener.listen()
        self._listener.settimeout(0.05)
        self._thread = threading.Thread(target=self._serve, name="websocket-loopback")

    def __enter__(self):
        self._thread.start()
        if not self._started.wait(1):
            self.close()
            raise AssertionError("loopback server did not start")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        if not self._errors.empty() and exc is None:
            raise self._errors.get()

    def _serve(self):
        self._started.set()
        try:
            for handler in self._handlers:
                while not self._stop.is_set():
                    try:
                        conn, _address = self._listener.accept()
                        break
                    except socket.timeout:
                        continue
                    except OSError:
                        return
                else:
                    return
                with conn:
                    conn.settimeout(1)
                    handler(conn)
        except BaseException as exc:
            self._errors.put(exc)
        finally:
            self._listener.close()

    def close(self):
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        self._thread.join(2)
        if self._thread.is_alive():
            raise AssertionError("loopback server did not stop")


class WebSocketLoopbackTests(unittest.TestCase):
    def connect_to(self, server, *, timeout=1, deadline=None):
        create_connection = socket.create_connection

        def redirect(_address, *args, **kwargs):
            return create_connection(("127.0.0.1", server.port), *args, **kwargs)

        return patch("asclient._websocket.socket.create_connection", side_effect=redirect), timeout, deadline

    def test_fragmented_first_frame_over_real_tcp(self):
        first_sent = threading.Event()
        allow_rest = threading.Event()

        def handler(conn):
            conn.sendall(accept_for(read_headers(conn)))
            frame = websocket_frame(0x1, b"first")
            conn.sendall(frame[:1])
            first_sent.set()
            if not allow_rest.wait(1):
                raise AssertionError("client did not start reading the fragmented frame")
            conn.sendall(frame[1:3])
            conn.sendall(frame[3:])

        with LoopbackServer([handler]) as server:
            redirect, timeout, deadline = self.connect_to(server)
            with redirect:
                websocket = WebSocket.connect("device", 10102, "/log/", timeout=timeout, deadline=deadline)
                self.assertTrue(first_sent.wait(1))
                allow_rest.set()
                self.assertEqual(websocket.receive(), (0x1, b"first"))
                websocket.close()

    def test_slow_handshake_honors_total_deadline(self):
        received = threading.Event()

        def handler(conn):
            read_headers(conn)
            received.set()
            while conn.recv(1024):
                pass

        with LoopbackServer([handler]) as server:
            redirect, timeout, _deadline = self.connect_to(server)
            deadline = time.monotonic() + 0.15
            with redirect:
                with self.assertRaisesRegex(TimeoutError, "handshake timed out"):
                    WebSocket.connect("device", 10102, "/log/", timeout=timeout, deadline=deadline)
            self.assertTrue(received.wait(1))

    def test_slow_frame_honors_total_deadline(self):
        received = threading.Event()

        def handler(conn):
            conn.sendall(accept_for(read_headers(conn)))
            received.set()
            while conn.recv(1024):
                pass

        with LoopbackServer([handler]) as server:
            redirect, timeout, _deadline = self.connect_to(server)
            deadline = time.monotonic() + 0.15
            with redirect:
                websocket = WebSocket.connect("device", 10102, "/log/", timeout=timeout, deadline=deadline)
                with self.assertRaisesRegex(TimeoutError, "receive timed out"):
                    websocket.receive()
                websocket.close()
            self.assertTrue(received.wait(1))

    def test_logs_reconnects_after_protocol_exception(self):
        def invalid_peer(conn):
            conn.sendall(accept_for(read_headers(conn)))
            conn.sendall(bytes((0x81, 0x80)) + b"\x00\x00\x00\x00")

        def recovered_peer(conn):
            conn.sendall(accept_for(read_headers(conn)))
            conn.sendall(websocket_frame(0x1, b'{"msg":"recovered","type":"e"}'))
            conn.sendall(websocket_frame(0x8, struct.pack(">H", 1000)))

        with LoopbackServer([invalid_peer, recovered_peer]) as server:
            create_connection = socket.create_connection

            def redirect(_address, *args, **kwargs):
                return create_connection(("127.0.0.1", server.port), *args, **kwargs)

            client = AScriptClient("device", timeout=1)
            with patch("asclient._websocket.socket.create_connection", side_effect=redirect):
                entries = list(client.logs(reconnects=1, reconnect_delay=0))
        self.assertEqual([(entry.message, entry.kind) for entry in entries], [("recovered", "e")])

    def test_closing_logs_generator_releases_socket(self):
        released = threading.Event()

        def handler(conn):
            conn.sendall(accept_for(read_headers(conn)))
            conn.sendall(websocket_frame(0x1, b"one"))
            try:
                data = conn.recv(1024)
            except socket.timeout as exc:
                raise AssertionError("client did not release the log socket") from exc
            if not data:
                released.set()
                return
            if data[0] & 0x0F != 0x8:
                raise AssertionError("client did not send a close frame")
            released.set()

        with LoopbackServer([handler]) as server:
            create_connection = socket.create_connection

            def redirect(_address, *args, **kwargs):
                return create_connection(("127.0.0.1", server.port), *args, **kwargs)

            client = AScriptClient("device", timeout=1)
            with patch("asclient._websocket.socket.create_connection", side_effect=redirect):
                logs = client.logs()
                self.assertEqual(next(logs).message, "one")
                logs.close()
            self.assertTrue(released.wait(1))


if __name__ == "__main__":
    unittest.main()
