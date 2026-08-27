"""uiautomator2-inspired automation primitives for AScript iOS devices.

The mobile service remains unchanged.  Selectors are serialized to AScript's
existing ``/api/tool/view/dump`` selector protocol, and actions use the
already-public coordinate APIs.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, TYPE_CHECKING

from .i18n import t

if TYPE_CHECKING:
    from .client import AScriptClient


@dataclass(frozen=True)
class Selector:
    """An immutable AScript element selector.

    ``device(text="Continue", type="XCUIElementTypeButton")`` is the most
    convenient entry point.  ``contains`` changes matching for one property.
    """

    attributes: tuple[tuple[str, Any, int], ...] = ()
    mode: str = "smart"
    point: tuple[float, float] | None = None
    max_depth: int = 0
    max_children: int = 30

    EQUAL = 0
    CONTAINS = 1
    MATCHES = 2

    def with_attr(self, key: str, value: Any, *, match: int = EQUAL) -> "Selector":
        if key not in {"name", "label", "value", "title", "type", "enabled", "selected", "focused", "visible", "index", "traits", "childCount"}:
            raise ValueError(f"unsupported selector attribute: {key}")
        return Selector(self.attributes + ((key, value, match),), self.mode, self.point, self.max_depth, self.max_children)

    def name(self, value: str, *, contains: bool = False) -> "Selector": return self.with_attr("name", value, match=self.CONTAINS if contains else self.EQUAL)
    def label(self, value: str, *, contains: bool = False) -> "Selector": return self.with_attr("label", value, match=self.CONTAINS if contains else self.EQUAL)
    def text(self, value: str, *, contains: bool = False) -> "Selector": return self.label(value, contains=contains)
    def value(self, value: str, *, contains: bool = False) -> "Selector": return self.with_attr("value", value, match=self.CONTAINS if contains else self.EQUAL)
    def type(self, value: str) -> "Selector": return self.with_attr("type", value)
    def title(self, value: str, *, contains: bool = False) -> "Selector": return self.with_attr("title", value, match=self.CONTAINS if contains else self.EQUAL)
    def enabled(self, value: bool = True) -> "Selector": return self.with_attr("enabled", value)
    def visible(self, value: bool = True) -> "Selector": return self.with_attr("visible", value)
    def selected(self, value: bool = True) -> "Selector": return self.with_attr("selected", value)
    def focused(self, value: bool = True) -> "Selector": return self.with_attr("focused", value)
    def traits(self, value: int) -> "Selector": return self.with_attr("traits", value)
    def child_count(self, value: int) -> "Selector": return self.with_attr("childCount", value)
    def index(self, value: int) -> "Selector": return self.with_attr("index", value)
    def at(self, x: float, y: float) -> "Selector": return Selector(self.attributes, "point", (x, y), self.max_depth, self.max_children)
    def full(self) -> "Selector": return Selector(self.attributes, "full", self.point, self.max_depth, self.max_children)
    def with_limits(self, *, max_depth: int = 0, max_children: int = 30) -> "Selector":
        if max_depth < 0 or max_children < 0: raise ValueError("selector limits cannot be negative")
        return Selector(self.attributes, self.mode, self.point, max_depth, max_children)

    def payload(self, *, find: int = 99999) -> dict[str, Any]:
        conditions = []
        for key, value, match in self.attributes:
            if isinstance(value, bool): value = "true" if value else "false"
            conditions.append({"key": key, "params": [value, match] if match else value})
        result: dict[str, Any] = {"sel": conditions, "find": find}
        if self.max_depth: result["depth"] = self.max_depth
        if self.max_children != 30: result["children"] = self.max_children
        return result

    def code(self) -> str:
        calls = []
        for key, value, match in self.attributes:
            suffix = ", contains=True" if match == self.CONTAINS else ""
            calls.append(f".{key}({value!r}{suffix})")
        head = "device.selector()"
        if self.mode == "full": head = "device.selector(mode='full')"
        if self.point: head += f".at({self.point[0]!r}, {self.point[1]!r})"
        return head + "".join(calls)


@dataclass
class UiObject:
    """A resolved UI element.

    Element rectangles and actions both use physical screen pixels.
    """

    device: "Device"
    info: Mapping[str, Any]
    selector: Selector

    @property
    def rect(self) -> dict[str, float]:
        info = self.info
        return {"x": float(info.get("x") or 0), "y": float(info.get("y") or 0), "width": float(info.get("width") or 0), "height": float(info.get("height") or 0)}

    @property
    def center(self) -> tuple[float, float]:
        rect = self.rect
        return rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2

    @property
    def exists(self) -> bool: return True

    def click(self) -> Any:
        return self.device.client.tap(*self.center)

    def set_text(self, text: str, *, interval_ms: int = 120) -> Any:
        self.click()
        return self.device.client.input_text(text, interval_ms=interval_ms)


@dataclass
class Device:
    """High-level device facade modelled after uiautomator2's ``Device``."""

    client: "AScriptClient"

    def selector(self, *, mode: str = "smart", **attributes: Any) -> Selector:
        selector = Selector(mode=mode)
        aliases = {"text": "label", "resource_id": "name", "description": "name", "class_name": "type"}
        for key, value in attributes.items():
            selector = selector.with_attr(aliases.get(key, key), value)
        return selector

    def __call__(self, **attributes: Any) -> "UiCollection":
        return UiCollection(self, self.selector(**attributes))

    def find_all(self, selector: Selector) -> list[UiObject]:
        data = self.client.ui_tree(selector=selector.payload(), mode=selector.mode, x=(selector.point or (0, 0))[0], y=(selector.point or (0, 0))[1])
        views = data.get("views") or []
        if not isinstance(views, list):
            raise ValueError("invalid element list returned by device")
        return [UiObject(self, dict(item), selector) for item in views if isinstance(item, Mapping)]

    def find(self, selector: Selector, *, timeout: float = 0, log: bool = False) -> UiObject | None:
        return UiCollection(self, selector).get(timeout=timeout, log=log)

    def wait(self, selector: Selector, *, timeout: float = 10.0, log: bool = False) -> UiObject:
        result = self.find(selector, timeout=timeout, log=log)
        if result is None: raise LookupError(f"element did not appear within {timeout}s: {selector.code()}")
        return result

    def wait_gone(self, selector: Selector, *, timeout: float = 10.0, log: bool = False) -> bool:
        return UiCollection(self, selector).wait_gone(timeout=timeout, log=log)

    def dump_hierarchy(self, *, mode: str = "smart") -> str:
        return self.client.ui_xml(mode=mode)

    def screenshot(self, destination: str | None = None) -> bytes | Any:
        return self.client.save_screenshot(destination) if destination else self.client.screenshot()

    def click_relative(self, x_ratio: float, y_ratio: float, *, duration_ms: int = 20) -> Any:
        """Click a point expressed as fractions of the current screen dimensions."""
        return self.client.tap_relative(x_ratio, y_ratio, duration_ms=duration_ms)

    def click_rel(self, x_ratio: float, y_ratio: float, *, duration_ms: int = 20) -> Any:
        """Short uiautomator2-style alias for :meth:`click_relative`."""
        return self.click_relative(x_ratio, y_ratio, duration_ms=duration_ms)

    def swipe_relative(self, x1_ratio: float, y1_ratio: float, x2_ratio: float, y2_ratio: float, *, duration_ms: int = 200) -> Any:
        """Swipe between two fractional screen coordinates."""
        return self.client.swipe_relative(x1_ratio, y1_ratio, x2_ratio, y2_ratio, duration_ms=duration_ms)

    def scroll_until_image(self, template: str | Path | bytes, **kwargs: Any) -> Any:
        """Scroll upward until a local image template appears."""
        return self.client.scroll_until_image(template, **kwargs)

    def find_image(self, template: str | Path | bytes, **kwargs: Any) -> Any:
        """Find a local image template in the current screenshot."""
        return self.client.find_image(template, **kwargs)

    def wait_image(self, template: str | Path | bytes, **kwargs: Any) -> Any:
        """Wait until a local image template appears."""
        return self.client.wait_image(template, **kwargs)

    def wait_image_gone(self, template: str | Path | bytes, **kwargs: Any) -> bool:
        """Wait until a local image template disappears."""
        return self.client.wait_image_gone(template, **kwargs)

    def tap_image(self, template: str | Path | bytes, **kwargs: Any) -> Any:
        """Wait for a local image template and tap its center."""
        return self.client.tap_image(template, **kwargs)


@dataclass
class UiCollection:
    device: Device
    selector: Selector

    @property
    def exists(self) -> bool: return bool(self.all())
    @property
    def count(self) -> int: return len(self.all())
    @property
    def info(self) -> Mapping[str, Any]:
        item = self.get()
        if item is None: raise LookupError(f"element not found: {self.selector.code()}")
        return item.info

    def all(self) -> list[UiObject]: return self.device.find_all(self.selector)
    def get(self, *, timeout: float = 0, log: bool = False) -> UiObject | None:
        deadline = time.monotonic() + timeout
        attempt = 0
        while True:
            attempt += 1
            found = self.all()
            if found:
                if log: print(t("selector_wait_found", attempt=attempt, selector=self.selector.code()))
                return found[0]
            if log: print(t("selector_wait_missing", attempt=attempt, selector=self.selector.code()))
            if time.monotonic() >= deadline: return None
            time.sleep(min(0.3, deadline - time.monotonic()))
    def wait_gone(self, *, timeout: float = 10.0, log: bool = False) -> bool:
        deadline = time.monotonic() + timeout
        attempt = 0
        while True:
            attempt += 1
            if not self.exists:
                if log: print(t("selector_wait_gone", attempt=attempt, selector=self.selector.code()))
                return True
            if log: print(t("selector_wait_present", attempt=attempt, selector=self.selector.code()))
            if time.monotonic() >= deadline: return False
            time.sleep(min(0.3, deadline - time.monotonic()))
    def click(self) -> Any:
        item = self.get()
        if item is None: raise LookupError(f"element not found: {self.selector.code()}")
        return item.click()
    def click_exists(self, *, timeout: float = 0) -> bool:
        item = self.get(timeout=timeout)
        if item is None: return False
        item.click()
        return True
    def set_text(self, text: str, *, interval_ms: int = 120) -> Any:
        item = self.get()
        if item is None: raise LookupError(f"element not found: {self.selector.code()}")
        return item.set_text(text, interval_ms=interval_ms)
