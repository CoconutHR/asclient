"""Tests for the init command, the single write path for a full config."""
from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from asclient.cli import main
from asclient.i18n import set_language
from asclient.initializer import LOOPBACK_ADDRESS, build_config, is_git_ignored, write_config


@contextmanager
def _workdir():
    """Run inside a throwaway directory, since config paths follow Path.cwd()."""
    previous = Path.cwd()
    with TemporaryDirectory() as directory:
        os.chdir(directory)
        try:
            yield Path(directory)
        finally:
            os.chdir(previous)


def _run(arguments: list[str]) -> tuple[int, str]:
    """Invoke the CLI while capturing output, keeping test logs readable."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(arguments)
    return code, out.getvalue() + err.getvalue()


class BuildConfigTests(unittest.TestCase):
    def test_defaults_to_loopback_and_log_port(self):
        config = build_config()

        self.assertEqual(config["device"]["address"], LOOPBACK_ADDRESS)
        self.assertEqual(config["tunnel"]["local_port"], 9096)
        self.assertEqual(config["tunnel"]["local_log_port"], 10102)
        self.assertEqual(config["tunnel"]["remote_log_port"], 10102)
        self.assertTrue(config["tunnel"]["forward_logs"])

    def test_every_documented_key_is_present(self):
        # JSON has no comments, so a complete file is the only self-documentation.
        config = build_config()

        self.assertEqual(set(config), {"language", "device", "tunnel"})
        self.assertEqual(set(config["device"]), {"address", "password", "timeout", "retries"})
        self.assertEqual(
            set(config["tunnel"]),
            {"iproxy", "local_host", "local_port", "remote_port", "local_log_port", "remote_log_port", "forward_logs", "udid", "startup_timeout"},
        )

    def test_password_and_udid_are_generated_empty(self):
        config = build_config()

        self.assertEqual(config["device"]["password"], "")
        self.assertEqual(config["tunnel"]["udid"], "")

    def test_explicit_address_is_used(self):
        config = build_config(address="192.168.3.25:9096")

        self.assertEqual(config["device"]["address"], "192.168.3.25:9096")

    def test_invalid_address_is_rejected_before_writing(self):
        with self.assertRaises(ValueError):
            build_config(address="1.2.3.4:99999")

    def test_detected_iproxy_is_stored_and_absent_one_falls_back(self):
        self.assertEqual(build_config(iproxy="/usr/bin/iproxy")["tunnel"]["iproxy"], "/usr/bin/iproxy")
        self.assertEqual(build_config(iproxy=None)["tunnel"]["iproxy"], "iproxy")


class WriteConfigTests(unittest.TestCase):
    def test_refuses_to_overwrite_without_force(self):
        with _workdir() as directory:
            target = directory / "asclient.json"
            target.write_text('{"device": {"password": "secret"}}', encoding="utf-8")

            with self.assertRaises(FileExistsError):
                write_config(build_config())

            # 未加 --force 时绝不能丢失已有密码。
            self.assertIn("secret", target.read_text(encoding="utf-8"))

    def test_force_overwrites(self):
        with _workdir() as directory:
            target = directory / "asclient.json"
            target.write_text('{"device": {"password": "secret"}}', encoding="utf-8")

            write_config(build_config(), force=True)

            self.assertNotIn("secret", target.read_text(encoding="utf-8"))

    def test_creates_parent_directories_for_an_explicit_path(self):
        with _workdir() as directory:
            written = write_config(build_config(), "envs/usb.json")

            self.assertTrue(written.is_file())
            self.assertEqual(written.parent.name, "envs")
            self.assertEqual(directory.resolve(), written.parent.parent)


class InitCommandTests(unittest.TestCase):
    def setUp(self):
        set_language("en")
        self.addCleanup(set_language, "auto")

    def test_print_only_writes_nothing(self):
        with _workdir() as directory:
            code, output = _run(["init", "--print"])

            self.assertEqual(code, 0)
            self.assertFalse((directory / "asclient.json").exists())
            self.assertEqual(json.loads(output)["device"]["address"], LOOPBACK_ADDRESS)

    def test_creates_a_loadable_config(self):
        with _workdir() as directory:
            code, _ = _run(["init"])

            self.assertEqual(code, 0)
            saved = json.loads((directory / "asclient.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["device"]["address"], LOOPBACK_ADDRESS)

    def test_second_run_fails_without_force(self):
        with _workdir():
            self.assertEqual(_run(["init"])[0], 0)
            code, output = _run(["init"])
            self.assertEqual(code, 1)
            self.assertIn("--force", output)
            self.assertEqual(_run(["init", "--force"])[0], 0)

    def test_device_option_is_written(self):
        with _workdir() as directory:
            _run(["init", "--device", "192.168.3.25:9096"])

            saved = json.loads((directory / "asclient.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["device"]["address"], "192.168.3.25:9096")

    def test_loopback_hint_is_shown_only_for_the_default_address(self):
        with _workdir():
            _, default_output = _run(["init"])
        with _workdir():
            _, explicit_output = _run(["init", "--device", "192.168.3.25:9096"])

        self.assertIn(LOOPBACK_ADDRESS, default_output)
        self.assertNotIn("was set to", explicit_output)

    def test_init_does_not_contact_a_device(self):
        # init must work before any device exists; it may never build a client.
        with _workdir(), patch("asclient.cli._client") as client:
            _run(["init"])

        client.assert_not_called()


class GitIgnoreDetectionTests(unittest.TestCase):
    def _git_available(self) -> bool:
        try:
            subprocess.run(["git", "--version"], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        return True

    def test_reports_false_inside_a_repository_without_a_rule(self):
        if not self._git_available():
            self.skipTest("git is not available")
        with _workdir() as directory:
            subprocess.run(["git", "init", "-q"], cwd=directory, capture_output=True, timeout=10)

            self.assertIs(is_git_ignored(directory / "asclient.json"), False)

    def test_reports_true_when_a_rule_matches(self):
        if not self._git_available():
            self.skipTest("git is not available")
        with _workdir() as directory:
            subprocess.run(["git", "init", "-q"], cwd=directory, capture_output=True, timeout=10)
            (directory / ".gitignore").write_text("asclient.json\n", encoding="utf-8")

            self.assertIs(is_git_ignored(directory / "asclient.json"), True)

    def test_reports_none_outside_a_repository(self):
        with _workdir() as directory:
            self.assertIsNone(is_git_ignored(directory / "asclient.json"))


if __name__ == "__main__":
    unittest.main()
