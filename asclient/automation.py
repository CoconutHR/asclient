"""uiautomator2-inspired automation primitives for AScript iOS devices."""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, TYPE_CHECKING

from .i18n import t

if TYPE_CHECKING:
    from .client import AScriptClient


@dataclass(frozen=True)
class Selector:
    """不可变 AScript 控件选择器。"""

    attributes: tuple[tuple[str, Any, int], ...] = ()
    mode: str = "smart"
    point: tuple[float, float] | None = None
    max_depth: int = 0
    max_children: int = 30

    EQUAL = 0
    CONTAINS = 1
    MATCHES = 2
    _FIELDS = frozenset({"name", "label", "value", "title", "type", "enabled", "selected", "focused", "visible", "index", "traits", "childCount"})

    def with_attr(self, key: str, value: Any, *, match: int = EQUAL) -> "Selector":
        if key not in self._FIELDS:
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
    def at_relative(self, x_ratio: float, y_ratio: float) -> "Selector": return Selector(self.attributes, "point_relative", (x_ratio, y_ratio), self.max_depth, self.max_children)
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


def _matches(info: Mapping[str, Any], selector: Selector) -> bool:
    for key, expected, match in selector.attributes:
        actual = info.get(key)
        if match == Selector.CONTAINS:
            if str(expected) not in str(actual or ""):
                return False
        elif actual != expected:
            return False
    return True


@dataclass
class UiObject:
    """A resolved UI element whose coordinates are physical screenshot pixels."""

    device: "Device"
    info: Mapping[str, Any]
    selector: Selector

    @property
    def rect(self) -> dict[str, float]:
        return {"x": float(self.info.get("x") or 0), "y": float(self.info.get("y") or 0), "width": float(self.info.get("width") or 0), "height": float(self.info.get("height") or 0)}

    @property
    def center(self) -> tuple[float, float]:
        rect = self.rect
        return rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2

    @property
    def exists(self) -> bool: return True

    def click(self) -> Any:
        rect = self.rect
        if rect["width"] <= 0 or rect["height"] <= 0: raise ValueError("element has an empty rectangle")
        return self.device.client.tap(*self.center)
    def click_relative(self, x_ratio: float, y_ratio: float) -> Any:
        try: x_ratio, y_ratio = float(x_ratio), float(y_ratio)
        except (TypeError, ValueError) as exc: raise ValueError("element relative coordinates must be finite numbers between 0 and 1") from exc
        if not all(math.isfinite(value) and 0 <= value <= 1 for value in (x_ratio, y_ratio)):
            raise ValueError("element relative coordinates must be finite numbers between 0 and 1")
        rect = self.rect
        if rect["width"] <= 0 or rect["height"] <= 0: raise ValueError("element has an empty rectangle")
        x = min(rect["x"] + rect["width"] - 1, rect["x"] + rect["width"] * x_ratio)
        y = min(rect["y"] + rect["height"] - 1, rect["y"] + rect["height"] * y_ratio)
        return self.device.client.tap(x, y)
    def set_text(self, text: str, *, interval_ms: int = 120) -> Any:
        self.click()
        return self.device.client.input_text(text, interval_ms=interval_ms)
    def screenshot(self, destination: str | Path | None = None) -> bytes | Path:
        frame = self.device.client.capture_frame()
        rect = self.rect
        left, top = max(0, int(rect["x"])), max(0, int(rect["y"]))
        right, bottom = min(frame.width, int(rect["x"] + rect["width"])), min(frame.height, int(rect["y"] + rect["height"]))
        if left >= right or top >= bottom: raise ValueError("element has an empty rectangle")
        from PIL import Image
        from io import BytesIO
        with Image.open(BytesIO(frame.png)) as image:
            output = BytesIO(); image.crop((left, top, right, bottom)).save(output, "PNG")
        data = output.getvalue()
        if destination is None: return data
        target = Path(destination); target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
        return target.resolve()


@dataclass(frozen=True)
class SnapshotNode:
    snapshot: "UiSnapshot"
    index: int

    @property
    def info(self) -> Mapping[str, Any]: return self.snapshot._nodes[self.index]
    @property
    def object(self) -> UiObject: return UiObject(self.snapshot.device, self.info, Selector(mode=self.snapshot.mode))
    @property
    def rect(self) -> dict[str, float]: return self.object.rect
    @property
    def center(self) -> tuple[float, float]: return self.object.center
    def click(self) -> Any: return self.object.click()
    def set_text(self, text: str, *, interval_ms: int = 120) -> Any: return self.object.set_text(text, interval_ms=interval_ms)


class SnapshotCollection:
    """Locally queried nodes from one immutable ``UiSnapshot``."""

    def __init__(self, snapshot: "UiSnapshot", indices: tuple[int, ...]):
        self.snapshot, self.indices = snapshot, indices

    @property
    def exists(self) -> bool: return bool(self.indices)
    @property
    def count(self) -> int: return len(self.indices)
    @property
    def info(self) -> Mapping[str, Any]:
        if not self.indices: raise LookupError("element not found in snapshot")
        return self.snapshot._nodes[self.indices[0]]
    def all(self) -> list[SnapshotNode]: return [SnapshotNode(self.snapshot, index) for index in self.indices]
    def get(self) -> SnapshotNode | None: return self.all()[0] if self.indices else None
    def where_regex(self, field: str, pattern: str) -> "SnapshotCollection":
        if field not in {"name", "label", "value", "title", "type", "enabled", "selected", "focused", "visible", "index", "traits", "childCount"}:
            raise ValueError(f"unsupported selector attribute: {field}")
        matcher = re.compile(pattern).search
        return SnapshotCollection(self.snapshot, tuple(index for index in self.indices if matcher(str(self.snapshot._nodes[index].get(field) or ""))))
    def child(self, selector: Selector | None = None) -> "SnapshotCollection":
        indices = tuple(child for index in self.indices for child in self.snapshot._children[index])
        return self.snapshot._filter(indices, selector)
    def descendant(self, selector: Selector | None = None) -> "SnapshotCollection":
        result: list[int] = []
        def walk(index: int) -> None:
            for child in self.snapshot._children[index]:
                result.append(child); walk(child)
        for index in self.indices: walk(index)
        return self.snapshot._filter(tuple(result), selector)
    def parent(self, selector: Selector | None = None) -> "SnapshotCollection":
        return self.snapshot._filter(tuple(index for item in self.indices if (index := self.snapshot._parents[item]) is not None), selector)
    def sibling(self, selector: Selector | None = None) -> "SnapshotCollection":
        result: list[int] = []
        for index in self.indices:
            parent = self.snapshot._parents[index]
            if parent is not None: result.extend(item for item in self.snapshot._children[parent] if item != index)
        return self.snapshot._filter(tuple(dict.fromkeys(result)), selector)


class UiSnapshot:
    """One full UI tree queried locally without further device requests."""

    def __init__(self, device: "Device", tree: Mapping[str, Any], *, mode: str):
        self.device, self.tree, self.mode = device, dict(tree), mode
        self._nodes: list[dict[str, Any]] = []
        self._parents: list[int | None] = []
        self._children: list[tuple[int, ...]] = []
        self._exact_index: dict[str, dict[Any, tuple[int, ...]]] = {}
        def visit(node: Mapping[str, Any], parent: int | None) -> int:
            index = len(self._nodes); info = {key: value for key, value in node.items() if key != "childs"}
            self._nodes.append(info); self._parents.append(parent); self._children.append(())
            children = tuple(visit(child, index) for child in (node.get("childs") or []) if isinstance(child, Mapping))
            self._children[index] = children
            return index
        for root in self.tree.get("views") or []:
            if isinstance(root, Mapping): visit(root, None)
        mutable_index: dict[str, dict[Any, list[int]]] = {}
        for index, node in enumerate(self._nodes):
            for field, value in node.items():
                if field not in Selector._FIELDS or not isinstance(value, (str, int, float, bool)):
                    continue
                mutable_index.setdefault(field, {}).setdefault(value, []).append(index)
        self._exact_index = {field: {value: tuple(indices) for value, indices in values.items()} for field, values in mutable_index.items()}

    def _filter(self, indices: tuple[int, ...], selector: Selector | None) -> SnapshotCollection:
        return SnapshotCollection(self, tuple(index for index in indices if selector is None or _matches(self._nodes[index], selector)))
    def __call__(self, **attributes: Any) -> SnapshotCollection: return self.select(self.device.selector(**attributes))
    def select(self, selector: Selector) -> SnapshotCollection:
        if selector.point is not None:
            if selector.mode == "point_relative":
                x, y = self.device.client.relative_point(*selector.point)
            else:
                x, y = selector.point
            candidates = tuple(sorted((index for index, node in enumerate(self._nodes) if float(node.get("width") or 0) > 0 and float(node.get("height") or 0) > 0 and float(node.get("x") or 0) <= x < float(node.get("x") or 0) + float(node.get("width") or 0) and float(node.get("y") or 0) <= y < float(node.get("y") or 0) + float(node.get("height") or 0)), key=lambda index: (float(self._nodes[index].get("width") or 0) * float(self._nodes[index].get("height") or 0), index)))
        else:
            exact_lists = [self._exact_index.get(field, {}).get(value, ()) for field, value, match in selector.attributes if match == Selector.EQUAL]
            if exact_lists and any(not values for values in exact_lists):
                candidates = ()
            elif exact_lists:
                allowed = set(min(exact_lists, key=len))
                for values in exact_lists:
                    allowed.intersection_update(values)
                candidates = tuple(index for index in range(len(self._nodes)) if index in allowed)
            else:
                candidates = tuple(range(len(self._nodes)))
        return self._filter(candidates, selector)
    def roots(self) -> SnapshotCollection: return SnapshotCollection(self, tuple(index for index, parent in enumerate(self._parents) if parent is None))


@dataclass
class Device:
    """类似 uiautomator2 的高层设备入口。"""

    client: "AScriptClient"

    def selector(self, *, mode: str = "smart", **attributes: Any) -> Selector:
        selector = Selector(mode=mode)
        aliases = {"text": "label", "resource_id": "name", "description": "name", "class_name": "type"}
        for key, value in attributes.items(): selector = selector.with_attr(aliases.get(key, key), value)
        return selector
    def __call__(self, **attributes: Any) -> "UiCollection": return UiCollection(self, self.selector(**attributes))
    def snapshot(self, *, mode: str = "full") -> UiSnapshot: return UiSnapshot(self, self.client.ui_tree(mode=mode), mode=mode)
    def find_all(self, selector: Selector) -> list[UiObject]:
        data = self.client.ui_tree(selector=selector.payload(), mode=selector.mode, x=(selector.point or (0, 0))[0], y=(selector.point or (0, 0))[1])
        views = data.get("views") or []
        if not isinstance(views, list): raise ValueError("invalid element list returned by device")
        return [UiObject(self, dict(item), selector) for item in views if isinstance(item, Mapping)]
    def find(self, selector: Selector, *, timeout: float = 0, interval: float = 0.3, log: bool = False) -> UiObject | None: return UiCollection(self, selector).get(timeout=timeout, interval=interval, log=log)
    def wait(self, selector: Selector, *, timeout: float = 10.0, interval: float = 0.3, log: bool = False) -> UiObject:
        result = self.find(selector, timeout=timeout, interval=interval, log=log)
        if result is None: raise LookupError(f"element did not appear within {timeout}s: {selector.code()}")
        return result
    def wait_any(self, selectors: Mapping[str, Selector], *, timeout: float = 10.0, interval: float = 0.3, log: bool = False) -> tuple[str, UiObject]:
        if not selectors: raise ValueError("selectors must not be empty")
        if timeout < 0 or interval <= 0: raise ValueError("timeout must be non-negative and interval must be positive")
        deadline = time.monotonic() + timeout
        while True:
            snapshot = self.snapshot(mode="full")
            for name, selector in selectors.items():
                found = snapshot.select(selector).get()
                if found: return name, found.object
            if time.monotonic() >= deadline: raise LookupError(f"none of the elements appeared within {timeout}s: {', '.join(selectors)}")
            time.sleep(min(interval, deadline - time.monotonic()))
    def wait_gone(self, selector: Selector, *, timeout: float = 10.0, interval: float = 0.3, log: bool = False) -> bool: return UiCollection(self, selector).wait_gone(timeout=timeout, interval=interval, log=log)
    def dump_hierarchy(self, *, mode: str = "smart") -> str: return self.client.ui_xml(mode=mode)
    def screenshot(self, destination: str | None = None) -> bytes | Any: return self.client.save_screenshot(destination) if destination else self.client.screenshot()
    def screenshot_crop(self, left: int, top: int, right: int, bottom: int) -> bytes: return self.client.screenshot_crop(left, top, right, bottom)
    def screenshot_crop_relative(self, left: float, top: float, right: float, bottom: float) -> bytes: return self.client.screenshot_crop_relative(left, top, right, bottom)
    def save_screenshot_crop(self, destination: str | Path, left: int, top: int, right: int, bottom: int) -> Path: return self.client.save_screenshot_crop(destination, left, top, right, bottom)
    def save_screenshot_crop_relative(self, destination: str | Path, left: float, top: float, right: float, bottom: float) -> Path: return self.client.save_screenshot_crop_relative(destination, left, top, right, bottom)
    def tap(self, x: float, y: float, *, duration_ms: int = 20) -> Any: return self.client.tap(x, y, duration_ms=duration_ms)
    def click_relative(self, x_ratio: float, y_ratio: float, *, duration_ms: int = 20) -> Any: return self.client.tap_relative(x_ratio, y_ratio, duration_ms=duration_ms)
    def click_rel(self, x_ratio: float, y_ratio: float, *, duration_ms: int = 20) -> Any: return self.click_relative(x_ratio, y_ratio, duration_ms=duration_ms)
    def swipe(self, x1: float, y1: float, x2: float, y2: float, *, duration_ms: int = 200) -> Any: return self.client.swipe(x1, y1, x2, y2, duration_ms=duration_ms)
    def swipe_relative(self, x1_ratio: float, y1_ratio: float, x2_ratio: float, y2_ratio: float, *, duration_ms: int = 200) -> Any: return self.client.swipe_relative(x1_ratio, y1_ratio, x2_ratio, y2_ratio, duration_ms=duration_ms)
    def capture_frame(self) -> Any: return self.client.capture_frame()
    def pixel(self, x: int, y: int) -> Any: return self.client.pixel(x, y)
    def pixel_relative(self, x_ratio: float, y_ratio: float) -> Any: return self.client.pixel_relative(x_ratio, y_ratio)
    def pixels(self, points: Any) -> Any: return self.client.pixels(points)
    def pixels_relative(self, points: Any) -> Any: return self.client.pixels_relative(points)
    def scroll_until_image(self, template: str | Path | bytes, **kwargs: Any) -> Any: return self.client.scroll_until_image(template, **kwargs)
    def find_image(self, template: str | Path | bytes, **kwargs: Any) -> Any: return self.client.find_image(template, **kwargs)
    def find_images(self, templates: Mapping[str, str | Path | bytes], **kwargs: Any) -> Any: return self.client.find_images(templates, **kwargs)
    def find_any_image(self, templates: Mapping[str, str | Path | bytes], **kwargs: Any) -> Any: return self.client.find_any_image(templates, **kwargs)
    def wait_image(self, template: str | Path | bytes, **kwargs: Any) -> Any: return self.client.wait_image(template, **kwargs)
    def wait_any_image(self, templates: Mapping[str, str | Path | bytes], **kwargs: Any) -> Any: return self.client.wait_any_image(templates, **kwargs)
    def wait_image_gone(self, template: str | Path | bytes, **kwargs: Any) -> bool: return self.client.wait_image_gone(template, **kwargs)
    def tap_image(self, template: str | Path | bytes, **kwargs: Any) -> Any: return self.client.tap_image(template, **kwargs)


@dataclass
class UiCollection:
    """一个选择器对应的延迟查询控件集合。"""
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
    def snapshot(self, *, mode: str = "full") -> SnapshotCollection: return self.device.snapshot(mode=mode).select(self.selector)
    def get(self, *, timeout: float = 0, interval: float = 0.3, log: bool = False) -> UiObject | None:
        if timeout < 0 or interval <= 0: raise ValueError("timeout must be non-negative and interval must be positive")
        deadline = time.monotonic() + timeout; attempt = 0
        while True:
            attempt += 1; found = self.all()
            if found:
                if log: print(t("selector_wait_found", attempt=attempt, selector=self.selector.code()))
                return found[0]
            if log: print(t("selector_wait_missing", attempt=attempt, selector=self.selector.code()))
            if time.monotonic() >= deadline: return None
            time.sleep(min(interval, deadline - time.monotonic()))
    def wait_gone(self, *, timeout: float = 10.0, interval: float = 0.3, log: bool = False) -> bool:
        if timeout < 0 or interval <= 0: raise ValueError("timeout must be non-negative and interval must be positive")
        deadline = time.monotonic() + timeout; attempt = 0
        while True:
            attempt += 1
            if not self.exists:
                if log: print(t("selector_wait_gone", attempt=attempt, selector=self.selector.code()))
                return True
            if log: print(t("selector_wait_present", attempt=attempt, selector=self.selector.code()))
            if time.monotonic() >= deadline: return False
            time.sleep(min(interval, deadline - time.monotonic()))
    def click(self) -> Any:
        item = self.get()
        if item is None: raise LookupError(f"element not found: {self.selector.code()}")
        return item.click()
    def click_exists(self, *, timeout: float = 0) -> bool:
        item = self.get(timeout=timeout)
        if item is None: return False
        item.click(); return True
    def set_text(self, text: str, *, interval_ms: int = 120) -> Any:
        item = self.get()
        if item is None: raise LookupError(f"element not found: {self.selector.code()}")
        return item.set_text(text, interval_ms=interval_ms)
