"""Local screenshot, color, and template-matching primitives."""
from __future__ import annotations

import math
import threading
import warnings
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .client import ImageMatch

@dataclass(frozen=True)
class _TemplateData:
    image: object
    rgb: bytes
    samples: tuple[tuple[int, int, tuple[int, int, int]], ...]


_TEMPLATE_CACHE: OrderedDict[tuple[str, int, int], _TemplateData] = OrderedDict()
_TEMPLATE_CACHE_LIMIT = 32
_TEMPLATE_CACHE_LOCK = threading.RLock()


@dataclass(frozen=True)
class PixelColor:
    """RGBA color sampled from a physical screenshot pixel."""
    r: int
    g: int
    b: int
    a: int = 255
    @classmethod
    def parse(cls, value: "PixelColor | tuple[int, int, int] | tuple[int, int, int, int] | str") -> "PixelColor":
        if isinstance(value, cls): return value
        if isinstance(value, str):
            text = value.removeprefix("#")
            if len(text) not in {6, 8} or any(char not in "0123456789abcdefABCDEF" for char in text):
                raise ValueError("hex color must be #RRGGBB or #RRGGBBAA")
            values = tuple(int(text[index:index + 2], 16) for index in range(0, len(text), 2))
            return cls(*values) if len(values) == 4 else cls(*values, 255)
        if not isinstance(value, tuple) or len(value) not in {3, 4} or any(not isinstance(channel, int) or isinstance(channel, bool) or not 0 <= channel <= 255 for channel in value):
            raise ValueError("color must be PixelColor, RGB/RGBA tuple, or #RRGGBB/#RRGGBBAA")
        return cls(*value) if len(value) == 4 else cls(*value, 255)

    def matches(self, value: "PixelColor | tuple[int, int, int] | tuple[int, int, int, int] | str", *, tolerance: int = 0, include_alpha: bool = False) -> bool:
        expected = self.parse(value)
        if not isinstance(tolerance, int) or isinstance(tolerance, bool) or tolerance < 0: raise ValueError("tolerance must be a non-negative integer")
        actual, target = self.rgba if include_alpha else self.rgb, expected.rgba if include_alpha else expected.rgb
        return all(abs(one - two) <= tolerance for one, two in zip(actual, target))

    @property
    def rgb(self) -> tuple[int, int, int]: return self.r, self.g, self.b
    @property
    def rgba(self) -> tuple[int, int, int, int]: return self.r, self.g, self.b, self.a
    @property
    def hex(self) -> str: return f"#{self.r:02X}{self.g:02X}{self.b:02X}"


def relative_point(width: int, height: int, x_ratio: float, y_ratio: float) -> tuple[int, int]:
    try: x_ratio, y_ratio = float(x_ratio), float(y_ratio)
    except (TypeError, ValueError) as exc: raise ValueError("relative coordinates must be finite numbers between 0 and 1") from exc
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in (x_ratio, y_ratio)):
        raise ValueError("relative coordinates must be finite numbers between 0 and 1")
    return min(width - 1, int(width * x_ratio)), min(height - 1, int(height * y_ratio))


class ScreenFrame:
    """One immutable device screenshot using physical-pixel coordinates."""
    def __init__(self, png: bytes):
        try: from PIL import Image
        except ImportError as exc: raise RuntimeError("vision features require Pillow; reinstall asclient to install its dependencies") from exc
        self.png = bytes(png)
        with Image.open(BytesIO(self.png)) as source: self._image = source.convert("RGBA")
        self.width, self.height = self._image.size
        self._rgb = None
        self._rgb_bytes = None

    @property
    def size(self) -> dict[str, float]: return {"width": float(self.width), "height": float(self.height)}
    def point_relative(self, x_ratio: float, y_ratio: float) -> tuple[int, int]: return relative_point(self.width, self.height, x_ratio, y_ratio)
    def pixel(self, x: int, y: int) -> PixelColor:
        if not isinstance(x, int) or not isinstance(y, int) or not (0 <= x < self.width and 0 <= y < self.height):
            raise ValueError(f"pixel coordinates must be within 0..{self.width - 1}, 0..{self.height - 1}")
        return PixelColor(*self._image.getpixel((x, y)))
    def pixel_relative(self, x_ratio: float, y_ratio: float) -> PixelColor: return self.pixel(*self.point_relative(x_ratio, y_ratio))
    def pixels(self, points: Iterable[tuple[int, int]]) -> list[PixelColor]: return [self.pixel(x, y) for x, y in points]
    def pixels_relative(self, points: Iterable[tuple[float, float]]) -> list[PixelColor]: return [self.pixel_relative(x, y) for x, y in points]

    def crop_pixels(self, left: int, top: int, right: int, bottom: int) -> bytes:
        if not all(isinstance(value, int) for value in (left, top, right, bottom)) or not (0 <= left < right <= self.width and 0 <= top < bottom <= self.height):
            raise ValueError("crop pixels must satisfy screen bounds and left < right, top < bottom")
        output = BytesIO(); self._image.crop((left, top, right, bottom)).save(output, "PNG")
        return output.getvalue()

    def crop_relative(self, left: float, top: float, right: float, bottom: float) -> bytes:
        x0, y0, x1, y1 = self._region(None, (left, top, right, bottom), None)
        x1, y1 = max(x1, x0 + 1), max(y1, y0 + 1)
        return self.crop_pixels(x0, y0, min(self.width, x1), min(self.height, y1))

    def color_matches(self, x: int, y: int, expected: PixelColor | tuple[int, int, int] | tuple[int, int, int, int] | str, *, tolerance: int = 0, include_alpha: bool = False) -> bool:
        return self.pixel(x, y).matches(expected, tolerance=tolerance, include_alpha=include_alpha)

    def color_matches_relative(self, x_ratio: float, y_ratio: float, expected: PixelColor | tuple[int, int, int] | tuple[int, int, int, int] | str, *, tolerance: int = 0, include_alpha: bool = False) -> bool:
        return self.pixel_relative(x_ratio, y_ratio).matches(expected, tolerance=tolerance, include_alpha=include_alpha)

    def find_color(self, expected: PixelColor | tuple[int, int, int] | tuple[int, int, int, int] | str, *, tolerance: int = 0, region: tuple[int, int, int, int] | None = None, region_relative: tuple[float, float, float, float] | None = None, include_alpha: bool = False) -> tuple[int, int] | None:
        x0, y0, x1, y1 = self._region(region, region_relative, None)
        for y in range(y0, y1):
            for x in range(x0, x1):
                if self.color_matches(x, y, expected, tolerance=tolerance, include_alpha=include_alpha): return x, y
        return None

    def count_color(self, expected: PixelColor | tuple[int, int, int] | tuple[int, int, int, int] | str, *, tolerance: int = 0, region: tuple[int, int, int, int] | None = None, region_relative: tuple[float, float, float, float] | None = None, include_alpha: bool = False) -> int:
        x0, y0, x1, y1 = self._region(region, region_relative, None)
        return sum(self.color_matches(x, y, expected, tolerance=tolerance, include_alpha=include_alpha) for y in range(y0, y1) for x in range(x0, x1))

    def assert_color(self, x: int, y: int, expected: PixelColor | tuple[int, int, int] | tuple[int, int, int, int] | str, *, tolerance: int = 0, include_alpha: bool = False) -> PixelColor:
        actual = self.pixel(x, y)
        if not actual.matches(expected, tolerance=tolerance, include_alpha=include_alpha): raise AssertionError(f"color at ({x}, {y}) is {actual.hex}, expected {PixelColor.parse(expected).hex}")
        return actual

    def _region(self, region: tuple[int, int, int, int] | tuple[float, float, float, float] | None, region_relative: tuple[float, float, float, float] | None, region_pixels: tuple[int, int, int, int] | None) -> tuple[int, int, int, int]:
        supplied = sum(value is not None for value in (region, region_relative, region_pixels))
        if supplied > 1: raise ValueError("region, region_relative, and region_pixels cannot be combined")
        if region_pixels is not None:
            warnings.warn("region_pixels is deprecated; use region for physical pixels", DeprecationWarning, stacklevel=3)
            region = region_pixels
        if region_relative is not None:
            try: left, top, right, bottom = (float(value) for value in region_relative)
            except (TypeError, ValueError) as exc: raise ValueError("region_relative must contain four ratios") from exc
            if not all(math.isfinite(value) and 0 <= value <= 1 for value in (left, top, right, bottom)) or left >= right or top >= bottom:
                raise ValueError("region_relative must satisfy 0 <= left < right <= 1 and 0 <= top < bottom <= 1")
            return int(self.width * left), int(self.height * top), max(int(self.width * right), int(self.width * left) + 1), max(int(self.height * bottom), int(self.height * top) + 1)
        if region is None: return 0, 0, self.width, self.height
        if len(region) != 4: raise ValueError("region must contain four physical pixel coordinates")
        if not all(isinstance(value, int) for value in region):
            warnings.warn("relative region is deprecated; use region_relative", DeprecationWarning, stacklevel=3)
            return self._region(None, tuple(float(value) for value in region), None)
        left, top, right, bottom = region
        if not (0 <= left < right <= self.width and 0 <= top < bottom <= self.height):
            raise ValueError("region must satisfy screen bounds and left < right, top < bottom")
        return region

    @staticmethod
    def _template(template: str | Path | bytes):
        try: from PIL import Image
        except ImportError as exc: raise RuntimeError("image matching requires Pillow; reinstall asclient to install its dependencies") from exc
        if isinstance(template, bytes):
            with Image.open(BytesIO(template)) as source: decoded = source.convert("RGB")
            return ScreenFrame._template_data(decoded)
        path = Path(template).resolve(); stat = path.stat(); key = (str(path), stat.st_mtime_ns, stat.st_size)
        with _TEMPLATE_CACHE_LOCK:
            cached = _TEMPLATE_CACHE.get(key)
            if cached is not None:
                _TEMPLATE_CACHE.move_to_end(key); return cached
        with Image.open(path) as source: decoded = source.convert("RGB")
        data = ScreenFrame._template_data(decoded)
        with _TEMPLATE_CACHE_LOCK:
            existing = _TEMPLATE_CACHE.get(key)
            if existing is not None:
                _TEMPLATE_CACHE.move_to_end(key); return existing
            _TEMPLATE_CACHE[key] = data
            while len(_TEMPLATE_CACHE) > _TEMPLATE_CACHE_LIMIT: _TEMPLATE_CACHE.popitem(last=False)
        return data

    @staticmethod
    def _template_data(image):
        width, height = image.size; pixels = image.load()
        sample_x = sorted({round(index * (width - 1) / 7) for index in range(8)})
        sample_y = sorted({round(index * (height - 1) / 7) for index in range(8)})
        return _TemplateData(image, image.tobytes(), tuple((x, y, pixels[x, y]) for y in sample_y for x in sample_x))

    def _find_exact(self, needle: _TemplateData, region: tuple[int, int, int, int]) -> "ImageMatch | None":
        from .client import ImageMatch

        if self._rgb_bytes is None:
            self._rgb_bytes = self._rgb.tobytes()
        x0, y0, x1, y1 = region
        template_width, template_height = needle.image.size
        row_size = template_width * 3
        stride = self.width * 3
        first_row = needle.rgb[:row_size]
        source = self._rgb_bytes
        for y in range(y0, y1 - template_height + 1):
            row_start = y * stride + x0 * 3
            row_end = y * stride + (x1 - template_width) * 3
            offset = source.find(first_row, row_start, row_end + row_size)
            while offset >= 0 and offset <= row_end:
                if (offset - y * stride) % 3 == 0:
                    x = (offset - y * stride) // 3
                    if all(
                        source[offset + ty * stride:offset + ty * stride + row_size]
                        == needle.rgb[ty * row_size:(ty + 1) * row_size]
                        for ty in range(1, template_height)
                    ):
                        return ImageMatch(x, y, template_width, template_height, 1.0)
                offset = source.find(first_row, offset + 1, row_end + row_size)
        return None

    def find_image(self, template: str | Path | bytes, *, confidence: float = 0.9, region: tuple[int, int, int, int] | tuple[float, float, float, float] | None = None, region_relative: tuple[float, float, float, float] | None = None, region_pixels: tuple[int, int, int, int] | None = None) -> "ImageMatch | None":
        from .client import ImageMatch
        if not math.isfinite(confidence) or not 0 < confidence <= 1: raise ValueError("confidence must be a finite number in (0, 1]")
        needle = self._template(template)
        if self._rgb is None: self._rgb = self._image.convert("RGB")
        haystack = self._rgb; template_width, template_height = needle.image.size; x0, y0, x1, y1 = self._region(region, region_relative, region_pixels)
        if template_width > x1 - x0 or template_height > y1 - y0: return None
        if confidence == 1:
            return self._find_exact(needle, (x0, y0, x1, y1))
        exact = self._find_exact(needle, (x0, y0, x1, y1))
        if exact is not None:
            return exact
        source_pixels, template_pixels = haystack.load(), needle.image.load()
        pixel_count = template_width * template_height * 3
        allowed = (1 - confidence) * 255 * pixel_count
        best = None
        for y in range(y0, y1 - template_height + 1):
            for x in range(x0, x1 - template_width + 1):
                error = 0
                for tx, ty, two in needle.samples:
                    one = source_pixels[x + tx, y + ty]
                    error += abs(one[0] - two[0]) + abs(one[1] - two[1]) + abs(one[2] - two[2])
                    if error > allowed: break
                if error > allowed: continue
                error = 0
                for ty in range(template_height):
                    for tx in range(template_width):
                        one, two = source_pixels[x + tx, y + ty], template_pixels[tx, ty]
                        error += abs(one[0] - two[0]) + abs(one[1] - two[1]) + abs(one[2] - two[2])
                        if error > allowed: break
                    if error > allowed: break
                score = 1 - error / (255 * pixel_count)
                if error <= allowed and (best is None or score > best.confidence): best = ImageMatch(x, y, template_width, template_height, score)
        return best

    def find_images(self, templates: dict[str, str | Path | bytes], *, confidence: float = 0.9, regions: dict[str, tuple[int, int, int, int] | tuple[float, float, float, float] | None] | None = None, regions_relative: dict[str, tuple[float, float, float, float] | None] | None = None, regions_pixels: dict[str, tuple[int, int, int, int] | None] | None = None) -> dict[str, "ImageMatch | None"]:
        return {name: self.find_image(template, confidence=confidence, region=(regions or {}).get(name), region_relative=(regions_relative or {}).get(name), region_pixels=(regions_pixels or {}).get(name)) for name, template in templates.items()}
