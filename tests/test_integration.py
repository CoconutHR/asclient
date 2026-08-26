"""Opt-in integration checks for a reachable AScript device.

Copy ``integration.example.json`` to ``integration.json``, fill the device
values, then explicitly set ``enabled`` to true. The default checks are read
only. Destructive project deployment is deliberately excluded from this file.
"""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from asclient import AScriptClient
from asclient.config import device_options, load_config


CONFIG_PATH = Path(__file__).with_name("integration.json")
CONFIG = load_config(CONFIG_PATH)
OPTIONS = device_options(CONFIG)
ENABLED = bool(CONFIG.get("enabled", False))
ARTIFACT_DIR = Path(str(CONFIG.get("artifacts_dir", "artifacts/integration")))


@unittest.skipUnless(ENABLED and OPTIONS.get("address"), "copy tests/integration.example.json to integration.json and set enabled=true")
class DeviceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = AScriptClient(OPTIONS["address"], password=OPTIONS.get("password", ""), timeout=float(OPTIONS.get("timeout", 20)), retries=int(OPTIONS.get("retries", 1)))
        cls.artifacts = ARTIFACT_DIR / time.strftime("%Y%m%d_%H%M%S")
        cls.artifacts.mkdir(parents=True, exist_ok=True)

    def test_01_connectivity_and_status(self):
        platform = self.client.ping()
        status = self.client.status()
        (self.artifacts / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        self.assertIn(platform, {"iOS", "Android"})
        self.assertTrue(status.get("available", True))

    def test_02_screenshot_and_tree(self):
        image = self.client.screenshot()
        xml = self.client.ui_xml()
        (self.artifacts / "screen.png").write_bytes(image)
        (self.artifacts / "tree.xml").write_text(xml, encoding="utf-8")
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"), "device did not return a PNG screenshot")
        self.assertIn("<", xml)

    def test_03_log_socket(self):
        # Empty output is valid on an idle device; establishing and closing the
        # WebSocket without a protocol error verifies the log service.
        entries = list(self.client.logs(duration=0.3))
        (self.artifacts / "log-probe.jsonl").write_text("".join(json.dumps(entry.__dict__, ensure_ascii=False) + "\n" for entry in entries), encoding="utf-8")

    def test_04_expected_selector(self):
        selector = CONFIG.get("expected_selector")
        if not isinstance(selector, dict): self.skipTest("set expected_selector in tests/integration.json for target-App validation")
        elements = self.client.find_elements(selector)
        (self.artifacts / "selector-result.json").write_text(json.dumps(elements, ensure_ascii=False, indent=2), encoding="utf-8")
        self.assertEqual(len(elements), 1, "expected selector must match exactly one element")
