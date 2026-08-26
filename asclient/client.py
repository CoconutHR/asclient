"""AScript local-device client. Public APIs are synchronous and dependency-free."""
from __future__ import annotations

import base64
import ipaddress
import json
import re
import secrets
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Optional

from .errors import AScriptError, DeviceConnectionError, DeviceOperationError, DeviceResponseError, ProtocolError


@dataclass(frozen=True)
class DeviceAddress:
    host: str
    port: int = 9096

    @classmethod
    def parse(cls, value: str | "DeviceAddress") -> "DeviceAddress":
        if isinstance(value, cls):
            return value
        value = value.strip().removeprefix("http://").removeprefix("https://").rstrip("/")
        if not value:
            raise ValueError("device address is empty")
        host, sep, port = value.rpartition(":")
        if not sep:
            return cls(value)
        if not host or not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ValueError("device address must be HOST[:PORT]")
        return cls(host, int(port))

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class LogEntry:
    message: str
    kind: str = "o"
    timestamp: str = ""


class AScriptClient:
    """Client for one AScript device-service endpoint.

    ``request`` intentionally remains public for confirmed but not yet
    high-level-wrapped endpoints.
    """

    def __init__(self, address: str | DeviceAddress, *, password: str = "", timeout: float = 15.0, retries: int = 1):
        self.address = DeviceAddress.parse(address)
        self.password, self.timeout, self.retries = password, float(timeout), max(0, int(retries))

    @property
    def base_url(self) -> str:
        return f"http://{self.address}"

    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        result = dict(extra or {})
        if self.password:
            result["Cookie"] = f"airscript={self.password}"
        return result

    def request(self, method: str, path: str, *, params: Optional[Mapping[str, Any]] = None, form: Optional[Mapping[str, Any]] = None, data: Optional[bytes] = None, headers: Optional[Mapping[str, str]] = None, timeout: Optional[float] = None) -> bytes:
        if not path.startswith("/"):
            raise ValueError("path must start with '/'")
        if form is not None and data is not None:
            raise ValueError("form and data are mutually exclusive")
        if params:
            path += ("&" if "?" in path else "?") + urllib.parse.urlencode(params, doseq=True)
        if form is not None:
            data = urllib.parse.urlencode(form, doseq=True).encode("utf-8")
            headers = {**(headers or {}), "Content-Type": "application/x-www-form-urlencoded"}
        req = urllib.request.Request(self.base_url + path, data=data, method=method.upper(), headers=self._headers(headers))
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                raise DeviceResponseError(f"HTTP {exc.code} for {path}", status=exc.code, body=exc.read().decode("utf-8", "replace")[:2000]) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(0.2 * (2 ** attempt))
        raise DeviceConnectionError(f"cannot reach AScript device at {self.address}: {last_error}") from last_error

    def json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        raw = self.request(method, path, **kwargs)
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeviceResponseError(f"invalid JSON response from {path}", body=raw[:1000].decode("utf-8", "replace")) from exc
        if not isinstance(value, dict):
            raise DeviceResponseError(f"expected object response from {path}", body=repr(value))
        return value

    @staticmethod
    def _ok(value: Mapping[str, Any]) -> dict[str, Any]:
        if value.get("code") not in (None, 1, True):
            raise DeviceOperationError(str(value.get("msg") or value))
        return dict(value)

    def ping(self) -> str:
        raw = self.request("POST", "/api/model/pip", data=b"{}", headers={"Content-Type": "application/json"}, timeout=5)
        return "iOS" if not raw.strip() else ("Android" if json.loads(raw.decode("utf-8")).get("code") == 1 else "iOS")

    def status(self) -> dict[str, Any]:
        """Return status, degrading gracefully for iOS 4001's broken endpoint.

        Its bundled implementation calls the Objective-C ``languageCode``
        property as a method, which fails on some Rubicon versions.
        """
        try:
            return self._ok(self.json("GET", "/api/status")).get("data", {})
        except DeviceOperationError as exc:
            result: dict[str, Any] = {"available": True, "status_api_error": str(exc), "platform": self.ping()}
            try:
                result["screen"] = self._ok(self.json("GET", "/api/screen/size")).get("data", {})
            except (DeviceOperationError, DeviceResponseError, DeviceConnectionError):
                pass
            try:
                result["current_app"] = self.current_app()
            except (DeviceOperationError, DeviceResponseError, DeviceConnectionError):
                pass
            return result

    def packages(self) -> list[Any]:
        """Return the installed package list reported by the iOS status API."""
        status = self.status()
        packages = status.get("python", {}).get("packages")
        if packages is not None:
            return packages
        value = self.eval_python(
            "import importlib.metadata as md, json\n"
            "_packages = {d.metadata['Name']: d.version for d in md.distributions() if d.metadata.get('Name')}\n"
            "_result = json.dumps(sorted(_packages.items()))"
        )
        return value if isinstance(value, list) else []

    def scan_subnet(self, *, workers: int = 64, probe_timeout: float = 1.0) -> list[tuple[DeviceAddress, str]]:
        """Discover AScript devices on the current IPv4 /24 subnet.

        Discovery is deliberately bounded to the local /24 and never probes
        public ranges. It is unsuitable for IPv6 or non-/24 enterprise LANs.
        """
        try:
            host = ipaddress.IPv4Address(self.address.host)
        except ipaddress.AddressValueError as exc:
            raise ValueError("scan requires an IPv4 device address as its subnet hint") from exc
        network = ipaddress.IPv4Network(f"{host}/24", strict=False)
        found: list[tuple[DeviceAddress, str]] = []
        lock = threading.Lock()

        def probe(ip: ipaddress.IPv4Address) -> None:
            candidate = DeviceAddress(str(ip), self.address.port)
            try:
                platform = AScriptClient(candidate, password=self.password, timeout=probe_timeout, retries=0).ping()
            except DeviceConnectionError:
                return
            with lock:
                found.append((candidate, platform))

        targets = list(network.hosts())
        for offset in range(0, len(targets), workers):
            batch = [threading.Thread(target=probe, args=(ip,), daemon=True) for ip in targets[offset:offset + workers]]
            for thread in batch: thread.start()
            for thread in batch: thread.join()
        return sorted(found, key=lambda item: (int(ipaddress.IPv4Address(item[0].host)), item[0].port))

    def screenshot(self) -> bytes:
        return self.request("GET", "/api/screen/capture", timeout=30)

    def save_screenshot(self, destination: str | Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.screenshot())
        return destination.resolve()

    def capture_artifacts(self, destination: str | Path, *, prefix: str = "failure", mode: str = "smart") -> dict[str, Path]:
        """Save as much diagnostic evidence as a partially healthy device permits."""
        directory = Path(destination); directory.mkdir(parents=True, exist_ok=True)
        result: dict[str, Path] = {}
        errors: dict[str, str] = {}
        try: result["screenshot"] = self.save_screenshot(directory / f"{prefix}.png")
        except (AScriptError, OSError) as exc: errors["screenshot"] = str(exc)
        xml = directory / f"{prefix}.xml"
        try:
            xml.write_text(self.ui_xml(mode=mode), encoding="utf-8")
            result["xml"] = xml.resolve()
        except (AScriptError, OSError) as exc: errors["xml"] = str(exc)
        context = directory / f"{prefix}.json"
        payload: dict[str, Any] = {"errors": errors}
        for key, action in (("status", self.status), ("current_app", self.current_app)):
            try: payload[key] = action()
            except AScriptError as exc: errors[key] = str(exc)
        context.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        result["context"] = context.resolve()
        return result

    def ui_xml(self, *, mode: str = "smart", depth: int = 0, x: float = 0, y: float = 0) -> str:
        return self.request("GET", "/api/node/dump", params={"mode": mode, "depth": depth, "x": x, "y": y}, timeout=30).decode("utf-8")

    def ui_tree(self, *, mode: str = "smart", selector: Optional[Mapping[str, Any]] = None, x: float = 0, y: float = 0) -> dict[str, Any]:
        params: dict[str, Any] = {"mode": mode, "x": x, "y": y}
        if selector is not None:
            params["selector"] = json.dumps(selector, ensure_ascii=False)
        return self._ok(self.json("GET", "/api/tool/view/dump", params=params)).get("data", {})

    def find_elements(self, selector: Mapping[str, Any], *, mode: str = "smart", x: float = 0, y: float = 0) -> list[dict[str, Any]]:
        """Resolve an AScript selector and return its matching element metadata.

        ``selector`` follows the documented AScript view-tree contract, for
        example ``{"sel": [{"key": "label", "params": "OK"}], "find": 99999}``.
        """
        data = self.ui_tree(mode=mode, selector=selector, x=x, y=y)
        views = data.get("views") or []
        if not isinstance(views, list):
            raise DeviceResponseError("invalid element list returned by device", body=repr(views))
        return [dict(view) for view in views if isinstance(view, Mapping)]

    def current_app(self) -> dict[str, Any]:
        return self._ok(self.json("GET", "/api/node/package")).get("data", {})

    def eval_python(self, code: str, *, image: str = "") -> Any:
        if not code.strip():
            raise ValueError("code is empty")
        value = self._ok(self.json("POST", "/api/gp/eval", form={"code": code, "image": image}, timeout=60)).get("data")
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    def tap(self, x: float, y: float, *, duration_ms: int = 20) -> Any:
        return self.eval_python("from ascript.ios.action import click\nclick(%r, %r, %r)\n_result=True" % (x, y, duration_ms))

    def swipe(self, x1: float, y1: float, x2: float, y2: float, *, duration_ms: int = 200) -> Any:
        return self.eval_python("from ascript.ios.action import slide\nslide(%r, %r, %r, %r, %r)\n_result=True" % (x1, y1, x2, y2, duration_ms))

    def input_text(self, text: str, *, interval_ms: int = 120) -> Any:
        return self.eval_python("from ascript.ios.action import input\ninput(%s, %r)\n_result=True" % (json.dumps(text, ensure_ascii=False), interval_ms))

    def home(self) -> Any:
        return self.eval_python("from ascript.ios.action import home\nhome()\n_result=True")

    def _device_screenshot_path(self) -> str:
        items = self._ok(self.json("GET", "/api/screen/capture/list", params={"capture": "true"})).get("data") or []
        if not items:
            raise DeviceOperationError("device did not return a screenshot path")
        return str(items[0]["path"])

    def gp(self, class_id: str, params: str, *, image: Optional[str] = None, name: str = "asclient") -> Any:
        track = [{"id": class_id, "type": "图色工具", "data": {"params": params}}]
        return self._ok(self.json("POST", "/api/screen/gp", form={"strack": json.dumps(track, ensure_ascii=False), "image": image or self._device_screenshot_path(), "gp": name}, timeout=60)).get("data")

    def ocr(self, rect: Optional[str] = None) -> Any:
        return self.gp("ascript.ios.screen.Ocr", "mode=5, confidence=0.1" + (f", rect=[{rect}]" if rect else ""))

    def find_colors(self, colors: str, *, diff: float = 0.98) -> Any:
        return self.gp("ascript.ios.screen.FindColors", f"colors={colors!r}, diff={diff}")

    def compare_colors(self, colors: str, *, diff: float = 0.9) -> Any:
        return self.gp("ascript.ios.screen.CompareColors", f"colors={colors!r}, diff={diff}")

    @staticmethod
    def _name(name: str) -> str:
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise ValueError("project name must be one non-empty directory name")
        return name

    @staticmethod
    def _relative(path: str) -> str:
        path = str(PurePosixPath(path.replace("\\", "/")))
        if not path or path == "." or path.startswith("/") or ".." in PurePosixPath(path).parts:
            raise ValueError("remote path must be relative and cannot contain '..'")
        return path

    def projects(self) -> list[dict[str, Any]]:
        return self._ok(self.json("POST", "/api/module/list")).get("data", [])

    def create_project(self, name: str) -> None:
        self._ok(self.json("GET", "/api/module/create", params={"name": self._name(name)}))

    def rename_project(self, name: str, new_name: str) -> None:
        self._ok(self.json("GET", "/api/module/rname", params={"name": self._name(name), "rename": self._name(new_name)}))

    def remove_project(self, name: str) -> None:
        self._ok(self.json("GET", "/api/module/remove", params={"name": self._name(name)}))

    def project_files(self, name: str) -> Any:
        return self._ok(self.json("GET", "/api/module/files", params={"name": self._name(name)})).get("data", [])

    @staticmethod
    def _project_file_paths(tree: Any) -> list[str]:
        """Normalize Android-style and iOS-4001 project file trees."""
        paths: list[str] = []

        def walk(node: Any, parent: str | None) -> None:
            if isinstance(node, list):
                for item in node:
                    walk(item, parent)
                return
            if not isinstance(node, dict):
                return
            name = str(node.get("name") or node.get("fileName") or "")
            children = node.get("childs") or node.get("children") or node.get("files") or []
            if children:
                # The root returned by iOS has the project name. It is not part
                # of the remote path below ~/modules/<project>/.
                child_parent = "" if parent is None else "/".join(part for part in (parent, name) if part)
                walk(children, child_parent)
            elif name and (node.get("isFile") is True or (not node.get("dir") and not node.get("isDir") and parent is not None)):
                paths.append(AScriptClient._relative("/".join(part for part in (parent or "", name) if part)))

        walk(tree, None)
        return sorted(set(paths))

    def download_project(self, project: str, destination: str | Path) -> list[Path]:
        """Download all project files, preserving their relative directories."""
        project = self._name(project)
        destination = Path(destination).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        result: list[Path] = []
        for relative in self._project_file_paths(self.project_files(project)):
            target = destination.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.read_file(f"~/modules/{project}/{relative}"))
            result.append(target)
        return result

    def run_project(self, name: str) -> None:
        self._ok(self.json("GET", "/api/module/run", params={"name": self._name(name)}))

    def stop_project(self) -> None:
        self._ok(self.json("GET", "/api/module/stop"))

    def read_file(self, remote_path: str) -> bytes:
        return self.request("GET", "/api/file/get", params={"path": remote_path}, timeout=30)

    def save_text(self, remote_path: str, content: str) -> None:
        self._ok(self.json("POST", "/api/file/save", form={"path": remote_path, "content": content}))

    def create_remote(self, parent: str, name: str, *, directory: bool = False) -> None:
        self._ok(self.json("GET", "/api/file/create", params={"path": parent, "name": name, "type": "floder" if directory else "file"}))

    def remove_remote(self, path: str) -> None:
        self._ok(self.json("GET", "/api/file/remove", params={"path": path}))

    def upload_file(self, project: str, local_path: str | Path, remote_path: Optional[str] = None) -> None:
        project, local_path = self._name(project), Path(local_path)
        if not local_path.is_file():
            raise FileNotFoundError(local_path)
        relative = self._relative(remote_path or local_path.name)
        try:
            self.create_project(project)
        except DeviceOperationError:
            pass
        boundary = "----ASClient" + secrets.token_hex(16)
        filename = urllib.parse.quote(local_path.name, safe="")
        prefix = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
        body = prefix + local_path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        result = self.json("POST", "/api/file/upload", params={"path": f"~/modules/{project}/{relative}", "overwrite": "true"}, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=60)
        self._ok(result)

    def upload_tree(self, project: str, directory: str | Path) -> int:
        directory = Path(directory)
        if not directory.is_dir():
            raise NotADirectoryError(directory)
        files = sorted(item for item in directory.rglob("*") if item.is_file())
        for item in files:
            self.upload_file(project, item, item.relative_to(directory).as_posix())
        return len(files)

    def deploy(self, project: str, entry_file: str | Path, *, log_seconds: float = 5.0) -> tuple[list[LogEntry], bytes]:
        self.upload_file(project, entry_file, "__init__.py")
        logs: list[LogEntry] = []
        stop = threading.Event()
        thread = threading.Thread(target=lambda: logs.extend(self.logs(duration=log_seconds, stop_event=stop)), daemon=True)
        thread.start(); time.sleep(0.2); self.run_project(project); thread.join(log_seconds + 3); stop.set()
        return logs, self.screenshot()

    def logs(self, *, duration: Optional[float] = None, stop_event: Optional[threading.Event] = None, reconnects: int = 0, reconnect_delay: float = 1.0) -> Iterator[LogEntry]:
        """Yield device stdout/stderr events from port 10102.

        ``reconnects`` is the number of unexpected disconnects to retry. The
        duration is a total deadline across every connection attempt.
        """
        deadline = time.monotonic() + duration if duration is not None else None
        remaining = max(0, int(reconnects))
        while not stop_event or not stop_event.is_set():
            try:
                complete = yield from self._logs_once(deadline=deadline, stop_event=stop_event)
                if complete: return
                if remaining <= 0: return
                remaining -= 1
                if deadline is not None and time.monotonic() >= deadline: return
                time.sleep(max(0.0, reconnect_delay))
            except (DeviceConnectionError, ProtocolError, OSError):
                if remaining <= 0: raise
                remaining -= 1
                if deadline is not None and time.monotonic() >= deadline: return True
                time.sleep(max(0.0, reconnect_delay))

    def _logs_once(self, *, deadline: Optional[float], stop_event: Optional[threading.Event]) -> Iterator[LogEntry]:
        try:
            sock = socket.create_connection((self.address.host, 10102), timeout=self.timeout)
        except OSError as exc:
            raise DeviceConnectionError(f"cannot reach AScript log service at {self.address.host}:10102: {exc}") from exc
        try:
            key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            headers = ["GET /log/ HTTP/1.1", f"Host: {self.address.host}:10102", "Upgrade: websocket", "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
            if self.password: headers.append(f"Cookie: airscript={self.password}")
            sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
            if not self._read_until(sock, b"\r\n\r\n").startswith(b"HTTP/1.1 101"):
                raise ProtocolError("AScript log endpoint rejected WebSocket upgrade")
            sock.settimeout(0.5)
            while not stop_event or not stop_event.is_set():
                if deadline is not None and time.monotonic() >= deadline: return
                try: opcode, payload = self._read_frame(sock)
                except socket.timeout: continue
                if opcode == 0x8: return False
                if opcode == 0x9: self._send_frame(sock, 0xA, payload)
                elif opcode == 0x1:
                    try:
                        event = json.loads(payload.decode("utf-8")); yield LogEntry(str(event.get("msg", "")), str(event.get("type", "o")), str(event.get("time", "")))
                    except (UnicodeDecodeError, json.JSONDecodeError): yield LogEntry(payload.decode("utf-8", "replace"))
            return True
        finally:
            sock.close()

    def save_logs(self, destination: str | Path, *, duration: Optional[float] = None, reconnects: int = 0) -> int:
        """Write log events as UTF-8 JSON Lines and return the event count."""
        destination = Path(destination); destination.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with destination.open("w", encoding="utf-8") as stream:
            for entry in self.logs(duration=duration, reconnects=reconnects):
                stream.write(json.dumps({"message": entry.message, "kind": entry.kind, "timestamp": entry.timestamp}, ensure_ascii=False) + "\n")
                count += 1
        return count

    def wait_for_log(self, pattern: str, *, timeout: float = 10.0, regex: bool = False, reconnects: int = 1) -> LogEntry | None:
        """Wait for a log line containing ``pattern`` (or matching its regex)."""
        matcher = re.compile(pattern).search if regex else lambda value: pattern in value
        for entry in self.logs(duration=timeout, reconnects=reconnects):
            if matcher(entry.message): return entry
        return None

    @staticmethod
    def _read_until(sock: socket.socket, marker: bytes) -> bytes:
        data = b""
        while marker not in data:
            part = sock.recv(4096)
            if not part: raise ProtocolError("unexpected EOF during WebSocket handshake")
            data += part
            if len(data) > 65536: raise ProtocolError("oversized WebSocket handshake")
        return data

    @staticmethod
    def _recv_exact(sock: socket.socket, size: int) -> bytes:
        data = b""
        while len(data) < size:
            part = sock.recv(size - len(data))
            if not part: raise ProtocolError("unexpected EOF during WebSocket frame")
            data += part
        return data

    @classmethod
    def _read_frame(cls, sock: socket.socket) -> tuple[int, bytes]:
        first, second = cls._recv_exact(sock, 2); opcode, size, masked = first & 0x0F, second & 0x7F, bool(second & 0x80)
        if size == 126: size = struct.unpack(">H", cls._recv_exact(sock, 2))[0]
        elif size == 127: size = struct.unpack(">Q", cls._recv_exact(sock, 8))[0]
        if size > 8 * 1024 * 1024: raise ProtocolError("oversized WebSocket frame")
        mask, data = (cls._recv_exact(sock, 4) if masked else b""), cls._recv_exact(sock, size)
        return opcode, bytes(value ^ mask[i % 4] for i, value in enumerate(data)) if mask else data

    @staticmethod
    def _send_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
        mask, size = secrets.token_bytes(4), len(payload)
        if size < 126: header = bytes([0x80 | opcode, 0x80 | size])
        elif size < 65536: header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", size)
        else: header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", size)
        sock.sendall(header + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(payload)))
