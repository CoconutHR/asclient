import base64
import json
import socket
import struct
import tempfile
import threading
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from urllib.parse import quote
from urllib.request import urlopen

from asclient import AScriptClient, AScriptTunnel, Device, DeviceOperationError, Run, connect
from asclient.cli import _stop_tunnel_on_sigterm, main
from asclient.config import device_options, load_config, tunnel_options
from asclient.doctor import DoctorCheck, _port_available, diagnose, save_report, set_iproxy_path
from asclient.i18n import set_language
from asclient.tunnel import _iproxy_not_found_message


class Handler(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *args):
        pass

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def _reply(self, value, status=200, content_type="application/json"):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def do_GET(self):
        parsed = urlparse(self.path); Handler.calls.append(("GET", parsed.path, parse_qs(parsed.query), b""))
        if parsed.path == "/api/screen/capture": return self._reply(b"PNG", content_type="image/png")
        if parsed.path == "/api/screen/size": return self._reply({"code": 1, "data": {"width": 100, "height": 200}})
        if parsed.path == "/api/node/dump": return self._reply(b"<App/>", content_type="application/xml")
        if parsed.path == "/api/node/package": return self._reply({"code": 1, "data": {"name": "Example App", "bundle_id": "com.example.app", "pid": 42}})
        if parsed.path == "/api/tool/view/dump": return self._reply({"code": 1, "data": {"config": {"display": {"widthPixels": 100, "heightPixels": 200}}, "views": [{"type": "XCUIElementTypeButton", "name": "confirm", "label": "Confirm", "x": 10, "y": 20, "width": 30, "height": 40, "childs": []}]}})
        if parsed.path == "/api/module/create": return self._reply({"code": 1})
        self._reply({"code": 1, "data": []})

    def do_POST(self):
        parsed = urlparse(self.path); body = self._body(); Handler.calls.append(("POST", parsed.path, parse_qs(parsed.query), body))
        if parsed.path == "/api/model/pip": return self._reply(b"")
        if parsed.path == "/api/gp/eval": return self._reply({"code": 1, "data": "true"})
        if parsed.path == "/api/bad": return self._reply({"code": -1, "msg": "bad request"})
        self._reply({"code": 1, "data": []})


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.client = AScriptClient(f"127.0.0.1:{cls.server.server_port}", retries=0)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()

    def setUp(self):
        Handler.calls.clear()

    def test_ping_screenshot_and_ui_xml(self):
        self.assertEqual(self.client.ping(), "iOS")
        self.assertEqual(self.client.screenshot(), b"PNG")
        self.assertEqual(self.client.ui_xml(), "<App/>")

    def test_relative_screenshot_crop_preserves_requested_pixels(self):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        pixels = [bytes((x, y, 0, 255)) for y in range(4) for x in range(4)]
        raw = b"".join(b"\0" + b"".join(pixels[row * 4:(row + 1) * 4]) for row in range(4))
        header = struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0)
        source = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
        crop = self.client.crop_png_relative(source, 0.25, 0.25, 0.75, 0.75)
        self.assertEqual(self.client._png_size(crop), (2.0, 2.0))
        self.assertIn(bytes((1, 1, 0, 255)), zlib.decompress(crop[crop.index(b"IDAT") + 4:-12]))
        with self.assertRaises(ValueError): self.client.crop_png_relative(source, 0.8, 0.1, 0.2, 0.9)

    def test_image_matching_honors_confidence_and_region(self):
        from PIL import Image
        from io import BytesIO
        source = Image.new("RGB", (24, 20), "black")
        template = Image.new("RGB", (4, 3), "black")
        for y in range(3):
            for x in range(4): template.putpixel((x, y), (20 + x * 30, 40 + y * 40, 180))
        source.paste(template, (12, 8))
        source_data, template_data = BytesIO(), BytesIO()
        source.save(source_data, "PNG"); template.save(template_data, "PNG")
        match = self.client._image_match(source_data.getvalue(), template_data.getvalue(), confidence=0.99, region=(0.4, 0.3, 0.9, 0.8))
        self.assertIsNotNone(match)
        self.assertEqual((match.x, match.y, match.width, match.height), (12, 8, 4, 3))
        self.assertIsNone(self.client._image_match(source_data.getvalue(), template_data.getvalue(), confidence=0.99, region=(0, 0, 0.4, 0.3)))

    def test_scroll_until_image_checks_before_each_directional_swipe(self):
        original_find, original_swipe, original_relative = self.client.find_image, self.client.swipe_relative, self.client.relative_point
        attempts, swipes = [], []
        try:
            self.client.find_image = lambda *args, **kwargs: attempts.append(1) or ({} if len(attempts) == 3 else None)
            self.client.swipe_relative = lambda *args, **kwargs: swipes.append((args, kwargs))
            self.client.relative_point = lambda *args: (1, 1)
            result = self.client.scroll_until_image(b"template", direction="up", max_swipes=4, interval=0.001, initial_delay=False)
        finally:
            self.client.find_image, self.client.swipe_relative, self.client.relative_point = original_find, original_swipe, original_relative
        self.assertEqual(result, {})
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(swipes), 2)
        self.assertEqual(swipes[0][0], (0.5, 0.8, 0.5, 0.2))

    def test_scroll_until_image_defaults_down_and_supports_horizontal_directions(self):
        original_find, original_swipe = self.client.find_image, self.client.swipe_relative
        swipes = []
        try:
            self.client.find_image = lambda *args, **kwargs: None
            self.client.swipe_relative = lambda *args, **kwargs: swipes.append(args)
            for direction in ("down", "left", "right", "上"):
                with self.assertRaises(TimeoutError): self.client.scroll_until_image(b"template", direction=direction, max_swipes=1, timeout=1, initial_delay=False)
        finally:
            self.client.find_image, self.client.swipe_relative = original_find, original_swipe
        self.assertEqual(swipes, [(0.5, 0.2, 0.5, 0.8), (0.8, 0.5, 0.2, 0.5), (0.2, 0.5, 0.8, 0.5), (0.5, 0.8, 0.5, 0.2)])

    def test_scroll_until_image_can_print_each_match_attempt(self):
        original_find, original_relative = self.client.find_image, self.client.relative_point
        output = StringIO()
        try:
            self.client.find_image = lambda *args, **kwargs: None
            self.client.relative_point = lambda *args: (1, 1)
            with redirect_stdout(output):
                with self.assertRaises(TimeoutError): self.client.scroll_until_image(b"template", max_swipes=0, log=True, initial_delay=False)
        finally:
            self.client.find_image, self.client.relative_point = original_find, original_relative
        self.assertIn("attempt 1", output.getvalue())

    def test_image_wait_can_print_each_attempt(self):
        original_find = self.client.find_image
        output = StringIO()
        try:
            self.client.find_image = lambda *args, **kwargs: None
            with redirect_stdout(output):
                with self.assertRaises(TimeoutError): self.client.wait_image(b"template", timeout=0, log=True)
        finally:
            self.client.find_image = original_find
        self.assertIn("attempt 1", output.getvalue())

    def test_image_wait_delays_before_its_first_probe_by_default(self):
        original_find = self.client.find_image
        try:
            self.client.find_image = lambda *args, **kwargs: {}
            with patch("asclient.client.time.sleep") as sleep:
                self.assertEqual(self.client.wait_image(b"template", timeout=5, interval=0.2), {})
        finally:
            self.client.find_image = original_find
        sleep.assert_called_once_with(0.2)

    def test_selector_wait_can_print_each_attempt(self):
        device = Device(self.client)
        original_find_all = device.find_all
        output = StringIO()
        try:
            device.find_all = lambda selector: []
            with redirect_stdout(output): self.assertIsNone(device.find(device.selector().name("missing"), timeout=0, log=True))
        finally:
            device.find_all = original_find_all
        self.assertIn("attempt 1", output.getvalue())

    def test_upload_builds_multipart_and_safe_path(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "main.py"; source.write_text("print(1)", encoding="utf-8")
            self.client.upload_file("demo", source, "src/main.py")
        upload = next(call for call in Handler.calls if call[1] == "/api/file/upload")
        self.assertEqual(upload[2]["path"], ["~/modules/demo/src/main.py"])
        self.assertIn(b"print(1)", upload[3])
        with self.assertRaises(ValueError): self.client.upload_file("../bad", source)

    def test_eval_actions_are_encoded(self):
        self.client.input_text("a'\n中文")
        call = next(call for call in Handler.calls if call[1] == "/api/gp/eval")
        self.assertIn(b"ascript.ios.action", call[3])

    def test_relative_coordinates_scale_and_remain_in_screen_bounds(self):
        original_json, original_tap, original_screenshot = self.client.json, self.client.tap, self.client.screenshot
        self.client.json = lambda method, path, **kwargs: {"code": 1, "data": {"width": 393, "height": 852}} if path == "/api/screen/size" else original_json(method, path, **kwargs)
        try:
            self.client.screenshot = lambda: b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + (1179).to_bytes(4, "big") + (2556).to_bytes(4, "big")
            self.assertEqual(self.client.screen_size(), {"width": 1179.0, "height": 2556.0})
            self.assertEqual(self.client.relative_point(0.5, 0.92), (589.5, 2351.52))
            self.assertEqual(self.client.relative_point(1, 1), (1178.0, 2555.0))
            tapped = []
            self.client.tap = lambda x, y, **kwargs: tapped.append((x, y, kwargs))
            Device(self.client).click_rel(0.5, 0.92, duration_ms=30)
        finally:
            self.client.json, self.client.tap, self.client.screenshot = original_json, original_tap, original_screenshot
        self.assertEqual(tapped, [(589.5, 2351.52, {"duration_ms": 30})])
        with self.assertRaises(ValueError): self.client.relative_point(-0.1, 0.5)
        with self.assertRaises(ValueError): self.client.relative_point(float("nan"), 0.5)

    def test_ui_tree_coordinates_are_normalized_to_action_pixels(self):
        original_screenshot = self.client.screenshot
        try:
            self.client.screenshot = lambda: b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + (300).to_bytes(4, "big") + (600).to_bytes(4, "big")
            tree = self.client.ui_tree(x=150, y=300)
        finally:
            self.client.screenshot = original_screenshot
        node = tree["views"][0]
        self.assertEqual((node["x"], node["y"], node["width"], node["height"]), (30.0, 60.0, 90.0, 120.0))
        self.assertEqual(tree["config"]["display"]["widthPixels"], 300.0)
        lookup = next(call for call in Handler.calls if call[1] == "/api/tool/view/dump")
        self.assertEqual(lookup[2]["x"], ["50.0"])
        self.assertEqual(lookup[2]["y"], ["100.0"])

    def test_operation_error(self):
        with self.assertRaises(DeviceOperationError): self.client._ok({"code": -1, "msg": "bad request"})

    def test_status_falls_back_when_the_device_reports_a_status_error(self):
        original, original_screenshot = self.client.json, self.client.screenshot
        def fake_json(method, path, **kwargs):
            if path == "/api/status": return {"code": -1, "msg": "ObjCStrInstance object is not callable"}
            if path == "/api/screen/size": return {"code": 1, "data": {"width": 100, "height": 200}}
            if path == "/api/node/package": return {"code": 1, "data": {"bundle_id": "example"}}
            return original(method, path, **kwargs)
        try:
            self.client.json = fake_json
            self.client.screenshot = lambda: b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + (300).to_bytes(4, "big") + (600).to_bytes(4, "big")
            status = self.client.status()
        finally:
            self.client.json, self.client.screenshot = original, original_screenshot
        self.assertTrue(status["available"])
        self.assertEqual(status["health"], "degraded")
        self.assertEqual(status["compatibility"]["status_api"]["issue"], "ios_objc_property_callable")
        self.assertEqual(status["compatibility"]["capabilities"]["screen"], "available")
        self.assertEqual(status["screen"]["width"], 300)
        self.assertEqual(status["logical_screen"]["width"], 100)

    def test_packages_uses_eval_when_status_has_no_package_list(self):
        original_status, original_eval = self.client.status, self.client.eval_python
        self.client.status = lambda: {"available": True}
        self.client.eval_python = lambda code: [["numpy", "1.0"], ["requests", "2.0"]]
        self.assertEqual(self.client.packages(), [["numpy", "1.0"], ["requests", "2.0"]])
        self.client.status, self.client.eval_python = original_status, original_eval

    def test_project_file_paths_support_ios_childs_and_nested_directories(self):
        tree = {"name": "demo", "isFile": False, "childs": [
            {"name": "res", "isFile": False, "childs": [
                {"name": "img", "isFile": False, "childs": [{"name": "logo.png", "isFile": True}]}
            ]},
            {"name": "__init__.py", "isFile": True},
        ]}
        self.assertEqual(self.client._project_file_paths(tree), ["__init__.py", "res/img/logo.png"])

    def test_uiautomator_style_device_selector_resolves_and_clicks(self):
        device = Device(self.client)
        button = device(text="Confirm", class_name="XCUIElementTypeButton")
        self.assertTrue(button.exists)
        self.assertEqual(button.count, 1)
        self.assertEqual(button.info["name"], "confirm")
        original_screenshot = self.client.screenshot
        self.client.screenshot = lambda: b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + (300).to_bytes(4, "big") + (600).to_bytes(4, "big")
        try: button.click()
        finally: self.client.screenshot = original_screenshot
        lookup = next(call for call in Handler.calls if call[1] == "/api/tool/view/dump")
        selector = json.loads(lookup[2]["selector"][0])
        self.assertEqual(selector["sel"][0], {"key": "label", "params": "Confirm"})
        action = next(call for call in Handler.calls if call[1] == "/api/gp/eval")[3]
        self.assertIn(b"ascript.ios.action", action)
        self.assertIn(b"click%2875.0%2C+120.0", action)

    def test_connect_returns_a_device_facade(self):
        self.assertIsInstance(connect(f"127.0.0.1:{self.server.server_port}", retries=0), Device)

    def test_json_configuration_is_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "asclient.json"
            path.write_text('{"device": {"address": "127.0.0.1:9096", "timeout": 20}}', encoding="utf-8")
            self.assertEqual(device_options(load_config(path))["address"], "127.0.0.1:9096")
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError): load_config(path)

    def test_tunnel_configuration_and_command(self):
        options = tunnel_options({"tunnel": {"iproxy": "custom-iproxy", "local_port": 19096, "remote_port": 9096, "local_log_port": 11002, "remote_log_port": 10102, "forward_logs": True, "udid": "abc"}})
        tunnel = AScriptTunnel(**{"local_port": int(options["local_port"]), "remote_port": int(options["remote_port"]), "local_log_port": int(options["local_log_port"]), "remote_log_port": int(options["remote_log_port"]), "forward_logs": options["forward_logs"], "udid": options["udid"], "executable": options["iproxy"]})
        self.assertEqual(tunnel.address, "127.0.0.1:19096")
        self.assertEqual(tunnel.log_address, "127.0.0.1:11002")
        self.assertEqual(tunnel.service.command, ["custom-iproxy", "-u", "abc", "19096", "9096"])
        self.assertEqual(tunnel.logs.command if tunnel.logs else None, ["custom-iproxy", "-u", "abc", "11002", "10102"])
        self.assertIsNone(AScriptTunnel(forward_logs=False).log_address)

    def test_missing_iproxy_message_is_actionable_on_windows(self):
        set_language("zh-CN")
        try:
            with patch("asclient.tunnel.sys.platform", "win32"):
                message = _iproxy_not_found_message("iproxy")
        finally:
            set_language("en")
        self.assertIn("未找到 iproxy", message)
        self.assertIn("where iproxy", message)
        self.assertIn("iproxy.exe", message)
        self.assertIn("tunnel.iproxy", message)

    def test_chinese_help_does_not_require_a_device(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            status = main(["--lang", "zh-CN", "help"])
        self.assertEqual(status, 0)
        self.assertIn("ASClient 使用帮助", stdout.getvalue())
        self.assertIn("doctor", stdout.getvalue())

    def test_doctor_can_write_only_a_validated_iproxy_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "iproxy.exe"
            executable.write_bytes(b"test")
            config = root / "asclient.json"
            config.write_text('{"device": {"address": "127.0.0.1:9096"}}', encoding="utf-8")
            target = set_iproxy_path(load_config(config), str(executable), path=config)
            saved = load_config(target)
            self.assertEqual(saved["tunnel"]["iproxy"], str(executable.resolve()))
            with self.assertRaises(ValueError):
                set_iproxy_path(saved, str(root / "missing.exe"), path=config)

    def test_doctor_report_omits_password(self):
        with tempfile.TemporaryDirectory() as directory:
            report = save_report([DoctorCheck("device", "ok", "reachable")], Path(directory) / "doctor.json", client=AScriptClient("127.0.0.1:9096", password="secret"))
            value = report.read_text(encoding="utf-8")
            self.assertIn('"device"', value)
            self.assertNotIn("secret", value)

    def test_doctor_detects_an_occupied_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            self.assertFalse(_port_available("127.0.0.1", occupied.getsockname()[1]))

    def test_doctor_recognizes_ports_used_by_an_active_local_tunnel(self):
        with patch("asclient.doctor._port_available", return_value=False):
            checks = diagnose(self.client, {"tunnel": {"local_port": 19096, "local_log_port": 11002}})
        ports = {check.name: check for check in checks if check.name in {"service_port", "log_port"}}
        self.assertEqual(ports["service_port"].status, "ok")
        self.assertEqual(ports["service_port"].detail, "active_local_tunnel")
        self.assertEqual(ports["log_port"].status, "ok")

    def test_sigterm_handler_enters_cleanup_path(self):
        with self.assertRaises(KeyboardInterrupt):
            _stop_tunnel_on_sigterm(15, None)

    def test_tunnel_stop_waits_for_local_port_release(self):
        class Process:
            def __init__(self): self.terminated = False
            def poll(self): return None
            def terminate(self): self.terminated = True
            def wait(self, timeout): return 0
        tunnel = AScriptTunnel(forward_logs=False).service
        process = Process()
        tunnel._process = process
        with patch.object(tunnel, "_wait_for_port_release") as released:
            tunnel.stop()
        self.assertTrue(process.terminated)
        released.assert_called_once_with()

    def test_cli_requires_yes_for_state_changes(self):
        stderr = StringIO()
        with redirect_stderr(stderr):
            status = main(["--device", f"127.0.0.1:{self.server.server_port}", "remove", "demo"])
        self.assertEqual(status, 1)
        self.assertIn("--yes", stderr.getvalue())
        self.assertFalse(any(call[1] == "/api/module/remove" for call in Handler.calls))

    def test_capture_artifacts_saves_available_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = self.client.capture_artifacts(directory)
            self.assertEqual(artifacts["screenshot"].read_bytes(), b"PNG")
            self.assertEqual(artifacts["xml"].read_text(encoding="utf-8"), "<App/>")
            self.assertTrue(artifacts["context"].is_file())

    def test_run_records_steps_and_failure_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            with Run(Device(self.client), directory, run_id="unit-run") as run:
                element = run.assert_unique(Device(self.client).selector().name("confirm"), name="confirm_is_unique")
                self.assertEqual(element.info["name"], "confirm")
                with self.assertRaises(RuntimeError): run.step("expected_failure", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
            manifest = json.loads((Path(directory) / "unit-run" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["outcome"], "passed")
            self.assertEqual(manifest["steps"][1]["outcome"], "failed")
            self.assertTrue((Path(directory) / "unit-run" / "expected_failure_failure.json").is_file())

    def test_inspector_serves_a_loopback_snapshot(self):
        from asclient.inspector import serve
        server = serve(self.client, open_browser=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=2) as response:
                page = response.read().decode("utf-8")
            self.assertIn('id="appmeta"', page)
            self.assertIn('id="coordinate"', page)
            self.assertIn('id="divider-left"', page)
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/snapshot", timeout=2) as response:
                snapshot = json.loads(response.read())
            self.assertEqual(snapshot["tree"]["views"][0]["name"], "confirm")
            self.assertEqual(snapshot["coordinate_space"], {"width": 100, "height": 200})
            self.assertEqual(snapshot["app"]["bundle_id"], "com.example.app")
            self.assertEqual(base64.b64decode(snapshot["image"]), b"PNG")
            selector = quote(json.dumps({"sel": [{"key": "name", "params": "confirm"}], "find": 99999}))
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/selector?selector={selector}", timeout=2) as response:
                self.assertEqual(json.loads(response.read())["count"], 1)
        finally:
            server.shutdown(); server.server_close()

    def test_inspector_ignores_a_closed_browser_socket(self):
        from asclient.inspector import serve
        server = serve(self.client, open_browser=False)
        handler = object.__new__(server.RequestHandlerClass)
        handler.send_response = lambda *args: None
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        class ClosedWriter:
            def write(self, value): raise ConnectionAbortedError("browser closed")
        handler.wfile = ClosedWriter()
        self.assertFalse(handler._send(200, b"snapshot", "text/plain"))
        server.server_close()


if __name__ == "__main__":
    unittest.main()
