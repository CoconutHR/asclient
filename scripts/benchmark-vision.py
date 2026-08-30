"""Benchmark local template matching against one frozen device screenshot."""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from PIL import Image, ImageOps, __version__ as pillow_version

import asclient
from asclient import AScriptClient, ScreenFrame


TEMPLATE_SIZES = ((32, 32), (100, 50), (300, 200))


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


def _measure(operation, warmups: int, repeats: int) -> tuple[Any, dict[str, Any]]:
    result = None
    for _ in range(warmups):
        result = operation()
    samples = []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        result = operation()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return result, {
        "min_ms": round(min(samples), 3),
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(_percentile(samples, 0.95), 3),
        "max_ms": round(max(samples), 3),
        "mean_ms": round(statistics.fmean(samples), 3),
        "samples_ms": [round(value, 3) for value in samples],
    }


def _anchor(frame: ScreenFrame, width: int, height: int) -> tuple[int, int]:
    return max(0, (frame.width - width) // 2), max(0, (frame.height - height) // 2)


def _region(frame: ScreenFrame, left: int, top: int, width: int, height: int) -> tuple[int, int, int, int]:
    margin_x, margin_y = width * 2, height * 2
    return (
        max(0, left - margin_x),
        max(0, top - margin_y),
        min(frame.width, left + width + margin_x),
        min(frame.height, top + height + margin_y),
    )


def run(address: str, *, password: str, timeout: float, warmups: int, repeats: int) -> dict[str, Any]:
    client = AScriptClient(address, password=password, timeout=timeout, retries=0)

    started = time.perf_counter_ns()
    png = client.screenshot()
    capture_ms = (time.perf_counter_ns() - started) / 1_000_000
    started = time.perf_counter_ns()
    frame = ScreenFrame(png)
    decode_ms = (time.perf_counter_ns() - started) / 1_000_000

    cases = []
    for width, height in TEMPLATE_SIZES:
        if width > frame.width or height > frame.height:
            continue
        left, top = _anchor(frame, width, height)
        template = frame.crop_pixels(left, top, left + width, top + height)
        with Image.open(BytesIO(template)) as source:
            inverted_image = ImageOps.invert(source.convert("RGB"))
            inverted_output = BytesIO()
            inverted_image.save(inverted_output, "PNG")
            inverted = inverted_output.getvalue()
        bounded_region = _region(frame, left, top, width, height)
        exact_region = (left, top, left + width, top + height)
        for name, region, confidence, candidate in (
            ("exact_full", None, 1.0, template),
            ("exact_region", bounded_region, 1.0, template),
            ("default_region", bounded_region, 0.9, template),
            ("default_single_candidate_miss", exact_region, 0.9, inverted),
        ):
            match, metrics = _measure(
                lambda candidate=candidate, region=region, confidence=confidence: frame.find_image(
                    candidate, confidence=confidence, region=region
                ),
                warmups,
                repeats,
            )
            cases.append({
                "name": name,
                "confidence": confidence,
                "template_size": [width, height],
                "region": list(region) if region is not None else None,
                "match": [match.x, match.y] if match is not None else None,
                **metrics,
            })

    return {
        "address": address,
        "screen_size": [frame.width, frame.height],
        "capture_ms": round(capture_ms, 3),
        "decode_ms": round(decode_ms, 3),
        "warmups": warmups,
        "repeats": repeats,
        "environment": {
            "python": platform.python_version(),
            "pillow": pillow_version,
            "platform": platform.platform(),
            "processor": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", ""),
            "git_sha": _git_sha(),
            "asclient_module": str(Path(asclient.__file__).resolve()),
        },
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="192.168.3.17:9096")
    parser.add_argument("--password", default="")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=15)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.warmups < 0 or args.repeats <= 0:
        parser.error("warmups must be non-negative and repeats must be positive")

    result = run(
        args.address,
        password=args.password,
        timeout=args.timeout,
        warmups=args.warmups,
        repeats=args.repeats,
    )
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
