"""Cross-process coordination primitives shared by clients for one device."""
from __future__ import annotations

import errno
import hashlib
import math
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from threading import RLock, TIMEOUT_MAX, local
from typing import IO, Iterator

from .errors import DeviceLockTimeoutError


_RETRY_INTERVAL = 0.05


class _DeviceLock:
    def __init__(self) -> None:
        self.lock = RLock()
        self.local = local()


_registry_lock = RLock()
_device_locks: dict[str, _DeviceLock] = {}


def normalize_lock_id(address: object, lock_id: object | None = None) -> str:
    """Return a stable lock identity for an address or explicit device ID."""
    if lock_id is not None:
        return str(lock_id)
    return str(address).strip().casefold().removeprefix("http://").removeprefix("https://").rstrip("/")


def lock_for(address: object, *, lock_id: object | None = None) -> RLock:
    """Return this process's reentrant mutex for a device lock identity."""
    key = normalize_lock_id(address, lock_id)
    with _registry_lock:
        return _device_locks.setdefault(key, _DeviceLock()).lock


def _lock_path(key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / "asclient-locks" / f"{digest}.lock"


def _acquire_file_lock(handle: IO[bytes], *, deadline: float | None = None, lock_id: object = "unknown", timeout: float | None = None) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                _wait_for_file_lock(deadline, lock_id, timeout)
    else:
        import fcntl

        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                _wait_for_file_lock(deadline, lock_id, timeout)


def _wait_for_file_lock(deadline: float | None, lock_id: object, timeout: float | None) -> None:
    if deadline is None:
        time.sleep(_RETRY_INTERVAL)
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeviceLockTimeoutError(lock_id, timeout)
    time.sleep(min(_RETRY_INTERVAL, remaining))


def _release_file_lock(handle: IO[bytes]) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def device_lock(address: object, *, lock_id: object | None = None, timeout: float | None = None) -> Iterator[None]:
    """Serialize actions for one device across threads and local processes.

    The process-local ``RLock`` remains reentrant. Its outermost acquisition by
    a thread holds the persistent per-device file lock until the matching exit.
    ``timeout`` bounds both local and file-lock acquisition; ``None`` waits
    indefinitely for backwards compatibility.
    """
    if timeout is not None:
        try:
            timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("lock timeout must be a finite non-negative number of seconds") from exc
        if not math.isfinite(timeout) or timeout < 0 or timeout > TIMEOUT_MAX:
            raise ValueError("lock timeout must be a finite non-negative number of seconds")
    deadline = None if timeout is None else time.monotonic() + timeout
    key = normalize_lock_id(address, lock_id)
    with _registry_lock:
        state = _device_locks.setdefault(key, _DeviceLock())
    if deadline is None:
        acquired = state.lock.acquire()
    else:
        acquired = state.lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
    if not acquired:
        raise DeviceLockTimeoutError(key, timeout)
    depth = getattr(state.local, "depth", 0)
    handle: IO[bytes] | None = None
    file_locked = False
    try:
        if depth == 0:
            path = _lock_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+b")
            if os.fstat(handle.fileno()).st_size == 0:
                handle.seek(0)
                handle.write(b"\0")
                handle.flush()
            _acquire_file_lock(handle, deadline=deadline, lock_id=key, timeout=timeout)
            file_locked = True
            state.local.handle = handle
        state.local.depth = depth + 1
        yield
    finally:
        try:
            state.local.depth = depth
            if depth == 0 and handle is not None:
                try:
                    if file_locked:
                        _release_file_lock(handle)
                finally:
                    handle.close()
                    if hasattr(state.local, "handle"):
                        del state.local.handle
        finally:
            state.lock.release()
