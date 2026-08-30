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

from asclient import AScriptClient, DeviceLockTimeoutError, connect
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

    def test_windows_file_lock_times_out_after_contention(self):
        locking = Mock(side_effect=OSError(errno.EACCES, "locked"))
        msvcrt = SimpleNamespace(LK_NBLCK=1, locking=locking)
        handle = Mock()
        handle.fileno.return_value = 42

        with (
            patch("asclient.runtime.os.name", "nt"),
            patch.dict(sys.modules, {"msvcrt": msvcrt}),
            patch("asclient.runtime.time.monotonic", side_effect=[0.0, 0.0, 0.1]),
            patch("asclient.runtime.time.sleep") as sleep,
            self.assertRaisesRegex(DeviceLockTimeoutError, "windows-device"),
        ):
            _acquire_file_lock(handle, deadline=0.1, lock_id="windows-device")

        self.assertEqual(locking.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(0.05)

    def test_unix_file_lock_retries_nonblocking_and_times_out(self):
        flock = Mock(side_effect=OSError(errno.EAGAIN, "locked"))
        fcntl = SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=flock)
        handle = Mock()
        handle.fileno.return_value = 42

        with (
            patch("asclient.runtime.os.name", "posix"),
            patch.dict(sys.modules, {"fcntl": fcntl}),
            patch("asclient.runtime.time.monotonic", side_effect=[0.0, 0.0, 0.1]),
            patch("asclient.runtime.time.sleep") as sleep,
            self.assertRaisesRegex(DeviceLockTimeoutError, "unix-device"),
        ):
            _acquire_file_lock(handle, deadline=0.1, lock_id="unix-device")

        self.assertEqual(flock.call_count, 3)
        flock.assert_called_with(42, 3)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(0.05)

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

    def test_releasing_file_lock_error_does_not_strand_thread_lock(self):
        with patch("asclient.runtime._release_file_lock", side_effect=OSError("release failed")):
            with self.assertRaisesRegex(OSError, "release failed"):
                with device_lock("release-error"):
                    pass
        acquired = threading.Event()

        def acquire_after_error():
            with device_lock("release-error", timeout=0):
                acquired.set()

        thread = threading.Thread(target=acquire_after_error)
        thread.start()
        thread.join(1)
        self.assertTrue(acquired.is_set())
        self.assertFalse(thread.is_alive())

    def test_thread_lock_timeout_uses_a_shared_deadline_and_cleans_up(self):
        entered = threading.Event()
        release = threading.Event()

        def hold_lock():
            with device_lock("timeout-thread"):
                entered.set()
                release.wait(1)

        thread = threading.Thread(target=hold_lock)
        thread.start()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        with self.assertRaisesRegex(DeviceLockTimeoutError, "timeout-thread") as captured:
            with device_lock("timeout-thread", timeout=0.05):
                pass
        self.assertEqual(captured.exception.lock_id, "timeout-thread")
        self.assertEqual(captured.exception.timeout, 0.05)
        self.assertLess(time.monotonic() - started, 0.3)
        release.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        with device_lock("timeout-thread", timeout=0):
            pass

    def test_reentrant_lock_does_not_reacquire_file_lock_or_time_out(self):
        with device_lock("reentrant-timeout"):
            with device_lock("reentrant-timeout", timeout=0):
                pass

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

    def test_subprocess_file_lock_times_out_and_releases_local_lock(self):
        address = "subprocess-timeout-device"
        code = (
            "from asclient.errors import DeviceLockTimeoutError\n"
            "from asclient.runtime import device_lock\n"
            f"try:\n with device_lock({address!r}, timeout=0.1): pass\n"
            "except DeviceLockTimeoutError:\n print('timed-out', flush=True)\n"
        )
        with device_lock(address):
            process = subprocess.Popen(
                [sys.executable, "-c", code],
                cwd=Path(__file__).parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), "timed-out")
        with device_lock(address, timeout=0):
            pass

    def test_client_lock_id_wiring(self):
        client = AScriptClient("Example.TEST", lock_id="device-123", lock_timeout=1.5)
        self.assertEqual(client.lock_id, "device-123")
        self.assertEqual(client.lock_timeout, 1.5)
        with client.locked():
            with AScriptClient("other-host", lock_id="device-123").locked():
                pass
        device = connect("example.test", lock_id="device-456", lock_timeout=2)
        self.assertEqual(device.client.lock_id, "device-456")
        self.assertEqual(device.client.lock_timeout, 2.0)

    def test_client_locked_timeout_override_and_validation(self):
        client = AScriptClient("client-timeout", lock_timeout=0)
        with patch("asclient.client.device_lock") as lock:
            client.locked()
            lock.assert_called_once_with(client.address, lock_id=None, timeout=0.0)
            lock.reset_mock()
            client.locked(timeout=None)
            lock.assert_called_once_with(client.address, lock_id=None, timeout=None)
        with self.assertRaises(ValueError):
            AScriptClient("client-timeout", lock_timeout=-1)
        with self.assertRaises(ValueError):
            AScriptClient("client-timeout", lock_timeout=float("inf"))
        with self.assertRaises(ValueError):
            AScriptClient("client-timeout", lock_timeout=threading.TIMEOUT_MAX * 2)
