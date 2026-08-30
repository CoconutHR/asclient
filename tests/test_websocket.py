import base64
import hashlib
import struct
import threading
import time
import unittest
from unittest.mock import patch

from asclient import ProtocolError
from asclient._websocket import WebSocket


class FakeSocket:
    def __init__(self, incoming=b""):
        self.incoming = incoming
        self.sent = bytearray()
        self.closed = False
        self.timeout = None

    def recv(self, size):
        result, self.incoming = self.incoming[:size], self.incoming[size:]
        return result

    def sendall(self, data):
        self.sent.extend(data)

    def close(self):
        self.closed = True

    def settimeout(self, timeout):
        self.timeout = timeout


def frame(opcode, payload=b"", *, final=True, masked=False):
    first = opcode | (0x80 if final else 0)
    mask = b"\x01\x02\x03\x04" if masked else b""
    if len(payload) < 126:
        header = bytes((first, len(payload) | (0x80 if masked else 0)))
    else:
        header = bytes((first, 126 | (0x80 if masked else 0))) + struct.pack(">H", len(payload))
    if masked:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return header + mask + payload


def close_code(sent):
    size = sent[1] & 0x7F
    mask, payload = sent[2:6], sent[6:6 + size]
    return struct.unpack(">H", bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))[0]


class WebSocketTests(unittest.TestCase):
    def test_connect_validates_handshake_and_preserves_buffered_frame(self):
        sock = FakeSocket()
        key = base64.b64encode(b"k" * 16).decode("ascii")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
        sock.incoming = f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: keep-alive, Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode("ascii") + frame(0x1, b"ready")
        with patch("asclient._websocket.socket.create_connection", return_value=sock), patch("asclient._websocket.secrets.token_bytes", return_value=b"k" * 16):
            websocket = WebSocket.connect("device", 10102, "/log/", timeout=3, headers={"Cookie": "airscript=pw"})
        self.assertIn(b"GET /log/ HTTP/1.1", sock.sent)
        self.assertIn(b"Cookie: airscript=pw", sock.sent)
        self.assertEqual(websocket.receive(), (0x1, b"ready"))

    def test_receive_reassembles_fragments_and_answers_ping(self):
        sock = FakeSocket(frame(0x1, b"hel", final=False) + frame(0x9, b"?", final=True) + frame(0x0, b"lo", final=True))
        websocket = WebSocket(sock)
        self.assertEqual(websocket.receive(), (0x1, b"hello"))
        self.assertEqual(sock.sent[0] & 0x0F, 0xA)
        self.assertTrue(sock.sent[1] & 0x80)
        self.assertEqual(sock.sent[1] & 0x7F, 1)

    def test_close_is_echoed_and_marks_connection_closed(self):
        sock = FakeSocket(frame(0x8, struct.pack(">H", 1000) + b"done"))
        websocket = WebSocket(sock)
        self.assertIsNone(websocket.receive())
        self.assertTrue(websocket.closed)
        self.assertEqual(sock.sent[0] & 0x0F, 0x8)
        self.assertTrue(sock.sent[1] & 0x80)

    def test_rejects_server_masking_invalid_text_and_invalid_handshake(self):
        with self.assertRaisesRegex(ProtocolError, "must not be masked"):
            WebSocket(FakeSocket(frame(0x1, b"bad", masked=True))).receive()
        websocket = WebSocket(FakeSocket(frame(0x1, b"\xff")))
        with self.assertRaisesRegex(ProtocolError, "invalid UTF-8"):
            websocket.receive()
        self.assertEqual(close_code(websocket._sock.sent), 1007)
        sock = FakeSocket(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: invalid\r\n\r\n")
        with self.assertRaisesRegex(ProtocolError, "Sec-WebSocket-Accept"):
            WebSocket._validate_handshake(sock.incoming.split(b"\r\n\r\n", 1)[0], "key")

    def test_handshake_combines_list_headers_but_rejects_duplicate_accept(self):
        key = "key"
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
        response = f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: keep-alive\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}".encode("ascii")
        WebSocket._validate_handshake(response, key)
        duplicate = response + f"\r\nSec-WebSocket-Accept: {accept}".encode("ascii")
        with self.assertRaisesRegex(ProtocolError, "Sec-WebSocket-Accept"):
            WebSocket._validate_handshake(duplicate, key)

    def test_protocol_errors_send_non_normal_close(self):
        websocket = WebSocket(FakeSocket(frame(0x1, b"bad", masked=True)))
        with self.assertRaisesRegex(ProtocolError, "must not be masked"):
            websocket.receive()
        websocket.close()
        self.assertEqual(close_code(websocket._sock.sent), 1002)

    def test_header_read_observes_deadline_and_stop_event(self):
        sock = FakeSocket()
        with self.assertRaisesRegex(TimeoutError, "timed out"):
            WebSocket._read_headers(sock, deadline=time.monotonic() - 1)
        stopped = threading.Event()
        stopped.set()
        with self.assertRaisesRegex(TimeoutError, "stopped"):
            WebSocket._read_headers(sock, stop_event=stopped)

    def test_rejects_invalid_frame_shapes_and_oversized_messages(self):
        with self.assertRaisesRegex(ProtocolError, "control frame"):
            WebSocket(FakeSocket(frame(0x9, b"x", final=False))).receive()
        with self.assertRaisesRegex(ProtocolError, "continuation"):
            WebSocket(FakeSocket(frame(0x0, b"orphan"))).receive()
        with patch("asclient._websocket._MAX_MESSAGE_SIZE", 3):
            with self.assertRaisesRegex(ProtocolError, "oversized"):
                WebSocket(FakeSocket(frame(0x1, b"long"))).receive()


if __name__ == "__main__":
    unittest.main()
