"""Tests for thread and process coordination of device locks."""
from __future__ import annotations

import errno
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from asclient import AScriptClient, connect
from asclient.runtime import _acquire_file_lock, _lock_path, device_lock, normalize_lock_id


class DeviceLockTests(unittest.TestCase):
    def test_windows_file_lock_retries_only_for_lock_contention(self):
        locking = Mock(side_effect=[OSError(errno.EACCES, "locked"), None])
        msvcrt = SimpleNamespace(LK_NBLCK=1, locking=locking)
        handle = Mock()
        handle.fileno.return_value = 42

        with (
            patch("asclient.runtime.os.name", "nt"),
            patch.dict(sys.modules, {"msvcrt": msvcrt}),
            patch("asclient.runtime.time.sleep") as sleep,
        ):
            _acquire_file_lock(handle)

        handle.seek.assert_called_once_with(0)
        self.assertEqual(locking.call_count, 2)
        sleep.assert_called_once_with(0.05)

    def test_windows_file_lock_raises_non_contention_errors(self):
        error = OSError(errno.EBADF, "bad file descriptor")
        locking = Mock(side_effect=error)
        msvcrt = SimpleNamespace(LK_NBLCK=1, locking=locking)
        handle = Mock()
        handle.fileno.return_value = 42

        with (
            patch("asclient.runtime.os.name", "nt"),
            patch.dict(sys.modules, {"msvcrt": msvcrt}),
            patch("asclient.runtime.time.sleep") as sleep,
            self.assertRaisesRegex(OSError, "bad file descriptor"),
        ):
            _acquire_file_lock(handle)

        locking.assert_called_once_with(42, 1, 1)
        sleep.assert_not_called()

    def test_normalizes_default_id_and_uses_stable_hash_path(self):
        key = normalize_lock_id(" HTTP://Example.TEST:9096/ ")
        self.assertEqual(key, "example.test:9096")
        self.assertEqual(_lock_path(key), _lock_path(key))
        self.assertEqual(len(_lock_path(key).stem), 64)

    def test_nested_lock_is_reentrant_and_releases_after_exception(self):
        with self.assertRaisesRegex(RuntimeError, "expected"):
            with device_lock("nested-device"):
                with device_lock("nested-device"):
                    raise RuntimeError("expected")

        acquired = threading.Event()

        def take_lock():
            with device_lock("nested-device"):
                acquired.set()

        thread = threading.Thread(target=take_lock)
        thread.start()
        thread.join(1)
        self.assertTrue(acquired.is_set())
        self.assertFalse(thread.is_alive())

    def test_threads_serialize_same_device(self):
        entered = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()

        def first():
            with device_lock("thread-device"):
                entered.set()
                release.wait(1)

        def second():
            with device_lock("thread-device"):
                second_entered.set()

        first_thread = threading.Thread(target=first)
        second_thread = threading.Thread(target=second)
        first_thread.start()
        self.assertTrue(entered.wait(1))
        second_thread.start()
        time.sleep(0.1)
        self.assertFalse(second_entered.is_set())
        release.set()
        first_thread.join(1)
        second_thread.join(1)
        self.assertTrue(second_entered.is_set())
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())

    def test_subprocess_waits_for_file_lock(self):
        address = "subprocess-device"
        code = (
            "from asclient.runtime import device_lock\n"
            f"with device_lock({address!r}):\n"
            " print('acquired', flush=True)\n"
        )
        with device_lock(address):
            process = subprocess.Popen(
                [sys.executable, "-c", code],
                cwd=Path(__file__).parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.2)
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                self.fail(f"subprocess exited early: stdout={stdout!r}, stderr={stderr!r}")
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), "acquired")

    def test_client_lock_id_wiring(self):
        client = AScriptClient("Example.TEST", lock_id="device-123")
        self.assertEqual(client.lock_id, "device-123")
        with client.locked():
            with AScriptClient("other-host", lock_id="device-123").locked():
                pass
        self.assertEqual(connect("example.test", lock_id="device-456").client.lock_id, "device-456")
