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

## Object API

The service stays on the phone; the client supplies an automation API modelled
after `uiautomator2` and resolves selectors through AScript's existing element
tree endpoint.

```python
from asclient import connect

device = connect("192.168.3.17:9096")
confirm = device(text="Confirm", class_name="XCUIElementTypeButton")
if confirm.exists:
    print(confirm.info)
    confirm.click()

# Stable explicit selectors and point inspection are also available.
device.selector().name("login_button")
device.selector().at(200, 600)
```

`py -m asclient --device 192.168.3.17:9096 inspect` starts a loopback-only
browser Inspector. It displays the current screenshot, control tree, properties
and a copyable selector. It needs no mobile-side installation or modification.
When an app exposes no accessibility tree, the inspector correctly shows no
semantic nodes; screenshot/OCR operations remain available separately.

Install on Windows from the repository root (the directory containing this file)
with `py -m pip install --user .`. The most reliable command is
`py -m asclient --device 192.168.3.17:9096 status`; it does not depend on the
Python Scripts directory being present in `PATH`.

The `asc.py` launcher remains compatible with `py asc.py ...`.

The library only depends on Python's standard library. The mobile APIs were statically verified against the bundled iOS 4001 IPA; use a reachable device to perform integration verification before production rollout.
