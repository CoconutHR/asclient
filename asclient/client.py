"""AScript local-device client. Public APIs are synchronous and dependency-free."""
from __future__ import annotations

import base64
import binascii
import copy
import json
import math
import re
import secrets
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Mapping, Optional

from .errors import AScriptError, DeviceConnectionError, DeviceOperationError, DeviceResponseError, ProtocolError
from .i18n import t
from .runtime import device_lock
from .vision import PixelColor, ScreenFrame, relative_point as _relative_frame_point
from ._websocket import WebSocket


LOG_PORT = 10102
"""Device-side log WebSocket port; the device service does not make it configurable."""

_MAX_CROPPABLE_PNG_BYTES = 64 * 1024 * 1024
"""Upper bound for a decoded PNG frame handled by the standard-library cropper."""


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


@dataclass(frozen=True)
class OcrItem:
    text: str
    rect: tuple[int, int, int, int] | None
    confidence: float | None
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class OcrResult:
    items: tuple[OcrItem, ...]
    raw: Any


_SWIPE_DIRECTIONS = {
    "down": (0.5, 0.2, 0.5, 0.8), "up": (0.5, 0.8, 0.5, 0.2),
    "left": (0.8, 0.5, 0.2, 0.5), "right": (0.2, 0.5, 0.8, 0.5),
    "下": (0.5, 0.2, 0.5, 0.8), "上": (0.5, 0.8, 0.5, 0.2),
    "左": (0.8, 0.5, 0.2, 0.5), "右": (0.2, 0.5, 0.8, 0.5),
}
_DEFAULT_LOCK_TIMEOUT = object()


def swipe_gesture(direction: str = "down", swipe_relative: tuple[float, float, float, float] | None = None, x1_ratio: float | None = None, y1_ratio: float | None = None, x2_ratio: float | None = None, y2_ratio: float | None = None) -> tuple[float, float, float, float]:
    """Validate direction/custom-swipe input and return screen ratio endpoints.

    ``direction`` 是手势移动方向，支持 down/up/left/right 及中文；提供
    ``swipe_relative`` 元组时覆盖方向轨迹，也兼容四个独立比例参数。
    """
    custom_swipe = (x1_ratio, y1_ratio, x2_ratio, y2_ratio)
    if any(value is not None for value in custom_swipe) and not all(value is not None for value in custom_swipe):
        raise ValueError("x1_ratio, y1_ratio, x2_ratio, and y2_ratio must be supplied together")
    if swipe_relative is not None:
        if any(value is not None for value in custom_swipe):
            raise ValueError("swipe_relative cannot be combined with individual ratio parameters")
        try:
            if len(swipe_relative) != 4: raise ValueError
            custom_swipe = tuple(float(value) for value in swipe_relative)
        except (TypeError, ValueError) as exc:
            raise ValueError("swipe_relative must contain four ratios: x1, y1, x2, y2") from exc
    if all(value is not None for value in custom_swipe):
        return custom_swipe
    try:
        return _SWIPE_DIRECTIONS[direction.lower()]
    except (AttributeError, KeyError) as exc:
        raise ValueError("direction must be one of: down, up, left, right, 下, 上, 左, 右") from exc


class AScriptClient:
    """AScript 单台设备服务客户端。

    参数 ``address`` 为 ``HOST[:PORT]``；``password`` 为可选服务密码；
    ``timeout`` 单位为秒。所有公开坐标均使用截图物理像素。
    """

    def __init__(self, address: str | DeviceAddress, *, password: str = "", timeout: float = 15.0, retries: int = 1, coordinate_cache_ttl: float = 1.0, lock_id: str | None = None, lock_timeout: float | None = None):
        self.address = DeviceAddress.parse(address)
        self.lock_id = lock_id
        self.password, self.timeout, self.retries = password, float(timeout), max(0, int(retries))
        if lock_timeout is not None:
            try:
                lock_timeout = float(lock_timeout)
            except (TypeError, ValueError) as exc:
                raise ValueError("lock_timeout must be a finite non-negative number of seconds") from exc
            if not math.isfinite(lock_timeout) or lock_timeout < 0 or lock_timeout > threading.TIMEOUT_MAX:
                raise ValueError("lock_timeout must be a finite non-negative number of seconds")
        self.lock_timeout = lock_timeout
        if not math.isfinite(coordinate_cache_ttl) or coordinate_cache_ttl < 0: raise ValueError("coordinate_cache_ttl must be a finite non-negative number of seconds")
        self.coordinate_cache_ttl = float(coordinate_cache_ttl)
        self._space_cache: tuple[float, dict[str, float], dict[str, float]] | None = None
        self._hid_vision_available: bool | None = None
        self._vision_action_dimensions: dict[str, float] | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.address}"

    def locked(self, *, timeout: float | None | object = _DEFAULT_LOCK_TIMEOUT):
        """Return the reentrant cross-process mutex for actions against this device."""
        return device_lock(self.address, lock_id=self.lock_id, timeout=self.lock_timeout if timeout is _DEFAULT_LOCK_TIMEOUT else timeout)

    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        result = dict(extra or {})
        if self.password:
            result["Cookie"] = f"airscript={self.password}"
        return result

    def request(self, method: str, path: str, *, params: Optional[Mapping[str, Any]] = None, form: Optional[Mapping[str, Any]] = None, data: Optional[bytes] = None, headers: Optional[Mapping[str, str]] = None, timeout: Optional[float] = None) -> bytes:
        """调用已确认的原始 HTTP 接口并返回字节。

        ``path`` 必须以 ``/`` 开头；``timeout`` 单位秒。仅在高层 API
        尚未覆盖的稳定设备端接口中使用。
        """
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
        """读取设备状态并在 iOS 4001 状态接口异常时降级。

        ``screen`` 为实际物理分辨率；``logical_screen`` 仅用于协议诊断。
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
            self._backfill_status_fields(result)
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

    def _backfill_status_fields(self, result: dict[str, Any]) -> None:
        """iOS 4001 服务端 ``/api/status`` 崩溃时，用一次 ``eval`` 回填等价的只读字段。

        设备端 ``api_status`` 把 ``languageCode`` 属性当方法调用导致整个接口报错
        （根因记录见 docs/生产使用指南.md 的兼容性章节）。这里只回填不改变设备
        状态的 ``device``/``system``/``app``/``python`` 字段；battery 等需要打开
        设备监控开关的字段保持缺失。补偿失败时静默跳过，不影响降级返回。
        """
        code = (
            "import json\n"
            "def _g(v):\n"
            "    try: return v() if callable(v) else v\n"
            "    except Exception: return v\n"
            "_o = {}\n"
            "try:\n"
            "    from rubicon.objc import ObjCClass\n"
            "    _d = _g(ObjCClass('UIDevice').currentDevice)\n"
            "    _o['device'] = {'brand': 'Apple', 'manufacturer': 'Apple', 'abi': 'arm64',"
            " 'model': str(_g(_d.model) or ''), 'full_name': str(_g(_d.name) or ''),"
            " 'product': str(_g(_d.systemName) or '')}\n"
            "    _o['system'] = {'os_name': 'iOS', 'ios_version': str(_g(_d.systemVersion) or ''),"
            " 'language': str(_g(_g(ObjCClass('NSLocale').currentLocale).languageCode) or ''),"
            " 'timezone': str(_g(_g(ObjCClass('NSTimeZone').localTimeZone).name) or '')}\n"
            "    _b = _g(ObjCClass('NSBundle').mainBundle)\n"
            "    _o['app'] = {'version': str(_b.objectForInfoDictionaryKey_('CFBundleShortVersionString') or ''),"
            " 'build': str(_b.objectForInfoDictionaryKey_('CFBundleVersion') or ''),"
            " 'package': str(_g(_b.bundleIdentifier) or '')}\n"
            "    import platform as _p\n"
            "    _o['python'] = {'version': _p.python_version()}\n"
            "except Exception:\n"
            "    pass\n"
            "_result = json.dumps(_o)\n"
        )
        try:
            value = self.eval_python(code)
        except (DeviceOperationError, DeviceResponseError, DeviceConnectionError):
            return
        if not isinstance(value, dict) or not value: return
        fields = sorted(str(key) for key in value)
        result.update(value)
        result["compatibility"]["status_api"]["compensated_fields"] = fields
        result["compatibility"]["status_api"]["message"] = (
            t("status_fallback_summary") + " " + t("status_compensated_fields", fields=", ".join(fields))
        )

    def packages(self) -> list[Any]:
        """返回设备端 Python 包列表，元素通常为 ``[名称, 版本]``。"""
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

    def screenshot(self) -> bytes:
        """获取当前屏幕 PNG 字节，坐标尺寸与 ``tap`` 一致。"""
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
        """返回 ``tap``、``swipe``、OCR 使用的截图物理像素尺寸。"""
        size = self._png_size(self.screenshot())
        if size is None:
            raise DeviceResponseError("invalid PNG returned by screenshot endpoint")
        return {"width": size[0], "height": size[1]}

    def screen_size(self) -> dict[str, float]:
        """返回当前真实物理屏幕分辨率。

        与截图、OCR、``tap``、``swipe`` 使用同一物理像素坐标系；
        设备端 ``/api/screen/size`` 的逻辑点尺寸由内部 ``_logical_screen`` 使用。
        """
        return self.action_size()

    def capture_frame(self) -> ScreenFrame:
        """抓取一张物理像素截图，供取色和多个视觉查询共享。"""
        return ScreenFrame(self.screenshot())

    def pixel(self, x: int, y: int) -> PixelColor:
        """读取物理像素坐标 ``x/y`` 的 RGBA 颜色。"""
        return self.capture_frame().pixel(x, y)

    def pixel_relative(self, x_ratio: float, y_ratio: float) -> PixelColor:
        """按屏幕宽高比例读取一个像素颜色。"""
        return self.capture_frame().pixel_relative(x_ratio, y_ratio)

    def pixels(self, points: Iterator[tuple[int, int]] | list[tuple[int, int]] | tuple[tuple[int, int], ...]) -> list[PixelColor]:
        """在同一张截图中读取多个物理像素坐标。"""
        return self.capture_frame().pixels(points)

    def pixels_relative(self, points: Iterator[tuple[float, float]] | list[tuple[float, float]] | tuple[tuple[float, float], ...]) -> list[PixelColor]:
        """在同一张截图中读取多个比例坐标。"""
        return self.capture_frame().pixels_relative(points)

    def save_screenshot(self, destination: str | Path) -> Path:
        """保存完整 PNG 截图到 ``destination``，返回绝对路径。"""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.screenshot())
        return destination.resolve()

    @staticmethod
    def crop_png(image: bytes, left: int, top: int, right: int, bottom: int) -> bytes:
        """Crop a standard screenshot PNG using a physical-pixel rectangle.

        The rectangle follows the public coordinate contract: left/top are
        inclusive, right/bottom are exclusive. This standard-library helper is
        also used by Inspector so a frozen source PNG can be cropped without
        requiring Pillow or round-tripping through a browser canvas.
        """
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise DeviceResponseError("screenshot is not a PNG image")
        size = AScriptClient._png_size(image)
        if size is None:
            raise DeviceResponseError("PNG image has no valid dimensions")
        width, height = (int(size[0]), int(size[1]))
        values = (left, top, right, bottom)
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in values) or not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise ValueError("crop pixels must satisfy screen bounds and left < right, top < bottom")
        offset, ihdr, compressed = 8, None, bytearray()
        while offset + 12 <= len(image):
            length = int.from_bytes(image[offset:offset + 4], "big")
            kind, data = image[offset + 4:offset + 8], image[offset + 8:offset + 8 + length]
            if len(data) != length:
                raise DeviceResponseError("truncated PNG image")
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
        expected_raw_length = height * (stride + 1)
        if expected_raw_length > _MAX_CROPPABLE_PNG_BYTES:
            raise DeviceResponseError("PNG image exceeds the maximum supported crop size")
        try:
            decompressor = zlib.decompressobj()
            raw = decompressor.decompress(bytes(compressed), expected_raw_length + 1)
            if len(raw) > expected_raw_length or decompressor.unconsumed_tail:
                raise DeviceResponseError("PNG image data exceeds the expected size")
            raw += decompressor.flush()
        except zlib.error as exc:
            raise DeviceResponseError("PNG image data cannot be decompressed") from exc
        if len(raw) != expected_raw_length or not decompressor.eof:
            raise DeviceResponseError("PNG image data has an invalid length")
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
        cropped_width, cropped_height = right - left, bottom - top
        cropped = b"".join(b"\0" + bytes(row[left * channels:right * channels]) for row in rows[top:bottom])
        header = struct.pack(">IIBBBBB", cropped_width, cropped_height, depth, color_type, compression, filter_method, interlace)
        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(cropped)) + chunk(b"IEND", b"")

    @staticmethod
    def crop_png_relative(image: bytes, left: float, top: float, right: float, bottom: float) -> bytes:
        """Crop a standard screenshot PNG using a 0..1 relative rectangle."""
        try:
            left, top, right, bottom = (float(value) for value in (left, top, right, bottom))
        except (TypeError, ValueError) as exc:
            raise ValueError("crop ratios must be finite numbers between 0 and 1") from exc
        if not all(math.isfinite(value) and 0 <= value <= 1 for value in (left, top, right, bottom)) or left >= right or top >= bottom:
            raise ValueError("crop ratios must satisfy 0 <= left < right <= 1 and 0 <= top < bottom <= 1")
        size = AScriptClient._png_size(image)
        if size is None:
            raise DeviceResponseError("screenshot is not a PNG image")
        width, height = (int(size[0]), int(size[1]))
        x0, x1 = int(width * left), min(width, max(int(width * right), int(width * left) + 1))
        y0, y1 = int(height * top), min(height, max(int(height * bottom), int(height * top) + 1))
        return AScriptClient.crop_png(image, x0, y0, x1, y1)

    def screenshot_crop_relative(self, left: float, top: float, right: float, bottom: float) -> bytes:
        """抓取并按比例裁剪 PNG。"""
        return self.capture_frame().crop_relative(left, top, right, bottom)

    def screenshot_crop(self, left: int, top: int, right: int, bottom: int) -> bytes:
        """抓取并按物理像素矩形裁剪 PNG。"""
        return self.capture_frame().crop_pixels(left, top, right, bottom)

    def save_screenshot_crop_relative(self, destination: str | Path, left: float, top: float, right: float, bottom: float) -> Path:
        """Capture a relative crop and save it as PNG."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.screenshot_crop_relative(left, top, right, bottom))
        return destination.resolve()

    def save_screenshot_crop(self, destination: str | Path, left: int, top: int, right: int, bottom: int) -> Path:
        """Capture a physical-pixel crop and save it as PNG."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.screenshot_crop(left, top, right, bottom))
        return destination.resolve()

    @staticmethod
    def _image_match(image: bytes, template: str | Path | bytes, *, confidence: float, region: tuple[int, int, int, int] | tuple[float, float, float, float] | None = None, region_relative: tuple[float, float, float, float] | None = None, region_pixels: tuple[int, int, int, int] | None = None) -> ImageMatch | None:
        return ScreenFrame(image).find_image(template, confidence=confidence, region=region, region_relative=region_relative, region_pixels=region_pixels)

    def _hid_vision_frame(self) -> ScreenFrame | None:
        """Return the existing HID recording JPEG frame, or ``None`` when unavailable.

        The endpoint is present only when the device-side broadcast extension is
        running. It is a best-effort optimization for host-side image matching;
        public screenshot and pixel APIs deliberately retain their PNG contract.
        """
        if self._hid_vision_available is False:
            return None
        try:
            value = self.json("GET", "/api/hid/screenshot", timeout=min(self.timeout, 10.0)).get("value")
            if not isinstance(value, str) or not value or value == "null":
                raise ValueError("HID screenshot response has no image")
            image = base64.b64decode(value, validate=True)
            frame = ScreenFrame(image)
        except (DeviceResponseError, DeviceOperationError, ValueError, binascii.Error, OSError):
            # A missing, malformed or incompatible endpoint should not make a
            # previously working vision workflow fail. Avoid retrying it in every
            # wait loop; the normal PNG endpoint remains the compatibility path.
            self._hid_vision_available = False
            return None
        except DeviceConnectionError:
            # Do not cache transient transport failures as an unavailable feature.
            return None
        self._hid_vision_available = True
        return frame

    def _vision_action_size(self, frame: ScreenFrame) -> dict[str, float]:
        """Return action-coordinate dimensions for a HID frame.

        HID JPEGs can differ by one pixel from the physical PNG action space.
        Calibrate once with the authoritative PNG endpoint, then refresh after
        a rotation. This keeps ImageMatch coordinates safe for ``tap()`` while
        leaving steady-state matching on the faster JPEG path.
        """
        cached = self._vision_action_dimensions
        if cached is not None and (cached["width"] > cached["height"]) == (frame.width > frame.height):
            return cached
        space = self._space_cache
        if space is not None and time.monotonic() < space[0]:
            action = dict(space[2])
            if (action["width"] > action["height"]) == (frame.width > frame.height):
                self._vision_action_dimensions = action
                return action
        action = self.action_size()
        self._vision_action_dimensions = action
        return action

    def _capture_vision_frame(self, *, exact: bool = False) -> tuple[ScreenFrame, dict[str, float]]:
        """Capture one frame for host-side template matching.

        Exact matching remains PNG-only because the HID source is a lossy JPEG.
        Other matching attempts use HID when present and automatically fall back
        to the regular PNG endpoint on unsupported devices.
        """
        if not exact:
            frame = self._hid_vision_frame()
            if frame is not None:
                try:
                    return frame, self._vision_action_size(frame)
                except AScriptError:
                    # Coordinate calibration is not optional; preserve coordinate
                    # correctness by falling back rather than returning raw JPEG
                    # coordinates when the authoritative PNG size is unavailable.
                    pass
        frame = self.capture_frame()
        return frame, {"width": float(frame.width), "height": float(frame.height)}

    @staticmethod
    def _scale_vision_region(region: tuple[int, int, int, int] | tuple[float, float, float, float] | None, action: Mapping[str, float], frame: ScreenFrame) -> tuple[int, int, int, int] | tuple[float, float, float, float] | None:
        """Map an absolute action-space region into a HID frame's pixel space."""
        if region is None or not all(isinstance(value, int) for value in region):
            return region
        x_scale, y_scale = frame.width / action["width"], frame.height / action["height"]
        left, top, right, bottom = region
        return (
            max(0, min(frame.width - 1, math.floor(left * x_scale))),
            max(0, min(frame.height - 1, math.floor(top * y_scale))),
            max(1, min(frame.width, math.ceil(right * x_scale))),
            max(1, min(frame.height, math.ceil(bottom * y_scale))),
        )

    @staticmethod
    def _scale_vision_match(match: ImageMatch | None, action: Mapping[str, float], frame: ScreenFrame) -> ImageMatch | None:
        """Map HID-frame match coordinates back to public action coordinates."""
        if match is None or (action["width"] == frame.width and action["height"] == frame.height):
            return match
        x_scale, y_scale = action["width"] / frame.width, action["height"] / frame.height
        left, top = round(match.x * x_scale), round(match.y * y_scale)
        right, bottom = round((match.x + match.width) * x_scale), round((match.y + match.height) * y_scale)
        return ImageMatch(left, top, max(1, right - left), max(1, bottom - top), match.confidence)

    def _find_image_in_vision_frame(self, frame: ScreenFrame, action: Mapping[str, float], template: str | Path | bytes, *, confidence: float, region: tuple[int, int, int, int] | tuple[float, float, float, float] | None = None, region_relative: tuple[float, float, float, float] | None = None, region_pixels: tuple[int, int, int, int] | None = None) -> ImageMatch | None:
        match = frame.find_image(
            template,
            confidence=confidence,
            region=self._scale_vision_region(region, action, frame),
            region_relative=region_relative,
            region_pixels=self._scale_vision_region(region_pixels, action, frame),
        )
        return self._scale_vision_match(match, action, frame)

    def find_image(self, template: str | Path | bytes, *, confidence: float = 0.9, region: tuple[int, int, int, int] | tuple[float, float, float, float] | None = None, region_relative: tuple[float, float, float, float] | None = None, region_pixels: tuple[int, int, int, int] | None = None) -> ImageMatch | None:
        """在当前截图中匹配一个本机模板。

        ``confidence < 1`` 时优先读取设备已有的 HID 录屏 JPEG 帧；接口不可用
        时自动回退 PNG。精确匹配（``confidence == 1``）保持无损 PNG 语义。
        """
        frame, action = self._capture_vision_frame(exact=confidence == 1)
        return self._find_image_in_vision_frame(frame, action, template, confidence=confidence, region=region, region_relative=region_relative, region_pixels=region_pixels)

    def find_images(self, templates: Mapping[str, str | Path | bytes], *, confidence: float = 0.9, regions: Mapping[str, tuple[int, int, int, int] | tuple[float, float, float, float] | None] | None = None, regions_relative: Mapping[str, tuple[float, float, float, float] | None] | None = None, regions_pixels: Mapping[str, tuple[int, int, int, int] | None] | None = None) -> dict[str, ImageMatch | None]:
        """在一张截图中匹配多个模板。"""
        frame, action = self._capture_vision_frame(exact=confidence == 1)
        return {
            name: self._find_image_in_vision_frame(frame, action, template, confidence=confidence, region=(regions or {}).get(name), region_relative=(regions_relative or {}).get(name), region_pixels=(regions_pixels or {}).get(name))
            for name, template in templates.items()
        }

    def find_any_image(self, templates: Mapping[str, str | Path | bytes], *, confidence: float = 0.9, regions: Mapping[str, tuple[int, int, int, int] | tuple[float, float, float, float] | None] | None = None, regions_relative: Mapping[str, tuple[float, float, float, float] | None] | None = None, regions_pixels: Mapping[str, tuple[int, int, int, int] | None] | None = None) -> tuple[str, ImageMatch] | None:
        """在一张截图中寻找任意模板，返回第一个命中的名称和结果。"""
        frame, action = self._capture_vision_frame(exact=confidence == 1)
        for name, template in templates.items():
            match = self._find_image_in_vision_frame(frame, action, template, confidence=confidence, region=(regions or {}).get(name), region_relative=(regions_relative or {}).get(name), region_pixels=(regions_pixels or {}).get(name))
            if match is not None:
                return name, match
        return None

    def wait_image(self, template: str | Path | bytes, *, confidence: float = 0.9, timeout: float = 10.0, interval: float = 0.5, region: tuple[int, int, int, int] | tuple[float, float, float, float] | None = None, region_relative: tuple[float, float, float, float] | None = None, region_pixels: tuple[int, int, int, int] | None = None, log: bool = False, initial_delay: bool = True) -> ImageMatch:
        """等待本机模板出现并返回 ``ImageMatch``。"""
        if timeout < 0 or interval <= 0: raise ValueError("timeout must be non-negative and interval must be positive")
        deadline = time.monotonic() + timeout
        if initial_delay: time.sleep(min(interval, timeout))
        attempt = 0
        while True:
            attempt += 1
            match = self.find_image(template, confidence=confidence, region=region, region_relative=region_relative, region_pixels=region_pixels)
            if match is not None:
                if log: print(t("image_wait_found", attempt=attempt, x=match.x, y=match.y, confidence=match.confidence))
                return match
            if log: print(t("image_wait_missing", attempt=attempt))
            if time.monotonic() >= deadline: raise TimeoutError("image did not appear before timeout")
            time.sleep(min(interval, deadline - time.monotonic()))

    def wait_any_image(self, templates: Mapping[str, str | Path | bytes], *, confidence: float = 0.9, timeout: float = 10.0, interval: float = 0.5, regions: Mapping[str, tuple[int, int, int, int] | tuple[float, float, float, float] | None] | None = None, regions_relative: Mapping[str, tuple[float, float, float, float] | None] | None = None, regions_pixels: Mapping[str, tuple[int, int, int, int] | None] | None = None, initial_delay: bool = True) -> tuple[str, ImageMatch]:
        """等待任意模板出现；每轮只抓取并解码一张截图。"""
        if not templates: raise ValueError("templates must not be empty")
        if timeout < 0 or interval <= 0: raise ValueError("timeout must be non-negative and interval must be positive")
        deadline = time.monotonic() + timeout
        if initial_delay: time.sleep(min(interval, timeout))
        while True:
            result = self.find_any_image(templates, confidence=confidence, regions=regions, regions_relative=regions_relative, regions_pixels=regions_pixels)
            if result is not None: return result
            if time.monotonic() >= deadline: raise TimeoutError(f"none of the images appeared before timeout: {', '.join(templates)}")
            time.sleep(min(interval, deadline - time.monotonic()))

    def wait_image_gone(self, template: str | Path | bytes, *, confidence: float = 0.9, timeout: float = 10.0, interval: float = 0.5, region: tuple[int, int, int, int] | tuple[float, float, float, float] | None = None, region_relative: tuple[float, float, float, float] | None = None, region_pixels: tuple[int, int, int, int] | None = None, log: bool = False, initial_delay: bool = True) -> bool:
        """等待模板消失，成功返回 ``True``，超时返回 ``False``。"""
        if timeout < 0 or interval <= 0: raise ValueError("timeout must be non-negative and interval must be positive")
        deadline = time.monotonic() + timeout
        if initial_delay: time.sleep(min(interval, timeout))
        attempt = 0
        while True:
            attempt += 1
            match = self.find_image(template, confidence=confidence, region=region, region_relative=region_relative, region_pixels=region_pixels)
            if match is None:
                if log: print(t("image_wait_gone", attempt=attempt))
                return True
            if log: print(t("image_wait_present", attempt=attempt, confidence=match.confidence))
            if time.monotonic() >= deadline: return False
            time.sleep(min(interval, deadline - time.monotonic()))

    def tap_image(self, template: str | Path | bytes, *, confidence: float = 0.9, timeout: float = 10.0, interval: float = 0.5, region: tuple[int, int, int, int] | tuple[float, float, float, float] | None = None, region_relative: tuple[float, float, float, float] | None = None, region_pixels: tuple[int, int, int, int] | None = None, duration: float | None = None, duration_ms: int | None = None) -> ImageMatch:
        """等待模板出现后点击中心，``duration_ms`` 为点击持续毫秒数。"""
        match = self.wait_image(template, confidence=confidence, timeout=timeout, interval=interval, region=region, region_relative=region_relative, region_pixels=region_pixels)
        self.tap(*match.center, duration=duration, duration_ms=duration_ms)
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
        """获取物理像素坐标已归一化的 XML 控件树。"""
        logical, action = self._coordinate_spaces()
        raw = self.request("GET", "/api/node/dump", params={"mode": mode, "depth": depth, "x": x * logical["width"] / action["width"], "y": y * logical["height"] / action["height"]}, timeout=30).decode("utf-8")
        return self._scale_xml_coordinates(raw, action["width"] / logical["width"], action["height"] / logical["height"])

    def ui_tree(self, *, mode: str = "smart", selector: Optional[Mapping[str, Any]] = None, x: float = 0, y: float = 0, normalize: bool = True) -> dict[str, Any]:
        """获取结构化控件树；``x/y`` 为物理像素点探测坐标。

        ``normalize=False`` 跳过坐标空间探测和坐标归一化：整个查询只需
        一次树请求，适合只判断元素存在性或读取非坐标字段的场景；此时
        返回节点坐标为设备端逻辑点，且不能使用点探测 selector。
        """
        params: dict[str, Any] = {"mode": mode}
        if selector is not None:
            params["selector"] = json.dumps(selector, ensure_ascii=False)
        if not normalize:
            if x or y: raise ValueError("point probing requires normalized queries")
            data = self._ok(self.json("GET", "/api/tool/view/dump", params=params)).get("data", {})
            if not isinstance(data, Mapping):
                raise DeviceResponseError("invalid UI tree returned by device", body=repr(data))
            return dict(data)
        logical, action = self._coordinate_spaces()
        params["x"] = x * logical["width"] / action["width"]
        params["y"] = y * logical["height"] / action["height"]
        data = self._ok(self.json("GET", "/api/tool/view/dump", params=params)).get("data", {})
        if not isinstance(data, Mapping):
            raise DeviceResponseError("invalid UI tree returned by device", body=repr(data))
        config = data.get("config") if isinstance(data.get("config"), Mapping) else {}
        scale = config.get("scale")
        try:
            protocol_scale = float(scale)
        except (TypeError, ValueError):
            protocol_scale = 0.0
        derived_x, derived_y = action["width"] / logical["width"], action["height"] / logical["height"]
        if protocol_scale > 0 and math.isclose(protocol_scale, derived_x, rel_tol=0.01) and math.isclose(protocol_scale, derived_y, rel_tol=0.01):
            x_scale = y_scale = protocol_scale
        else:
            x_scale, y_scale = derived_x, derived_y
        return self._scale_tree_coordinates(dict(data), x_scale, y_scale, action)

    def find_elements(self, selector: Mapping[str, Any], *, mode: str = "smart", x: float = 0, y: float = 0, normalize: bool = True) -> list[dict[str, Any]]:
        """Resolve an AScript selector and return its matching element metadata.

        ``selector`` follows the documented AScript view-tree contract, for
        example ``{"sel": [{"key": "label", "params": "OK"}], "find": 99999}``.
        ``normalize=False`` 只发一次树请求，返回的坐标为设备端逻辑点。
        """
        data = self.ui_tree(mode=mode, selector=selector, x=x, y=y, normalize=normalize)
        views = data.get("views") or []
        if not isinstance(views, list):
            raise DeviceResponseError("invalid element list returned by device", body=repr(views))
        return [dict(view) for view in views if isinstance(view, Mapping)]

    @staticmethod
    def _duration_ms(duration: float | None, duration_ms: int | None, *, default_ms: int) -> int:
        if duration is not None and duration_ms is not None: raise ValueError("duration and duration_ms cannot be combined")
        if duration is not None:
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0: raise ValueError("duration must be a finite non-negative number of seconds")
            return int(round(duration * 1000))
        if duration_ms is not None:
            if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0: raise ValueError("duration_ms must be a non-negative integer")
            return duration_ms
        return default_ms

    def current_app(self) -> dict[str, Any]:
        return self._ok(self.json("GET", "/api/node/package")).get("data", {})

    def wait_current_app(self, expected: str | Callable[[Mapping[str, Any]], bool], *, timeout: float = 10.0, interval: float = 0.3) -> dict[str, Any]:
        if timeout < 0 or interval <= 0: raise ValueError("timeout must be non-negative and interval must be positive")
        matcher = (lambda app: app.get("bundle_id") == expected) if isinstance(expected, str) else expected
        if not callable(matcher): raise ValueError("expected must be a bundle id or callable")
        deadline = time.monotonic() + timeout
        while True:
            app = self.current_app()
            if matcher(app): return app
            if time.monotonic() >= deadline: raise LookupError(f"foreground app did not match within {timeout}s")
            time.sleep(min(interval, deadline - time.monotonic()))

    _APP_STATE_NAMES = {0: "not_running", 1: "not_running", 2: "background", 3: "foreground", 4: "foreground"}

    def app_start(self, bundle_id: str, *, timeout: float = 15.0, wait: bool = True) -> dict[str, Any]:
        """启动指定 bundle id 的 App。

        ``wait=True`` 时等待其进入前台（最多 ``timeout`` 秒）；返回启动后的
        ``current_app`` 结果。部分 App 首次启动有隐私弹窗，建议配合 ``watch()``。
        """
        if not bundle_id or not isinstance(bundle_id, str): raise ValueError("bundle_id must be a non-empty string")
        code = "from ascript.ios import system\nsystem.app_start(%r)\n_result=True" % bundle_id
        with self.locked(): self.eval_python(code)
        if not wait: return self.current_app()
        try:
            return self.wait_current_app(bundle_id, timeout=timeout)
        except LookupError:
            raise DeviceOperationError(f"app {bundle_id!r} did not reach the foreground within {timeout}s") from None

    def app_stop(self, bundle_id: str) -> None:
        """停止指定 bundle id 的 App（等价于上划杀掉）。"""
        if not bundle_id or not isinstance(bundle_id, str): raise ValueError("bundle_id must be a non-empty string")
        code = "from ascript.ios import system\nsystem.app_stop(%r)\n_result=True" % bundle_id
        with self.locked(): self.eval_python(code)

    def app_state(self, bundle_id: str) -> dict[str, Any]:
        """返回 App 运行状态：``{"code": 0-4, "state": "not_running|background|foreground"}``。

        设备端 WDA 状态码为静态值（真机实测前台/后台/被杀均返回相同码，
        且不随状态变化），因此 ``"background"`` 与 ``"not_running"`` 在该
        实现上不可区分；客户端在同一帧内同时读取 ``app_current``，bundle
        一致时强制判定为 foreground，其余情况报 not_running。
        """
        if not bundle_id or not isinstance(bundle_id, str): raise ValueError("bundle_id must be a non-empty string")
        code = (
            "from ascript.ios import system as s\n"
            "import json\n"
            "_code = s.app_state(%r)\n"
            "_cur = ''\n"
            "try:\n"
            "    _cur = s.app_current().bundle_id or ''\n"
            "except Exception:\n"
            "    pass\n"
            "_result = json.dumps({'code': int(_code) if _code is not None else -1, 'current': _cur})\n" % bundle_id
        )
        value = self.eval_python(code)
        if not isinstance(value, dict): raise DeviceResponseError("invalid app state returned by device", body=repr(value))
        numeric = value.get("code", -1)
        state = self._APP_STATE_NAMES.get(numeric, "unknown")
        if value.get("current") == bundle_id and state != "foreground": state = "foreground"
        return {"code": numeric, "state": state}

    def lock_screen(self) -> None:
        """锁定屏幕。

        设备端不提供可靠的锁屏状态查询：WDA ``/wda/locked`` 在实测设备上
        恒为 False（锁屏前后均如此，已真机确认）。需要判断时可对截图做
        全黑检测。
        """
        with self.locked(): self.eval_python("from ascript.ios import system\nsystem.lock()\n_result=True")

    def unlock_screen(self) -> None:
        """解锁屏幕；已设锁屏密码的设备无法用本方法解锁，需真机输入密码。"""
        with self.locked(): self.eval_python("from ascript.ios import system\nsystem.unlock()\n_result=True")

    def get_clipboard(self) -> str:
        """读取设备剪贴板文本。"""
        value = self.eval_python("from ascript.ios import system\n_result=str(system.get_clipboard() or '')")
        return value if isinstance(value, str) else str(value)

    def set_clipboard(self, content: str) -> None:
        """写入文本到设备剪贴板。"""
        if not isinstance(content, str): raise ValueError("content must be a string")
        with self.locked(): self.eval_python("from ascript.ios import system\nsystem.set_clipboard(%r)\n_result=True" % content)

    def orientation(self) -> str:
        """返回当前屏幕方向：``"portrait"`` 或 ``"landscape"``。

        设备端 WDA 客户端未提供设置方向的接口，程序化旋转暂不可用；
        方向跟随设备物理旋转实时变化（已真机验证）。
        """
        value = self.eval_python("from ascript.ios import system\n_result=str(system.screen_orientation()).split('.')[-1].lower()")
        if not isinstance(value, str) or value not in ("portrait", "landscape"): raise DeviceResponseError("invalid orientation returned by device", body=repr(value))
        return value

    def open_url(self, url: str) -> None:
        """通过系统 open 打开 URL 或 App 深链。"""
        if not isinstance(url, str) or not url.strip(): raise ValueError("url must be a non-empty string")
        with self.locked(): self.eval_python("from ascript.ios import system\nsystem.open_url(%r)\n_result=True" % url)

    def dismiss_keyboard(self) -> None:
        """尝试收起当前软键盘。"""
        with self.locked(): self.eval_python("from ascript.ios.system import client\nclient.keyboard_dismiss()\n_result=True")

    # 友好名 -> 设备端 wdapy.Keycode 枚举成员名
    _KEY_CODES = {"home": "HOME", "volume_up": "VOLUME_UP", "volume_down": "VOLUME_DOWN", "power": "POWER", "power_plus_home": "POWER_PLUS_HOME", "snapshot": "SNAPSHOT"}

    def press_key(self, key: str) -> None:
        """发送按键事件。``key``：home、volume_up、volume_down、power、power_plus_home、snapshot。"""
        try: member = self._KEY_CODES[key]
        except KeyError: raise ValueError(f"key must be one of {sorted(self._KEY_CODES)}") from None
        with self.locked(): self.eval_python("from ascript.ios.system import client\nfrom ascript.ios.wdapy import Keycode\nclient.press(Keycode.%s)\n_result=True" % member)

    def device_info(self) -> dict[str, Any]:
        """返回设备信息字典；字段缺失时为 ``None``（如 model、name、uuid、locale、界面风格）。"""
        value = self.eval_python(
            "import json\n"
            "from ascript.ios.system import client\n"
            "di = client.device_info()\n"
            "_result = json.dumps({'model': di.model, 'name': di.name, 'uuid': di.uuid,"
            " 'time_zone': di.time_zone, 'current_locale': di.current_locale,"
            " 'user_interface_idiom': di.user_interface_idiom, 'user_interface_style': di.user_interface_style,"
            " 'is_simulator': di.is_simulator})\n"
        )
        return value if isinstance(value, dict) else {}

    def battery_info(self) -> dict[str, Any]:
        """返回电池信息：``{"level": 0.0-1.0, "state": "unplugged|charging|full|unknown"}``。"""
        value = self.eval_python(
            "import json\n"
            "from ascript.ios.system import client\n"
            "bi = client.battery_info()\n"
            "_result = json.dumps({'level': bi.level, 'state': str(bi.state).split('.')[-1].split(':')[0].strip().lower()})\n"
        )
        if not isinstance(value, dict): raise DeviceResponseError("invalid battery info returned by device", body=repr(value))
        try: value["level"] = float(value.get("level"))
        except (TypeError, ValueError): value["level"] = None
        state = str(value.get("state") or "").split(".")[-1].split(":")[0].strip().lower()
        battery_states = {"0": "unknown", "1": "unplugged", "2": "charging", "3": "full"}
        value["state"] = battery_states.get(state, state) or None
        return value

    def element_text(self, node_id: str) -> str:
        """读取元素文本（等价设备端 WDA ``element/text``）；``node_id`` 取自元素属性的 ``id``。"""
        value = self.eval_python("from ascript.ios.node import Node\nfrom ascript.ios.system import client as sc\n_result=str(Node(sc, %r).text)" % node_id)
        return value if isinstance(value, str) else str(value)

    def element_scroll(self, node_id: str, direction: str = "down", distance: float = 1.0) -> None:
        """在可滚动元素内滚动；``direction``：up/down/left/right，``distance`` 为元素宽高的倍数（0..1+）。"""
        if direction not in ("up", "down", "left", "right"): raise ValueError("direction must be one of up/down/left/right")
        if not isinstance(distance, (int, float)) or not 0 < distance <= 5: raise ValueError("distance must be within (0, 5]")
        with self.locked(): self.eval_python("from ascript.ios.node import Node\nfrom ascript.ios.system import client as sc\nNode(sc, %r).scroll(%r, %r)\n_result=True" % (node_id, direction, float(distance)))

    def open_notification(self) -> None:
        """下拉打开通知中心；收起可再上滑或按 home。"""
        size = self.action_size()
        w, h = float(size["width"]), float(size["height"])
        self.swipe(w / 2, 3, w / 2, h * 0.5, duration_ms=400)

    def screen_cache(self, enabled: bool) -> None:
        """开关设备端整帧缓存：开启后首次截图复用同一帧，批量 find_color/OCR 提速；
        画面会变化时务必关闭。等价设备端 ``screen.cache``。"""
        with self.locked(): self.eval_python("from ascript.ios.screen import cache\ncache(%r)\n_result=True" % bool(enabled))

    def notify(self, msg: str, title: str | None = None, *, notification_id: str = "9096") -> None:
        """发送系统通知（脚本完成/告警提醒）。"""
        if not msg or not isinstance(msg, str): raise ValueError("msg must be a non-empty string")
        with self.locked(): self.eval_python("from ascript.ios.developer.api import oc\noc.notify(%r, %r, %r)\n_result=True" % (msg, title, notification_id))

    def _resolve_capture_region(self, region: Any, region_relative: Any) -> tuple[int, int, int, int]:
        if region is not None and region_relative is not None: raise ValueError("region and region_relative cannot be combined")
        size = self.action_size()
        width, height = float(size["width"]), float(size["height"])
        if region_relative is not None:
            l_ratio, t_ratio, r_ratio, b_ratio = (float(value) for value in region_relative)
            if not all(0 <= value <= 1 for value in (l_ratio, t_ratio, r_ratio, b_ratio)): raise ValueError("region_relative values must be within 0..1")
            return round(l_ratio * width), round(t_ratio * height), round(r_ratio * width), round(b_ratio * height)
        if region is not None:
            left, top, right, bottom = (int(value) for value in region)
            return left, top, right, bottom
        return 0, 0, int(width), int(height)

    def find_sift(self, templates: Any, *, threshold: float = 0.5, rgb: bool = False, max_res: int = 0, region: Any = None, region_relative: Any = None) -> list[dict[str, Any]]:
        """SIFT 特征匹配：比模板匹配更抗尺度/光照变化；在设备端原生 OpenCV 上执行。

        ``templates`` 为**设备端**小图路径列表（如 ``~/res/img/x.png``）；本机模板请先
        上传到设备。返回 ``[{"result": (x, y), "rect": (l, t, r, b), "center_x", "center_y",
        "confidence"}]``，坐标为截图像素并已叠加 ``region`` 偏移。``max_res=0`` 返回全部命中。
        """
        if not templates: raise ValueError("templates must be a non-empty sequence of device-side paths")
        paths = [str(item) for item in templates]
        if not 0 < float(threshold) <= 1: raise ValueError("threshold must be within (0, 1]")
        left, top, right, bottom = self._resolve_capture_region(region, region_relative)
        code = (
            "import json\n"
            "from ascript.ios.screen import capture\n"
            "from ascript.ios.developer.api import oc\n"
            "img = capture(rect=(%d, %d, %d, %d))\n"
            "res = oc.find_sift(img, %r, threshold=%r, rgb=%r, max_res=%d, offset_xy=(%d, %d))\n"
            "_result = json.dumps(res)\n" % (left, top, right, bottom, paths, float(threshold), bool(rgb), int(max_res), left, top)
        )
        value = self.eval_python(code)
        return value if isinstance(value, list) else []

    def scan_code(self, *, region: Any = None, region_relative: Any = None) -> list[dict[str, Any]]:
        """二维码/条码识别（设备端原生 MLKitx）。返回 ``[{"result": (x, y), "rect",
        "center_x", "center_y", "value", "type", "format"}]``，坐标为截图像素。"""
        left, top, right, bottom = self._resolve_capture_region(region, region_relative)
        code = (
            "import json\n"
            "from ascript.ios.screen import capture\n"
            "from ascript.ios.developer.api import oc\n"
            "img = capture(rect=(%d, %d, %d, %d))\n"
            "res = oc.code_scanner(img, offset_x=%d, offset_y=%d)\n"
            "_result = json.dumps(res)\n" % (left, top, right, bottom, left, top)
        )
        value = self.eval_python(code)
        return value if isinstance(value, list) else []

    def yolov_load(self, param_path: str, bin_path: str, yaml_path: str | None = None, *, use_gpu: bool = False) -> bool:
        """加载 YOLOv8/v11 ncnn 模型（加载一次后可反复 ``yolov_detect``）。

        三个路径均为**设备端**路径（``.param``/``.bin`` 为 ncnn 权重，``yaml_path``
        可选，用于解析类别名）。返回加载是否成功。
        """
        if not param_path or not bin_path: raise ValueError("param_path and bin_path are required device-side paths")
        code = (
            "from ascript.ios.screen import yolov11\n"
            "_result = bool(yolov11.load(%r, %r, %r, use_gpu=%r))\n" % (str(param_path), str(bin_path), str(yaml_path) if yaml_path else None, bool(use_gpu))
        )
        return bool(self.eval_python(code))

    def yolov_detect(self, *, target_size: int = 640, threshold: float = 0.4, nms_threshold: float = 0.5, region: Any = None, region_relative: Any = None) -> list[dict[str, Any]]:
        """YOLO 目标检测：自动截取当前屏幕（或 ``region``/``region_relative`` 区域）推理。

        返回 ``[{"class_id", "confidence", "rect": [l, t, r, b], "tag"}]``，坐标为
        截图像素（区域检测结果已自动加回偏移）。``tag`` 来自加载时 yaml 的类别名。
        需先 ``yolov_load``。
        """
        if not 0 < float(threshold) <= 1: raise ValueError("threshold must be within (0, 1]")
        if not 0 <= float(nms_threshold) <= 1: raise ValueError("nms_threshold must be within 0..1")
        rect = "None"
        if region is not None or region_relative is not None:
            left, top, right, bottom = self._resolve_capture_region(region, region_relative)
            rect = json.dumps([left, top, right, bottom])
        code = (
            "import json\n"
            "from ascript.ios.screen import yolov11\n"
            "_result = json.dumps(yolov11.detect(target_size=%d, threshold=%r, nms_threshold=%r, rect=%s))\n" % (int(target_size), float(threshold), float(nms_threshold), rect)
        )
        value = self.eval_python(code)
        return value if isinstance(value, list) else []

    def yolov_free(self) -> None:
        """释放已加载的 YOLO 模型。"""
        with self.locked(): self.eval_python("from ascript.ios.screen import yolov11\nyolov11.free()\n_result=True")

    def yolov_nc(self) -> int:
        """返回已加载模型的类别数；未加载时为 0。"""
        return int(self.eval_python("from ascript.ios.screen import yolov11\n_result=int(yolov11.nc())"))

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

    def tap(self, x: float, y: float, *, duration: float | None = None, duration_ms: int | None = None, jitter: int = 0) -> Any:
        """点击物理像素 ``x/y``；``duration`` 单位秒；``jitter`` 为随机抖动像素数(拟人)。"""
        milliseconds = self._duration_ms(duration, duration_ms, default_ms=20)
        jitter = int(jitter or 0)
        if jitter < 0: raise ValueError("jitter must be a non-negative integer")
        with self.locked(): return self.eval_python("from ascript.ios.action import click\nclick(%r, %r, %r, %r)\n_result=True" % (x, y, milliseconds, jitter))

    def click_random(self, x1: float, y1: float, x2: float, y2: float, *, duration: float | None = None, duration_ms: int | None = None) -> Any:
        """在矩形 ``(x1,y1)-(x2,y2)`` 内随机点击一个点(物理像素,两角顺序不限)。"""
        milliseconds = self._duration_ms(duration, duration_ms, default_ms=20)
        corners = (int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2)), milliseconds)
        with self.locked(): return self.eval_python("from ascript.ios.action import click_random\nclick_random(%r, %r, %r, %r, %r)\n_result=True" % corners)

    def click_random_relative(self, x1_ratio: float, y1_ratio: float, x2_ratio: float, y2_ratio: float, *, duration: float | None = None, duration_ms: int | None = None) -> Any:
        """``click_random`` 的比例坐标版本。"""
        a = self.relative_point(x1_ratio, y1_ratio)
        b = self.relative_point(x2_ratio, y2_ratio)
        return self.click_random(a[0], a[1], b[0], b[1], duration=duration, duration_ms=duration_ms)

    def slide_path(self, points: Any, *, durations: Any = None, duration: int = 800, touch_down_duration: int = 0, touch_up_duration: int = 0) -> Any:
        """沿多段轨迹滑动(物理像素点序列,至少两个点)。

        ``durations`` 为每段移动耗时(毫秒)列表,长度须为点数减一;
        缺省时把 ``duration`` 均分到各段。``touch_down_duration``/``touch_up_duration``
        为按下后/松开前的额外停留毫秒数。
        """
        pts = [[float(point[0]), float(point[1])] for point in points]
        if len(pts) < 2: raise ValueError("slide_path requires at least two points")
        durations_json = json.dumps([int(item) for item in durations]) if durations is not None else "None"
        with self.locked(): return self.eval_python("import json\nfrom ascript.ios.action import slide_path\nslide_path(json.loads(%r), duration=%d, durations=%s, touch_down_duration=%d, touch_up_duration=%d)\n_result=True" % (json.dumps(pts), int(duration), durations_json, int(touch_down_duration), int(touch_up_duration)))

    def slide_path_relative(self, points: Any, *, durations: Any = None, duration: int = 800, touch_down_duration: int = 0, touch_up_duration: int = 0) -> Any:
        """``slide_path`` 的比例坐标版本;``points`` 为 0..1 比例点序列。"""
        absolute = [self.relative_point(point[0], point[1]) for point in points]
        return self.slide_path(absolute, durations=durations, duration=duration, touch_down_duration=touch_down_duration, touch_up_duration=touch_up_duration)

    def touch_and_slide(self, from_x: float, from_y: float, to_x: float, to_y: float, *, touch_down_duration: int = 500, touch_move_duration: int = 1000, touch_up_duration: int = 500) -> Any:
        """带停留的拖拽:按下停 ``touch_down_duration`` 毫秒 → 移动 ``touch_move_duration`` 毫秒 → 松开前停 ``touch_up_duration`` 毫秒。"""
        with self.locked(): return self.eval_python("from ascript.ios.action import touch_and_slide\ntouch_and_slide(%r, %r, %r, %r, %r, %r, %r)\n_result=True" % (from_x, from_y, to_x, to_y, touch_down_duration / 1000.0, touch_move_duration / 1000.0, touch_up_duration / 1000.0))

    def touch_and_slide_relative(self, from_x_ratio: float, from_y_ratio: float, to_x_ratio: float, to_y_ratio: float, *, touch_down_duration: int = 500, touch_move_duration: int = 1000, touch_up_duration: int = 500) -> Any:
        """``touch_and_slide`` 的比例坐标版本。"""
        start = self.relative_point(from_x_ratio, from_y_ratio)
        end = self.relative_point(to_x_ratio, to_y_ratio)
        return self.touch_and_slide(start[0], start[1], end[0], end[1], touch_down_duration=touch_down_duration, touch_move_duration=touch_move_duration, touch_up_duration=touch_up_duration)

    def long_press(self, x: float, y: float, *, duration: float | None = None, duration_ms: int | None = None) -> Any:
        """在物理像素 ``x/y`` 长按；默认 0.8 秒。"""
        milliseconds = self._duration_ms(duration, duration_ms, default_ms=800)
        return self.tap(x, y, duration_ms=milliseconds)

    def double_tap(self, x: float, y: float, *, duration: float | None = None, duration_ms: int | None = None, interval: float = 0.08) -> Any:
        """在物理像素 ``x/y`` 连续点击两次。"""
        milliseconds = self._duration_ms(duration, duration_ms, default_ms=20)
        if not math.isfinite(interval) or interval < 0: raise ValueError("interval must be a finite non-negative number of seconds")
        self.tap(x, y, duration_ms=milliseconds); time.sleep(interval)
        return self.tap(x, y, duration_ms=milliseconds)

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
        """Return logical and action sizes, reusing a short-TTL cache.

        轮询类查询每轮都会读取坐标空间；缓存把 3 次设备往返降到 1 次。
        只缓存截图成功的结果；截屏失败时不缓存，下次重试。旋转屏幕会
        改变物理尺寸，旋转敏感的流程可设 ``coordinate_cache_ttl=0``。
        """
        now = time.monotonic()
        if self._space_cache is not None and now < self._space_cache[0]:
            return self._space_cache[1], self._space_cache[2]
        logical = self._logical_screen()
        try:
            action = self.screen_size()
        except DeviceResponseError:
            # A malformed screenshot must not make tree inspection unusable.
            return logical, logical
        if self.coordinate_cache_ttl > 0:
            self._space_cache = (now + self.coordinate_cache_ttl, logical, action)
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

    def tap_relative(self, x_ratio: float, y_ratio: float, *, duration: float | None = None, duration_ms: int | None = None, jitter: int = 0) -> Any:
        """按屏幕比例点击；``duration`` 单位秒；``jitter`` 为随机抖动像素数。"""
        return self.tap(*self.relative_point(x_ratio, y_ratio), **({"duration": duration} if duration is not None else {"duration_ms": duration_ms} if duration_ms is not None else {}), jitter=jitter)

    def long_press_relative(self, x_ratio: float, y_ratio: float, *, duration: float | None = None, duration_ms: int | None = None) -> Any:
        return self.long_press(*self.relative_point(x_ratio, y_ratio), duration=duration, duration_ms=duration_ms)

    def double_tap_relative(self, x_ratio: float, y_ratio: float, *, duration: float | None = None, duration_ms: int | None = None, interval: float = 0.08) -> Any:
        return self.double_tap(*self.relative_point(x_ratio, y_ratio), duration=duration, duration_ms=duration_ms, interval=interval)

    def swipe(self, x1: float, y1: float, x2: float, y2: float, *, duration: float | None = None, duration_ms: int | None = None) -> Any:
        milliseconds = self._duration_ms(duration, duration_ms, default_ms=200)
        with self.locked(): return self.eval_python("from ascript.ios.action import slide\nslide(%r, %r, %r, %r, %r)\n_result=True" % (x1, y1, x2, y2, milliseconds))

    def drag(self, x1: float, y1: float, x2: float, y2: float, *, duration: float | None = None, duration_ms: int | None = None) -> Any:
        """从一个物理像素点拖拽到另一个点；默认 0.5 秒。"""
        milliseconds = self._duration_ms(duration, duration_ms, default_ms=500)
        return self.swipe(x1, y1, x2, y2, duration_ms=milliseconds)

    def swipe_relative(self, x1_ratio: float, y1_ratio: float, x2_ratio: float, y2_ratio: float, *, duration: float | None = None, duration_ms: int | None = None) -> Any:
        """按屏幕比例滑动；``duration`` 单位秒。"""
        return self.swipe(*self.relative_point(x1_ratio, y1_ratio), *self.relative_point(x2_ratio, y2_ratio), duration=duration, duration_ms=duration_ms)

    def drag_relative(self, x1_ratio: float, y1_ratio: float, x2_ratio: float, y2_ratio: float, *, duration: float | None = None, duration_ms: int | None = None) -> Any:
        return self.drag(*self.relative_point(x1_ratio, y1_ratio), *self.relative_point(x2_ratio, y2_ratio), duration=duration, duration_ms=duration_ms)

    def scroll_until_image(self, template: str | Path | bytes, *, direction: str = "down", swipe_relative: tuple[float, float, float, float] | None = None, x1_ratio: float | None = None, y1_ratio: float | None = None, x2_ratio: float | None = None, y2_ratio: float | None = None, confidence: float = 0.9, timeout: float = 20.0, interval: float = 0.5, max_swipes: int = 10, region: tuple[int, int, int, int] | tuple[float, float, float, float] | None = None, region_relative: tuple[float, float, float, float] | None = None, region_pixels: tuple[int, int, int, int] | None = None, duration: float | None = None, duration_ms: int | None = None, log: bool = False, initial_delay: bool = True) -> ImageMatch:
        """Swipe in ``direction`` until a template appears, then return its match."""
        if timeout < 0 or interval <= 0 or max_swipes < 0:
            raise ValueError("timeout must be non-negative, interval positive, and max_swipes non-negative")
        x1, y1, x2, y2 = swipe_gesture(direction, swipe_relative, x1_ratio, y1_ratio, x2_ratio, y2_ratio)
        # Validate a custom ratio gesture before the initial wait or any action.
        self.relative_point(x1, y1); self.relative_point(x2, y2)
        deadline = time.monotonic() + timeout
        if initial_delay: time.sleep(min(interval, timeout))
        for swipe_number in range(max_swipes + 1):
            match = self.find_image(template, confidence=confidence, region=region, region_relative=region_relative, region_pixels=region_pixels)
            attempt = swipe_number + 1
            if match is not None:
                if log: print(t("image_scroll_match", attempt=attempt, x=match.x, y=match.y, confidence=match.confidence))
                return match
            if swipe_number == max_swipes or time.monotonic() >= deadline:
                if log: print(t("image_scroll_stop", attempt=attempt))
                break
            if log: print(t("image_scroll_next", attempt=attempt))
            self.swipe_relative(x1, y1, x2, y2, **({"duration": duration} if duration is not None else {"duration_ms": duration_ms} if duration_ms is not None else {}))
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
        raise TimeoutError(f"image did not appear after {max_swipes} {direction} swipes or before timeout")

    def input_text(self, text: str, *, interval_ms: int = 120) -> Any:
        """向当前焦点输入文本；``interval_ms`` 为字符间隔毫秒数。"""
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

    def ocr_raw(self, *, region: tuple[int, int, int, int] | None = None, region_relative: tuple[float, float, float, float] | None = None) -> Any:
        if region is not None and region_relative is not None: raise ValueError("region and region_relative cannot be combined")
        if region_relative is not None:
            frame = self.capture_frame(); left, top, right, bottom = frame._region(None, region_relative, None); region = (left, top, right, bottom)
        rect = "|".join(str(value) for value in region) if region else None
        return self.gp("ascript.ios.screen.Ocr", "mode=5, confidence=0.1" + (f", rect=[{rect}]" if rect else ""))

    def ocr(self, *, region: tuple[int, int, int, int] | None = None, region_relative: tuple[float, float, float, float] | None = None) -> OcrResult:
        raw = self.ocr_raw(region=region, region_relative=region_relative)
        try: payload = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError: payload = raw
        entries = payload.get("data", []) if isinstance(payload, Mapping) else payload if isinstance(payload, list) else []
        items: list[OcrItem] = []
        for entry in entries:
            if not isinstance(entry, Mapping): continue
            rect_value = entry.get("rect")
            rect = tuple(int(value) for value in rect_value) if isinstance(rect_value, list) and len(rect_value) == 4 else None
            confidence = float(entry["confidence"]) if isinstance(entry.get("confidence"), (int, float)) else None
            items.append(OcrItem(str(entry.get("text") or ""), rect, confidence, dict(entry)))
        return OcrResult(tuple(items), payload)

    def find_ocr_text(self, text: str, *, contains: bool = True, region: tuple[int, int, int, int] | None = None, region_relative: tuple[float, float, float, float] | None = None) -> list[OcrItem]:
        result = self.ocr(region=region, region_relative=region_relative)
        return [item for item in result.items if text in item.text] if contains else [item for item in result.items if item.text == text]

    def wait_ocr_text(self, text: str, *, contains: bool = True, timeout: float = 10.0, interval: float = 0.5, region: tuple[int, int, int, int] | None = None, region_relative: tuple[float, float, float, float] | None = None) -> OcrItem:
        if timeout < 0 or interval <= 0: raise ValueError("timeout must be non-negative and interval must be positive")
        deadline = time.monotonic() + timeout
        while True:
            found = self.find_ocr_text(text, contains=contains, region=region, region_relative=region_relative)
            if found: return found[0]
            if time.monotonic() >= deadline: raise LookupError(f"OCR text did not appear within {timeout}s: {text}")
            time.sleep(min(interval, deadline - time.monotonic()))

    def color_matches(self, x: int, y: int, expected: PixelColor | tuple[int, int, int] | tuple[int, int, int, int] | str, *, tolerance: int = 0, include_alpha: bool = False) -> bool:
        return self.capture_frame().color_matches(x, y, expected, tolerance=tolerance, include_alpha=include_alpha)

    def color_matches_relative(self, x_ratio: float, y_ratio: float, expected: PixelColor | tuple[int, int, int] | tuple[int, int, int, int] | str, *, tolerance: int = 0, include_alpha: bool = False) -> bool:
        return self.capture_frame().color_matches_relative(x_ratio, y_ratio, expected, tolerance=tolerance, include_alpha=include_alpha)

    def find_color(self, expected: PixelColor | tuple[int, int, int] | tuple[int, int, int, int] | str, *, tolerance: int = 0, region: tuple[int, int, int, int] | None = None, region_relative: tuple[float, float, float, float] | None = None) -> tuple[int, int] | None:
        return self.capture_frame().find_color(expected, tolerance=tolerance, region=region, region_relative=region_relative)

    def count_color(self, expected: PixelColor | tuple[int, int, int] | tuple[int, int, int, int] | str, *, tolerance: int = 0, region: tuple[int, int, int, int] | None = None, region_relative: tuple[float, float, float, float] | None = None) -> int:
        return self.capture_frame().count_color(expected, tolerance=tolerance, region=region, region_relative=region_relative)

    def assert_color(self, x: int, y: int, expected: PixelColor | tuple[int, int, int] | tuple[int, int, int, int] | str, *, tolerance: int = 0, include_alpha: bool = False) -> PixelColor:
        return self.capture_frame().assert_color(x, y, expected, tolerance=tolerance, include_alpha=include_alpha)

    def assert_color_relative(self, x_ratio: float, y_ratio: float, expected: PixelColor | tuple[int, int, int] | tuple[int, int, int, int] | str, *, tolerance: int = 0, include_alpha: bool = False) -> PixelColor:
        frame = self.capture_frame(); x, y = frame.point_relative(x_ratio, y_ratio)
        return frame.assert_color(x, y, expected, tolerance=tolerance, include_alpha=include_alpha)

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

    def rename_remote(self, path: str, new_name: str) -> None:
        """重命名设备端文件或目录（同目录内）。

        ``path`` 为完整远程路径（支持 ``~/`` 前缀）；``new_name`` 只是新名字，
        不能包含路径分隔符，设备端会在原目录下完成改名。
        """
        if not new_name or "/" in new_name or "\\" in new_name: raise ValueError("new_name must be a bare file name without path separators")
        with self.locked(): self._ok(self.json("GET", "/api/file/rename", params={"path": path, "name": new_name}))

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
        if deadline is not None and time.monotonic() >= deadline:
            return
        while not stop_event or not stop_event.is_set():
            try:
                complete = yield from self._logs_once(deadline=deadline, stop_event=stop_event)
                if complete or remaining <= 0:
                    return
                remaining -= 1
                if not self._wait_to_reconnect(reconnect_delay, deadline, stop_event):
                    return
            except (DeviceConnectionError, ProtocolError, OSError):
                if remaining <= 0:
                    raise
                remaining -= 1
                if not self._wait_to_reconnect(reconnect_delay, deadline, stop_event):
                    return

    @staticmethod
    def _wait_to_reconnect(delay: float, deadline: Optional[float], stop_event: Optional[threading.Event]) -> bool:
        wait = max(0.0, delay)
        if deadline is not None:
            wait = min(wait, max(0.0, deadline - time.monotonic()))
            if wait <= 0:
                return False
        if stop_event is not None:
            return not stop_event.wait(wait)
        time.sleep(wait)
        return deadline is None or time.monotonic() < deadline

    def _logs_once(self, *, deadline: Optional[float], stop_event: Optional[threading.Event]) -> Iterator[LogEntry]:
        headers = {"Cookie": f"airscript={self.password}"} if self.password else None
        connect_timeout = self.timeout
        if deadline is not None:
            connect_timeout = min(connect_timeout, max(0.001, deadline - time.monotonic()))
        try:
            websocket = WebSocket.connect(self.address.host, LOG_PORT, "/log/", timeout=connect_timeout, headers=headers, deadline=deadline, stop_event=stop_event)
        except TimeoutError:
            if (deadline is not None and time.monotonic() >= deadline) or (stop_event is not None and stop_event.is_set()):
                return True
            raise
        except OSError as exc:
            raise DeviceConnectionError(t("cannot_reach_logs", host=self.address.host, port=LOG_PORT, detail=exc)) from exc
        try:
            while not stop_event or not stop_event.is_set():
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return True
                    websocket.settimeout(min(0.5, remaining))
                try:
                    message = websocket.receive()
                except TimeoutError:
                    continue
                if message is None:
                    return websocket.close_code in {None, 1000, 1001}
                opcode, payload = message
                if opcode != 0x1:
                    continue
                text = payload.decode("utf-8")
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    event = None
                if isinstance(event, Mapping):
                    yield LogEntry(str(event.get("msg", "")), str(event.get("type", "o")), str(event.get("time", "")))
                else:
                    yield LogEntry(text)
            return True
        finally:
            websocket.close()

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
