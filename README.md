# AScript Local Client

`asclient` is a dependency-free Python client and CLI for AScript iOS local device services.

```python
from asclient import AScriptClient

device = AScriptClient("192.168.3.17:9096")
device.save_screenshot("screen.png")
device.tap(200, 600)
device.upload_file("demo", "__init__.py")
device.run_project("demo")
```

Install on Windows from the repository root (the directory containing this file)
with `py -m pip install --user .`. The most reliable command is
`py -m asclient --device 192.168.3.17:9096 status`; it does not depend on the
Python Scripts directory being present in `PATH`.

The `asc.py` launcher remains compatible with `py asc.py ...`.

The library only depends on Python's standard library. The mobile APIs were statically verified against the bundled iOS 4001 IPA; use a reachable device to perform integration verification before production rollout.
