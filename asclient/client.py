"""AScript local-device client. Public APIs are synchronous and dependency-free."""
from __future__ import annotations

import base64
import copy
import ipaddress
import json
import math
import re
import secrets
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Optional

from .errors import AScriptError, DeviceConnectionError, DeviceOperationError, DeviceResponseError, ProtocolError
from .i18n import t
from .runtime import device_lock


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
            raise ValueError(t("device_address_empty"))
        host, sep, port = value.rpartition(":")
        if not sep:
            return cls(value)
        if not host or not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ValueError(t("device_address_invalid"))
        return cls(host, int(port))

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class LogEntry:
    message: str
    kind: str = "o"
    timestamp: str = ""


@dataclass(frozen=True)
class ImageMatch:
    """A visual-template match in physical screen pixels."""
    x: int
    y: int
    width: int
    height: int
    confidence: float

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2


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

    def locked(self):
        """Return the process-local mutex for actions against this device."""
        return device_lock(self.address)

    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        result = dict(extra or {})
        if self.password:
            result["Cookie"] = f"airscript={self.password}"
        return result

    def request(self, method: str, path: str, *, params: Optional[Mapping[str, Any]] = None, form: Optional[Mapping[str, Any]] = None, data: Optional[bytes] = None, headers: Optional[Mapping[str, str]] = None, timeout: Optional[float] = None) -> bytes:
        if not path.startswith("/"):
            raise ValueError(t("path_must_start"))
        if form is not None and data is not None:
            raise ValueError(t("form_data_exclusive"))
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
        raise DeviceConnectionError(t("cannot_reach_device", address=self.address, detail=last_error)) from last_error

    def json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        raw = self.request(method, path, **kwargs)
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeviceResponseError(t("invalid_json", path=path), body=raw[:1000].decode("utf-8", "replace")) from exc
        if not isinstance(value, dict):
            raise DeviceResponseError(t("expected_object", path=path), body=repr(value))
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
            result = self._ok(self.json("GET", "/api/status")).get("data", {})
            if not isinstance(result, dict):
                raise DeviceResponseError("invalid status returned by device", body=repr(result))
            self._attach_status_screen(result)
            return result
        except DeviceOperationError as exc:
            issue = "ios_objc_property_callable" if "ObjCStrInstance" in str(exc) and "callable" in str(exc) else "device_status_error"
            result: dict[str, Any] = {
                "available": True,
                "health": "degraded",
                "status_api_error": str(exc),
                "compatibility": {
                    "status_api": {"state": "degraded", "issue": issue, "message": t("status_fallback_summary")},
                    "capabilities": {},
                },
                "platform": self.ping(),
            }
            self._attach_status_screen(result, result["compatibility"]["capabilities"])
            try:
                result["current_app"] = self.current_app()
                result["compatibility"]["capabilities"]["current_app"] = "available"
            except (DeviceOperationError, DeviceResponseError, DeviceConnectionError):
                result["compatibility"]["capabilities"]["current_app"] = "unavailable"
            return result

    def _attach_status_screen(self, result: dict[str, Any], capabilities: dict[str, Any] | None = None) -> None:
        try:
            result["logical_screen"] = self._logical_screen()
        except (DeviceOperationError, DeviceResponseError, DeviceConnectionError):
            pass
        try:
            result["screen"] = self.screen_size()
            if capabilities is not None: capabilities["screen"] = "available"
        except (DeviceOperationError, DeviceResponseError, DeviceConnectionError):
            if capabilities is not None: capabilities["screen"] = "unavailable"

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

    @staticmethod
    def _png_size(image: bytes) -> tuple[float, float] | None:
        """Return PNG dimensions without adding an image-library dependency."""
        if len(image) < 24 or not image.startswith(b"\x89PNG\r\n\x1a\n"):
            return None
        width = int.from_bytes(image[16:20], "big")
        height = int.from_bytes(image[20:24], "big")
        if width <= 0 or height <= 0:
            return None
        return float(width), float(height)

    def action_size(self) -> dict[str, float]:
        """Return the physical-pixel coordinate space used by ``tap`` and ``swipe``.

        AScript's iOS action module uses screenshot pixels, while
        :meth:`screen_size` reports iOS logical points.  The two must not be
        mixed on Retina devices.
        """
        size = self._png_size(self.screenshot())
        if size is None:
            raise DeviceResponseError("invalid PNG returned by screenshot endpoint")
        return {"width": size[0], "height": size[1]}

    def screen_size(self) -> dict[str, float]:
        """Return the current physical screen resolution used for actions.

        This is the same coordinate system as screenshots, OCR, ``tap`` and
        ``swipe``.
        """
        return self.action_size()

    def save_screenshot(self, destination: str | Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.screenshot())
        return destination.resolve()

    @staticmethod
    def crop_png_relative(image: bytes, left: float, top: float, right: float, bottom: float) -> bytes:
        """Crop a standard screenshot PNG using a 0..1 relative rectangle."""
        try:
            left, top, right, bottom = (float(value) for value in (left, top, right, bottom))
        except (TypeError, ValueError) as exc:
            raise ValueError("crop ratios must be finite numbers between 0 and 1") from exc
        if not all(math.isfinite(value) and 0 <= value <= 1 for value in (left, top, right, bottom)) or left >= right or top >= bottom:
            raise ValueError("crop ratios must satisfy 0 <= left < right <= 1 and 0 <= top < bottom <= 1")
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise DeviceResponseError("screenshot is not a PNG image")
        offset, ihdr, compressed = 8, None, bytearray()
        while offset + 12 <= len(image):
            length = int.from_bytes(image[offset:offset + 4], "big")
            kind, data = image[offset + 4:offset + 8], image[offset + 8:offset + 8 + length]
            if len(data) != length: raise DeviceResponseError("truncated PNG image")
            if kind == b"IHDR": ihdr = data
            elif kind == b"IDAT": compressed.extend(data)
            elif kind == b"IEND": break
            offset += length + 12
        if ihdr is None or len(ihdr) != 13:
            raise DeviceResponseError("PNG image has no valid IHDR chunk")
        width, height, depth, color_type, compression, filter_method, interlace = struct.unpack(">IIBBBBB", ihdr)
        channels = {2: 3, 6: 4}.get(color_type)
        if depth != 8 or channels is None or compression != 0 or filter_method != 0 or interlace != 0:
            raise DeviceResponseError("only non-interlaced 8-bit RGB/RGBA PNG screenshots can be cropped")
        stride, bpp = width * channels, channels
        raw = zlib.decompress(compressed)
        if len(raw) != height * (stride + 1): raise DeviceResponseError("PNG image data has an invalid length")
        rows: list[bytearray] = []
        previous = bytearray(stride)
        for row_index in range(height):
            start = row_index * (stride + 1); filter_type = raw[start]; row = bytearray(raw[start + 1:start + 1 + stride])
            for index in range(stride):
                a = row[index - bpp] if index >= bpp else 0; b = previous[index]; c = previous[index - bpp] if index >= bpp else 0
                if filter_type == 1: row[index] = (row[index] + a) & 255
                elif filter_type == 2: row[index] = (row[index] + b) & 255
                elif filter_type == 3: row[index] = (row[index] + ((a + b) // 2)) & 255
                elif filter_type == 4:
                    p = a + b - c; pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    row[index] = (row[index] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
                elif filter_type != 0: raise DeviceResponseError("PNG image uses an unsupported filter")
            rows.append(row); previous = row
        x0, x1 = int(width * left), max(int(width * right), int(width * left) + 1)
        y0, y1 = int(height * top), max(int(height * bottom), int(height * top) + 1)
        x1, y1 = min(x1, width), min(y1, height)
        cropped_width, cropped_height = x1 - x0, y1 - y0
        cropped = b"".join(b"\0" + bytes(row[x0 * channels:x1 * channels]) for row in rows[y0:y1])
        header = struct.pack(">IIBBBBB", cropped_width, cropped_height, depth, color_type, compression, filter_method, interlace)
        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(cropped)) + chunk(b"IEND", b"")

    def screenshot_crop_relative(self, left: float, top: float, right: float, bottom: float) -> bytes:
        """Capture and crop a screenshot with a relative ``left, top, right, bottom`` rectangle."""
        return self.crop_png_relative(self.screenshot(), left, top, right, bottom)

    def save_screenshot_crop_relative(self, destination: str | Path, left: float, top: float, right: float, bottom: float) -> Path:
        """Capture a relative crop and save it as PNG."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.screenshot_crop_relative(left, top, right, bottom))
        return destination.resolve()

    @staticmethod
    def _image_match(image: bytes, template: str | Path | bytes, *, confidence: float, region: tuple[float, float, float, float] | None) -> ImageMatch | None:
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("image matching requires Pillow; reinstall asclient to install its dependencies") from exc
        if not math.isfinite(confidence) or not 0 < confidence <= 1:
            raise ValueError("confidence must be a finite number in (0, 1]")
        from io import BytesIO
        source = Image.open(BytesIO(image)).convert("RGB")
        template_image = Image.open(BytesIO(template) if isinstance(template, bytes) else str(template)).convert("RGB")
        screen_width, screen_height = source.size; template_width, template_height = template_image.size
        if template_width > screen_width or template_height > screen_height: return None
        if region is None: left, top, right, bottom = 0, 0, 1, 1
        else: left, top, right, bottom = (float(value) for value in region)
        if not all(math.isfinite(value) and 0 <= value <= 1 for value in (left, top, right, bottom)) or left >= right or top >= bottom:
            raise ValueError("region must satisfy 0 <= left < right <= 1 and 0 <= top < bottom <= 1")
        x0, y0, x1, y1 = int(screen_width * left), int(screen_height * top), int(screen_width * right), int(screen_height * bottom)
        x1, y1 = min(x1, screen_width), min(y1, screen_height)
        if x1 - x0 < template_width or y1 - y0 < template_height: return None
        source_pixels, template_pixels = source.load(), template_image.load()
        sample_x = sorted({round(index * (template_width - 1) / 7) for index in range(8)})
        sample_y = sorted({round(index * (template_height - 1) / 7) for index in range(8)})
        sample_count = len(sample_x) * len(sample_y) * 3; allowed = (1 - confidence) * 255 * sample_count
        best: ImageMatch | None = None
        for y in range(y0, y1 - template_height + 1):
            for x in range(x0, x1 - template_width + 1):
                error = 0
                for ty in sample_y:
                    for tx in sample_x:
                        source_pixel, template_pixel = source_pixels[x + tx, y + ty], template_pixels[tx, ty]
                        error += abs(source_pixel[0] - template_pixel[0]) + abs(source_pixel[1] - template_pixel[1]) + abs(source_pixel[2] - template_pixel[2])
                        if error > allowed: break
                    if error > allowed: break
                score = 1 - error / (255 * sample_count)
                if error <= allowed and (best is None or score > best.confidence): best = ImageMatch(x, y, template_width, template_height, score)
        return best

    def find_image(self, template: str | Path | bytes, *, confidence: float = 0.9, region: tuple[float, float, float, float] | None = None) -> ImageMatch | None:
        """Find a local image template in the current screenshot."""
        return self._image_match(self.screenshot(), template, confidence=confidence, region=region)

    def wait_image(self, template: str | Path | bytes, *, confidence: float = 0.9, timeout: float = 10.0, interval: float = 0.5, region: tuple[float, float, float, float] | None = None, log: bool = False, initial_delay: bool = True) -> ImageMatch:
        """Wait until a local image template appears, then return its match."""
        if timeout < 0 or interval <= 0: raise ValueError("timeout must be non-negative and interval must be positive")
        deadline = time.monotonic() + timeout
        if initial_delay: time.sleep(min(interval, timeout))
        attempt = 0
        while True:
            attempt += 1
            match = self.find_image(template, confidence=confidence, region=region)
            if match is not None:
                if log: print(t("image_wait_found", attempt=attempt, x=match.x, y=match.y, confidence=match.confidence))
                return match
            if log: print(t("image_wait_missing", attempt=attempt))
            if time.monotonic() >= deadline: raise TimeoutError("image did not appear before timeout")
            time.sleep(min(interval, deadline - time.monotonic()))

    def wait_image_gone(self, template: str | Path | bytes, *, confidence: float = 0.9, timeout: float = 10.0, interval: float = 0.5, region: tuple[float, float, float, float] | None = None, log: bool = False, initial_delay: bool = True) -> bool:
        """Wait until a local image template is no longer present."""
        if timeout < 0 or interval <= 0: raise ValueError("timeout must be non-negative and interval must be positive")
        deadline = time.monotonic() + timeout
        if initial_delay: time.sleep(min(interval, timeout))
        attempt = 0
        while True:
            attempt += 1
            match = self.find_image(template, confidence=confidence, region=region)
            if match is None:
                if log: print(t("image_wait_gone", attempt=attempt))
                return True
            if log: print(t("image_wait_present", attempt=attempt, confidence=match.confidence))
            if time.monotonic() >= deadline: return False
            time.sleep(min(interval, deadline - time.monotonic()))

    def tap_image(self, template: str | Path | bytes, *, confidence: float = 0.9, timeout: float = 10.0, interval: float = 0.5, region: tuple[float, float, float, float] | None = None, duration_ms: int = 20) -> ImageMatch:
        """Wait for a template and tap its center."""
        match = self.wait_image(template, confidence=confidence, timeout=timeout, interval=interval, region=region)
        self.tap(*match.center, duration_ms=duration_ms)
        return match

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
        logical, action = self._coordinate_spaces()
        raw = self.request("GET", "/api/node/dump", params={"mode": mode, "depth": depth, "x": x * logical["width"] / action["width"], "y": y * logical["height"] / action["height"]}, timeout=30).decode("utf-8")
        return self._scale_xml_coordinates(raw, action["width"] / logical["width"], action["height"] / logical["height"])

    def ui_tree(self, *, mode: str = "smart", selector: Optional[Mapping[str, Any]] = None, x: float = 0, y: float = 0) -> dict[str, Any]:
        logical, action = self._coordinate_spaces()
        params: dict[str, Any] = {"mode": mode, "x": x * logical["width"] / action["width"], "y": y * logical["height"] / action["height"]}
        if selector is not None:
            params["selector"] = json.dumps(selector, ensure_ascii=False)
        data = self._ok(self.json("GET", "/api/tool/view/dump", params=params)).get("data", {})
        if not isinstance(data, Mapping):
            raise DeviceResponseError("invalid UI tree returned by device", body=repr(data))
        return self._scale_tree_coordinates(dict(data), action["width"] / logical["width"], action["height"] / logical["height"], action)

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
        with self.locked(): value = self._ok(self.json("POST", "/api/gp/eval", form={"code": code, "image": image}, timeout=60)).get("data")
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    def tap(self, x: float, y: float, *, duration_ms: int = 20) -> Any:
        with self.locked(): return self.eval_python("from ascript.ios.action import click\nclick(%r, %r, %r)\n_result=True" % (x, y, duration_ms))

    def _logical_screen(self) -> dict[str, float]:
        value = self._ok(self.json("GET", "/api/screen/size")).get("data", {})
        try:
            width, height = float(value["width"]), float(value["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DeviceResponseError(t("screen_size_invalid"), body=repr(value)) from exc
        if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
            raise DeviceResponseError(t("screen_size_invalid"), body=repr(value))
        return {"width": width, "height": height}

    def _coordinate_spaces(self) -> tuple[dict[str, float], dict[str, float]]:
        logical = self._logical_screen()
        try:
            action = self.screen_size()
        except DeviceResponseError:
            # A malformed screenshot must not make tree inspection unusable.
            action = logical
        return logical, action

    @staticmethod
    def _scale_tree_coordinates(tree: dict[str, Any], x_scale: float, y_scale: float, action: Mapping[str, float]) -> dict[str, Any]:
        x_keys, y_keys = {"x", "left", "right", "center_x"}, {"y", "top", "bottom", "center_y"}
        width_keys, height_keys = {"width", "widthPixels", "noncompatWidthPixels"}, {"height", "heightPixels", "noncompatHeightPixels"}
        value = copy.deepcopy(tree)
        def visit(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if isinstance(child, (int, float)) and not isinstance(child, bool):
                        if key in x_keys or key in width_keys: item[key] = child * x_scale
                        elif key in y_keys or key in height_keys: item[key] = child * y_scale
                    else: visit(child)
            elif isinstance(item, list):
                for child in item: visit(child)
        visit(value)
        display = value.setdefault("config", {}).setdefault("display", {})
        display["widthPixels"], display["heightPixels"] = action["width"], action["height"]
        return value

    @staticmethod
    def _scale_xml_coordinates(xml: str, x_scale: float, y_scale: float) -> str:
        import xml.etree.ElementTree as ET
        if x_scale == 1 and y_scale == 1:
            return xml
        try: root = ET.fromstring(xml)
        except ET.ParseError: return xml
        for element in root.iter():
            for key, scale in (("x", x_scale), ("width", x_scale), ("y", y_scale), ("height", y_scale)):
                if key in element.attrib:
                    try: element.attrib[key] = str(int(round(float(element.attrib[key]) * scale)))
                    except ValueError: pass
        return ET.tostring(root, encoding="unicode")

    def relative_point(self, x_ratio: float, y_ratio: float) -> tuple[float, float]:
        """Convert two 0..1 ratios into physical action coordinates.

        The result is directly suitable for :meth:`tap` and :meth:`swipe`.
        """
        try:
            x_ratio, y_ratio = float(x_ratio), float(y_ratio)
        except (TypeError, ValueError) as exc:
            raise ValueError(t("relative_ratio_invalid")) from exc
        if not all(math.isfinite(value) and 0 <= value <= 1 for value in (x_ratio, y_ratio)):
            raise ValueError(t("relative_ratio_invalid"))
        size = self.action_size()
        # Keep 1.0 within the valid final coordinate while retaining fractional points elsewhere.
        return min(size["width"] - 1, size["width"] * x_ratio), min(size["height"] - 1, size["height"] * y_ratio)

    def tap_relative(self, x_ratio: float, y_ratio: float, *, duration_ms: int = 20) -> Any:
        """Tap a point expressed as fractions of the current screen dimensions."""
        return self.tap(*self.relative_point(x_ratio, y_ratio), duration_ms=duration_ms)

    def swipe(self, x1: float, y1: float, x2: float, y2: float, *, duration_ms: int = 200) -> Any:
        with self.locked(): return self.eval_python("from ascript.ios.action import slide\nslide(%r, %r, %r, %r, %r)\n_result=True" % (x1, y1, x2, y2, duration_ms))

    def swipe_relative(self, x1_ratio: float, y1_ratio: float, x2_ratio: float, y2_ratio: float, *, duration_ms: int = 200) -> Any:
        """Swipe between two points expressed as fractions of current screen dimensions."""
        return self.swipe(*self.relative_point(x1_ratio, y1_ratio), *self.relative_point(x2_ratio, y2_ratio), duration_ms=duration_ms)

    def scroll_until_image(self, template: str | Path | bytes, *, direction: str = "down", confidence: float = 0.9, timeout: float = 20.0, interval: float = 0.5, max_swipes: int = 10, region: tuple[float, float, float, float] | None = None, duration_ms: int = 300, log: bool = False, initial_delay: bool = True) -> ImageMatch:
        """Swipe in ``direction`` until a template appears, then return its match."""
        if timeout < 0 or interval <= 0 or max_swipes < 0:
            raise ValueError("timeout must be non-negative, interval positive, and max_swipes non-negative")
        directions = {
            "down": (0.5, 0.2, 0.5, 0.8), "up": (0.5, 0.8, 0.5, 0.2),
            "left": (0.8, 0.5, 0.2, 0.5), "right": (0.2, 0.5, 0.8, 0.5),
            "下": (0.5, 0.2, 0.5, 0.8), "上": (0.5, 0.8, 0.5, 0.2),
            "左": (0.8, 0.5, 0.2, 0.5), "右": (0.2, 0.5, 0.8, 0.5),
        }
        try: x1, y1, x2, y2 = directions[direction.lower()]
        except (AttributeError, KeyError) as exc: raise ValueError("direction must be one of: down, up, left, right, 下, 上, 左, 右") from exc
        deadline = time.monotonic() + timeout
        if initial_delay: time.sleep(min(interval, timeout))
        for swipe_number in range(max_swipes + 1):
            match = self.find_image(template, confidence=confidence, region=region)
            attempt = swipe_number + 1
            if match is not None:
                if log: print(t("image_scroll_match", attempt=attempt, x=match.x, y=match.y, confidence=match.confidence))
                return match
            if swipe_number == max_swipes or time.monotonic() >= deadline:
                if log: print(t("image_scroll_stop", attempt=attempt))
                break
            if log: print(t("image_scroll_next", attempt=attempt))
            self.swipe_relative(x1, y1, x2, y2, duration_ms=duration_ms)
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
        raise TimeoutError(f"image did not appear after {max_swipes} {direction} swipes or before timeout")

    def input_text(self, text: str, *, interval_ms: int = 120) -> Any:
        with self.locked(): return self.eval_python("from ascript.ios.action import input\ninput(%s, %r)\n_result=True" % (json.dumps(text, ensure_ascii=False), interval_ms))

    def home(self) -> Any:
        with self.locked(): return self.eval_python("from ascript.ios.action import home\nhome()\n_result=True")

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
        with self.locked(): self._ok(self.json("GET", "/api/module/create", params={"name": self._name(name)}))

    def rename_project(self, name: str, new_name: str) -> None:
        with self.locked(): self._ok(self.json("GET", "/api/module/rname", params={"name": self._name(name), "rename": self._name(new_name)}))

    def remove_project(self, name: str) -> None:
        with self.locked(): self._ok(self.json("GET", "/api/module/remove", params={"name": self._name(name)}))

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
        with self.locked(): self._ok(self.json("GET", "/api/module/run", params={"name": self._name(name)}))

    def stop_project(self) -> None:
        with self.locked(): self._ok(self.json("GET", "/api/module/stop"))

    def read_file(self, remote_path: str) -> bytes:
        return self.request("GET", "/api/file/get", params={"path": remote_path}, timeout=30)

    def save_text(self, remote_path: str, content: str) -> None:
        with self.locked(): self._ok(self.json("POST", "/api/file/save", form={"path": remote_path, "content": content}))

    def create_remote(self, parent: str, name: str, *, directory: bool = False) -> None:
        with self.locked(): self._ok(self.json("GET", "/api/file/create", params={"path": parent, "name": name, "type": "floder" if directory else "file"}))

    def remove_remote(self, path: str) -> None:
        with self.locked(): self._ok(self.json("GET", "/api/file/remove", params={"path": path}))

    def upload_file(self, project: str, local_path: str | Path, remote_path: Optional[str] = None) -> None:
        project, local_path = self._name(project), Path(local_path)
        if not local_path.is_file():
            raise FileNotFoundError(local_path)
        relative = self._relative(remote_path or local_path.name)
        with self.locked():
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
        with self.locked():
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
            raise DeviceConnectionError(t("cannot_reach_logs", host=self.address.host, detail=exc)) from exc
        try:
            key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            headers = ["GET /log/ HTTP/1.1", f"Host: {self.address.host}:10102", "Upgrade: websocket", "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
            if self.password: headers.append(f"Cookie: airscript={self.password}")
            sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
            if not self._read_until(sock, b"\r\n\r\n").startswith(b"HTTP/1.1 101"):
                raise ProtocolError(t("websocket_rejected"))
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
