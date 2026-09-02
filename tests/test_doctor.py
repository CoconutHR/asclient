"""Tests for the read-only diagnostics and the explicit config repair path."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from asclient import AScriptClient
from asclient.doctor import diagnose, set_iproxy_path
from asclient.errors import AScriptError
from asclient.i18n import set_language


class DoctorPortTests(unittest.TestCase):
    def setUp(self):
        set_language("en")
        self.addCleanup(set_language, "auto")

    def _diagnose(self, config, *, address="192.168.3.17:9096"):
        """Run diagnose with every outbound probe stubbed out."""
        client = AScriptClient(address, timeout=1.0, retries=0)
        with (
            patch("asclient.doctor._executable_path", return_value="/usr/bin/iproxy"),
            patch("asclient.doctor._port_available", return_value=True),
            patch("asclient.doctor.socket.create_connection") as connection,
            patch.object(AScriptClient, "ping", return_value="iOS"),
            patch.object(AScriptClient, "status", return_value={}),
        ):
            checks = diagnose(client, config)
        return checks, connection

    def test_log_probe_uses_configured_remote_log_port(self):
        config = {"tunnel": {"remote_log_port": 11002}}
        checks, connection = self._diagnose(config)

        # 回归保护：此前该探测硬编码 10102，自定义端口会误报。
        connection.assert_called_once()
        host, port = connection.call_args.args[0]
        self.assertEqual(port, 11002)
        log_check = next(check for check in checks if check.name == "log_service")
        self.assertEqual(log_check.status, "ok")
        self.assertIn("11002", log_check.message)
        self.assertNotIn("10102", log_check.message)

    def test_log_probe_defaults_to_10102(self):
        checks, connection = self._diagnose({})

        host, port = connection.call_args.args[0]
        self.assertEqual(port, 10102)
        log_check = next(check for check in checks if check.name == "log_service")
        self.assertIn("10102", log_check.message)

    def test_log_probe_failure_reports_configured_port(self):
        client = AScriptClient("192.168.3.17:9096", timeout=1.0, retries=0)
        with (
            patch("asclient.doctor._executable_path", return_value="/usr/bin/iproxy"),
            patch("asclient.doctor._port_available", return_value=True),
            patch("asclient.doctor.socket.create_connection", side_effect=OSError("refused")),
            patch.object(AScriptClient, "ping", return_value="iOS"),
            patch.object(AScriptClient, "status", return_value={}),
        ):
            checks = diagnose(client, {"tunnel": {"remote_log_port": 11002}})

        log_check = next(check for check in checks if check.name == "log_service")
        self.assertEqual(log_check.status, "warning")
        self.assertIn("11002", log_check.message)

    def test_missing_iproxy_is_error_on_loopback_and_warning_on_wifi(self):
        for address, expected in (("127.0.0.1:9096", "error"), ("192.168.3.17:9096", "warning")):
            with self.subTest(address=address):
                client = AScriptClient(address, timeout=1.0, retries=0)
                with (
                    patch("asclient.doctor._executable_path", return_value=None),
                    patch("asclient.doctor._port_available", return_value=True),
                    patch("asclient.doctor.socket.create_connection"),
                    patch.object(AScriptClient, "ping", return_value="iOS"),
                    patch.object(AScriptClient, "status", return_value={}),
                ):
                    checks = diagnose(client, {})
                iproxy = next(check for check in checks if check.name == "iproxy")
                self.assertEqual(iproxy.status, expected)

    def test_unreachable_device_reports_error_without_raising(self):
        client = AScriptClient("192.168.3.17:9096", timeout=1.0, retries=0)
        with (
            patch("asclient.doctor._executable_path", return_value="/usr/bin/iproxy"),
            patch("asclient.doctor._port_available", return_value=True),
            patch("asclient.doctor.socket.create_connection"),
            patch.object(AScriptClient, "ping", side_effect=AScriptError("HTTP 502")),
        ):
            checks = diagnose(client, {})

        device = next(check for check in checks if check.name == "device")
        self.assertEqual(device.status, "error")


class DoctorRepairTests(unittest.TestCase):
    def setUp(self):
        set_language("en")
        self.addCleanup(set_language, "auto")

    def test_fix_preserves_unrelated_config_keys(self):
        original = {
            "language": "zh-CN",
            "device": {"address": "192.168.3.25:9096", "password": "secret"},
            "tunnel": {"udid": "ABC123", "local_port": 19096},
        }
        with TemporaryDirectory() as directory:
            executable = Path(directory) / "iproxy"
            executable.write_text("", encoding="utf-8")
            target = Path(directory) / "asclient.json"

            set_iproxy_path(original, str(executable), path=target)
            saved = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(saved["language"], "zh-CN")
        self.assertEqual(saved["device"]["password"], "secret")
        self.assertEqual(saved["tunnel"]["udid"], "ABC123")
        self.assertEqual(saved["tunnel"]["local_port"], 19096)
        self.assertEqual(saved["tunnel"]["iproxy"], str(executable.resolve()))

    def test_fix_rejects_a_path_that_is_not_a_file(self):
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "absent"
            with self.assertRaises(ValueError):
                set_iproxy_path({}, str(missing), path=Path(directory) / "asclient.json")

    def test_fix_creates_a_minimal_config_when_none_exists(self):
        with TemporaryDirectory() as directory:
            executable = Path(directory) / "iproxy"
            executable.write_text("", encoding="utf-8")
            target = Path(directory) / "asclient.json"

            set_iproxy_path({}, str(executable), path=target)
            saved = json.loads(target.read_text(encoding="utf-8"))

        # 新建时只写 tunnel.iproxy；device 段仍需用户自行填写。
        self.assertEqual(list(saved), ["tunnel"])
        self.assertNotIn("device", saved)


if __name__ == "__main__":
    unittest.main()
