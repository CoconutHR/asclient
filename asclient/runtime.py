"""Process-local coordination primitives shared by clients for one device."""
from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from typing import Iterator


_registry_lock = RLock()
_device_locks: dict[str, RLock] = {}


def lock_for(address: object) -> RLock:
    key = str(address)
    with _registry_lock:
        return _device_locks.setdefault(key, RLock())


@contextmanager
def device_lock(address: object) -> Iterator[None]:
    """Serialize process-local actions directed at a single device address."""
    lock = lock_for(address)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
