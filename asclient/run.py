"""Evidence-producing execution context for production automation flows."""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING, TypeVar

from .client import AScriptClient

if TYPE_CHECKING:
    from .automation import Device, Selector, UiObject


T = TypeVar("T")


def _label(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return result or "step"


@dataclass
class Run:
    """Serialize one device workflow and preserve inspectable execution evidence.

    Failed steps always capture screenshot, XML and device context. Successful
    steps can request before/after evidence explicitly when diagnosing a flow.
    """

    device: "Device | AScriptClient"
    artifacts_root: str | Path = "artifacts"
    run_id: str | None = None
    directory: Path = field(init=False)
    manifest: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        self.client = self.device.client if hasattr(self.device, "client") else self.device
        if not isinstance(self.client, AScriptClient): raise TypeError("device must be Device or AScriptClient")
        self.run_id = self.run_id or time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        self.directory = Path(self.artifacts_root) / self.run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.manifest = {"run_id": self.run_id, "device": str(self.client.address), "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "steps": []}
        self._write_manifest()

    def __enter__(self) -> "Run": return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.manifest["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.manifest["outcome"] = "failed" if exc_type else "passed"
        if exc_type: self.manifest["exception"] = f"{getattr(exc_type, '__name__', exc_type)}: {exc}"
        self._write_manifest()
        return False

    def _write_manifest(self) -> None:
        (self.directory / "manifest.json").write_text(json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def capture(self, label: str, *, mode: str = "smart") -> dict[str, Path]:
        return self.client.capture_artifacts(self.directory, prefix=_label(label), mode=mode)

    def step(self, name: str, action: Callable[[], T], *, capture_before: bool = False, capture_after: bool = False) -> T:
        """Run one action under the device lock and append its result to manifest."""
        label = _label(name)
        record: dict[str, Any] = {"name": name, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        self.manifest["steps"].append(record)
        started = time.monotonic()
        try:
            with self.client.locked():
                if capture_before: record["before"] = {key: str(path) for key, path in self.capture(f"{label}_before").items()}
                result = action()
                if capture_after: record["after"] = {key: str(path) for key, path in self.capture(f"{label}_after").items()}
            record["outcome"] = "passed"
            return result
        except Exception as exc:
            record["outcome"] = "failed"; record["error"] = f"{type(exc).__name__}: {exc}"
            try: record["failure"] = {key: str(path) for key, path in self.capture(f"{label}_failure").items()}
            except Exception as evidence_error: record["evidence_error"] = f"{type(evidence_error).__name__}: {evidence_error}"
            raise
        finally:
            record["duration_seconds"] = round(time.monotonic() - started, 3)
            record["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            self._write_manifest()

    def wait(self, selector: "Selector", *, timeout: float = 10.0, name: str = "wait", log: bool = False) -> "UiObject":
        if not hasattr(self.device, "wait"): raise TypeError("wait requires a Device, not AScriptClient")
        return self.step(name, lambda: self.device.wait(selector, timeout=timeout, log=log))

    def assert_unique(self, selector: "Selector", *, name: str = "assert_unique") -> "UiObject":
        if not callable(getattr(self.device, "find_all", None)): raise TypeError("assert_unique requires a Device, not AScriptClient")
        def check() -> "UiObject":
            elements = self.device.find_all(selector)
            if len(elements) != 1: raise AssertionError(f"selector matched {len(elements)} elements: {selector.code()}")
            return elements[0]
        return self.step(name, check)
