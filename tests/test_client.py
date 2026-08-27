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
from urllib.request import Request, urlopen

from asclient import AScriptClient, AScriptTunnel, Device, DeviceOperationError, Run, UiObject, connect
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
        # 文案断言固定为英文，避免测试结果依赖操作系统语言。
        set_language("en")
        self.addCleanup(set_language, None)

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
        original = self.client.screenshot
        self.client.screenshot = lambda: source
        try:
            self.assertEqual(self.client._png_size(self.client.screenshot_crop(1, 1, 3, 3)), (2.0, 2.0))
        finally:
            self.client.screenshot = original
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
        original_find, original_swipe, original_relative = self.client.find_image, self.client.swipe_relative, self.client.relative_point
        swipes = []
        try:
            self.client.find_image = lambda *args, **kwargs: None
            self.client.swipe_relative = lambda *args, **kwargs: swipes.append(args)
            self.client.relative_point = lambda *args: (1, 1)
            for direction in ("down", "left", "right", "上"):
                with self.assertRaises(TimeoutError): self.client.scroll_until_image(b"template", direction=direction, max_swipes=1, timeout=1, initial_delay=False)
        finally:
            self.client.find_image, self.client.swipe_relative, self.client.relative_point = original_find, original_swipe, original_relative
        self.assertEqual(swipes, [(0.5, 0.2, 0.5, 0.8), (0.8, 0.5, 0.2, 0.5), (0.2, 0.5, 0.8, 0.5), (0.5, 0.8, 0.5, 0.2)])

    def test_scroll_until_image_accepts_custom_relative_swipe(self):
        original_find, original_swipe, original_relative = self.client.find_image, self.client.swipe_relative, self.client.relative_point
        swipes = []
        try:
            self.client.find_image = lambda *args, **kwargs: None
            self.client.swipe_relative = lambda *args, **kwargs: swipes.append((args, kwargs))
            self.client.relative_point = lambda *args: (1, 1)
            with self.assertRaises(TimeoutError):
                self.client.scroll_until_image(b"template", swipe_relative=(0.7, 0.75, 0.35, 0.25), duration_ms=650, max_swipes=1, initial_delay=False)
            with self.assertRaises(ValueError): self.client.scroll_until_image(b"template", x1_ratio=0.5)
            with self.assertRaises(ValueError): self.client.scroll_until_image(b"template", swipe_relative=(0.5, 0.5), initial_delay=False)
        finally:
            self.client.find_image, self.client.swipe_relative, self.client.relative_point = original_find, original_swipe, original_relative
        self.assertEqual(swipes, [((0.7, 0.75, 0.35, 0.25), {"duration_ms": 650})])

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

    def test_tunnel_from_config_reads_the_tunnel_section(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "asclient.json"
            path.write_text(json.dumps({"tunnel": {"iproxy": "custom-iproxy", "local_port": 19096, "udid": "abc", "unknown": "ignored"}}), encoding="utf-8")
            tunnel = AScriptTunnel.from_config(path)
            self.assertEqual(tunnel.address, "127.0.0.1:19096")
            self.assertEqual(tunnel.service.command, ["custom-iproxy", "-u", "abc", "19096", "9096"])
            # 显式参数优先于配置文件；未覆盖的键继续沿用配置。
            overridden = AScriptTunnel.from_config(path, udid="device-b", local_port=29096)
            self.assertEqual(overridden.service.command, ["custom-iproxy", "-u", "device-b", "29096", "9096"])
            # 配置文件不存在时退回内置默认值。
            self.assertEqual(AScriptTunnel.from_config(Path(directory) / "missing.json").address, "127.0.0.1:9096")

    def test_tunnel_from_config_supports_and_rejects_parameter_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "asclient.json"
            path.write_text('{"tunnel": {"iproxy": "custom-iproxy", "local_port": 19096, "udid": "abc"}}', encoding="utf-8")
            # 过时别名 executable 仍可用，但发出弃用警告。
            with self.assertWarns(DeprecationWarning):
                aliased = AScriptTunnel.from_config(path, executable="alias-iproxy")
            self.assertEqual(aliased.service.command, ["alias-iproxy", "-u", "abc", "19096", "9096"])
            with self.assertRaises(ValueError): AScriptTunnel.from_config(path, executable="a", iproxy="b")
            with self.assertRaises(ValueError): AScriptTunnel.from_config(path, unknown=1)

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

    def test_scan_command_is_removed(self):
        with self.assertRaises(SystemExit):
            main(["scan"])

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

    def test_run_label_keeps_unicode_and_sanitizes_unsafe_characters(self):
        from asclient.run import _label
        self.assertEqual(_label("填写用户名"), "填写用户名")
        self.assertEqual(_label("打开/登录: 第二步"), "打开_登录_第二步")
        self.assertEqual(_label("step 1."), "step_1")
        self.assertEqual(_label("///"), "step")

    def test_cli_accepts_yes_after_the_subcommand(self):
        stderr = StringIO()
        with redirect_stderr(stderr):
            status = main(["--device", f"127.0.0.1:{self.server.server_port}", "remove", "demo", "--yes"])
        self.assertEqual(status, 0)
        self.assertTrue(any(call[1] == "/api/module/remove" for call in Handler.calls))

    def test_help_documents_every_cli_command(self):
        from argparse import _SubParsersAction
        from asclient.cli import _HELP, _parser
        choices = next(action.choices for action in _parser()._actions if isinstance(action, _SubParsersAction))
        self.assertEqual(set(choices), set(_HELP))

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
            self.assertIn("ASClient 控件检查器", page)
            self.assertIn("裁剪保存", page)
            self.assertIn("验证选择器", page)
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

    def test_inspector_saves_browser_crop_only_in_configured_directory(self):
        from asclient.inspector import serve
        image = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + (2).to_bytes(4, "big") + (3).to_bytes(4, "big")
        with tempfile.TemporaryDirectory() as directory:
            server = serve(self.client, open_browser=False, output_dir=directory)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                request = Request(f"http://127.0.0.1:{server.server_port}/api/crop", data=json.dumps({"image": base64.b64encode(image).decode()}).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request, timeout=2) as response: result = json.loads(response.read())
                saved = Path(result["path"])
                self.assertEqual(saved.parent, Path(directory).resolve())
                self.assertEqual(saved.read_bytes(), image)
            finally:
                server.shutdown(); server.server_close()

    def test_frame_pixel_colors_and_relative_coordinates_share_one_screenshot(self):
        from io import BytesIO
        from PIL import Image
        image = Image.new("RGBA", (4, 3), (0, 0, 0, 255)); image.putpixel((1, 1), (12, 34, 56, 78))
        output = BytesIO(); image.save(output, "PNG")
        original = self.client.screenshot; calls = []
        self.client.screenshot = lambda: calls.append(1) or output.getvalue()
        try:
            frame = self.client.capture_frame()
            color = frame.pixel(1, 1)
            self.assertEqual(color.rgb, (12, 34, 56))
            self.assertEqual(color.rgba, (12, 34, 56, 78))
            self.assertEqual(color.hex, "#0C2238")
            self.assertEqual(frame.pixel_relative(0.25, 1 / 3).rgb, (12, 34, 56))
            self.assertEqual(self.client.pixels([(1, 1), (0, 0)])[0].hex, "#0C2238")
        finally:
            self.client.screenshot = original
        self.assertEqual(len(calls), 2)

    def test_multi_template_matching_uses_one_frame_and_each_region(self):
        from io import BytesIO
        from PIL import Image
        image = Image.new("RGB", (16, 12), "black")
        red = Image.new("RGB", (2, 2), "red"); blue = Image.new("RGB", (2, 2), "blue")
        image.paste(red, (2, 3)); image.paste(blue, (11, 7))
        source, red_data, blue_data = BytesIO(), BytesIO(), BytesIO()
        image.save(source, "PNG"); red.save(red_data, "PNG"); blue.save(blue_data, "PNG")
        original = self.client.screenshot; calls = []
        self.client.screenshot = lambda: calls.append(1) or source.getvalue()
        try:
            matches = self.client.find_images({"red": red_data.getvalue(), "blue": blue_data.getvalue()}, confidence=1, regions_relative={"red": (0, 0, .5, .7), "blue": (.5, .5, 1, 1)})
            self.assertEqual((matches["red"].x, matches["red"].y), (2, 3))
            self.assertEqual((matches["blue"].x, matches["blue"].y), (11, 7))
            self.assertEqual(self.client.find_any_image({"blue": blue_data.getvalue(), "red": red_data.getvalue()}, confidence=1)[0], "blue")
        finally:
            self.client.screenshot = original
        self.assertEqual(len(calls), 2)

    def test_tree_uses_protocol_scale_for_retina_coordinates(self):
        original_json, original_screenshot = self.client.json, self.client.screenshot
        def fake_json(method, path, **kwargs):
            if path == "/api/screen/size": return {"code": 1, "data": {"width": 393, "height": 852}}
            if path == "/api/tool/view/dump": return {"code": 1, "data": {"config": {"scale": 3, "display": {"widthPixels": 393, "heightPixels": 852}}, "views": [{"name": "button", "x": 20, "y": 63, "width": 353, "height": 36, "rect": {"left": 20, "top": 63, "right": 373, "bottom": 99}, "childs": []}]}}
            return original_json(method, path, **kwargs)
        try:
            self.client.json = fake_json
            self.client.screenshot = lambda: b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + (1179).to_bytes(4, "big") + (2556).to_bytes(4, "big")
            tree = self.client.ui_tree(x=300, y=600)
        finally:
            self.client.json, self.client.screenshot = original_json, original_screenshot
        node = tree["views"][0]
        self.assertEqual((node["x"], node["y"], node["width"], node["height"]), (60.0, 189.0, 1059.0, 108.0))
        self.assertEqual((node["rect"]["left"], node["rect"]["right"]), (60.0, 1119.0))

    def test_ui_snapshot_queries_relationships_without_extra_requests(self):
        original = self.client.ui_tree; calls = []
        self.client.ui_tree = lambda **kwargs: calls.append(kwargs) or {"views": [{"name": "root", "childs": [{"name": "left", "label": "A", "childs": [{"name": "deep", "childs": []}]}, {"name": "right", "label": "B", "childs": []}]}]}
        try:
            device = Device(self.client); snapshot = device.snapshot()
            left = snapshot(name="left")
            self.assertEqual(left.child(device.selector().name("deep")).count, 1)
            self.assertEqual(left.descendant(device.selector().name("deep")).count, 1)
            self.assertEqual(left.sibling(device.selector().name("right")).count, 1)
            self.assertEqual(left.parent(device.selector().name("root")).count, 1)
            self.assertEqual(device(name="left").snapshot().count, 1)
        finally:
            self.client.ui_tree = original
        self.assertEqual(len(calls), 2)

    def test_template_path_cache_reloads_when_file_changes(self):
        from io import BytesIO
        from PIL import Image
        screen = Image.new("RGB", (8, 8), "black"); screen.paste(Image.new("RGB", (2, 2), "red"), (3, 3))
        screen_data = BytesIO(); screen.save(screen_data, "PNG")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "template.png"
            Image.new("RGB", (2, 2), "red").save(path)
            from asclient import ScreenFrame
            image = ScreenFrame(screen_data.getvalue())
            self.assertIsNotNone(image.find_image(path, confidence=1))
            Image.new("RGB", (2, 2), "blue").save(path)
            self.assertIsNone(image.find_image(path, confidence=1))

    def test_wait_any_image_returns_matching_name(self):
        from io import BytesIO
        from PIL import Image
        source = Image.new("RGB", (8, 8), "black"); source.paste(Image.new("RGB", (2, 2), "green"), (4, 4))
        source_data, green_data, red_data = BytesIO(), BytesIO(), BytesIO()
        source.save(source_data, "PNG"); Image.new("RGB", (2, 2), "green").save(green_data, "PNG"); Image.new("RGB", (2, 2), "red").save(red_data, "PNG")
        original = self.client.screenshot; self.client.screenshot = lambda: source_data.getvalue()
        try:
            name, match = self.client.wait_any_image({"missing": red_data.getvalue(), "found": green_data.getvalue()}, confidence=1, timeout=0, initial_delay=False)
        finally:
            self.client.screenshot = original
        self.assertEqual(name, "found")
        self.assertEqual((match.x, match.y), (4, 4))

    def test_wait_any_uses_one_full_snapshot_per_attempt_and_regex_stays_local(self):
        original = self.client.ui_tree; calls = []
        self.client.ui_tree = lambda **kwargs: calls.append(kwargs) or {"views": [{"name": "root", "childs": [{"name": "success_42", "label": "完成", "childs": []}]}]}
        try:
            device = Device(self.client)
            name, element = device.wait_any({"failure": device.selector().name("failure"), "success": device.selector().name("success_42")}, timeout=0)
            self.assertEqual(name, "success")
            self.assertEqual(element.info["name"], "success_42")
            self.assertEqual(device.snapshot()(name="root").descendant().where_regex("name", r"success_\d+").count, 1)
        finally:
            self.client.ui_tree = original
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["mode"], "full")

    def test_wait_interval_is_configurable_and_validated(self):
        device = Device(self.client)
        with self.assertRaises(ValueError): device.find(device.selector().name("missing"), interval=0)
        with self.assertRaises(ValueError): device.wait_gone(device.selector().name("missing"), interval=0)

    def test_coordinate_api_pairs_and_snapshot_index_keep_tree_order(self):
        original_tree, original_tap, original_size = self.client.ui_tree, self.client.tap, self.client.action_size
        self.client.ui_tree = lambda **kwargs: {"views": [{"name": "root", "x": 0, "y": 0, "width": 100, "height": 200, "childs": [{"name": "button", "type": "Button", "label": "A", "x": 10, "y": 20, "width": 30, "height": 40, "childs": []}, {"name": "button", "type": "Button", "label": "B", "x": 50, "y": 60, "width": 20, "height": 30, "childs": []}]}]}
        taps = []; self.client.tap = lambda x, y, **kwargs: taps.append((x, y, kwargs))
        try:
            device = Device(self.client); snapshot = device.snapshot()
            self.assertEqual([item.info["label"] for item in snapshot(name="button").all()], ["A", "B"])
            self.assertEqual(snapshot.select(device.selector().at(15, 25)).info["label"], "A")
            self.client.action_size = lambda: {"width": 100, "height": 200}
            self.assertEqual(snapshot.select(device.selector().at_relative(.15, .125)).info["label"], "A")
            element = snapshot(name="button").get(); element.object.click_relative(1, 1)
            self.assertEqual(taps[0][:2], (39.0, 59.0))
            self.assertTrue(hasattr(device, "tap")); self.assertTrue(hasattr(device, "screenshot_crop"))
        finally:
            self.client.ui_tree, self.client.tap, self.client.action_size = original_tree, original_tap, original_size

    def test_empty_element_rectangles_and_absolute_image_regions_are_validated(self):
        device = Device(self.client)
        with self.assertRaises(ValueError):
            UiObject(device, {"x": 0, "y": 0, "width": 0, "height": 0}, device.selector()).click()
        from io import BytesIO
        from PIL import Image
        screen, template = Image.new("RGB", (8, 8), "black"), Image.new("RGB", (2, 2), "red")
        screen.paste(template, (4, 4)); source, needle = BytesIO(), BytesIO(); screen.save(source, "PNG"); template.save(needle, "PNG")
        original = self.client.screenshot; self.client.screenshot = lambda: source.getvalue()
        try:
            self.assertEqual(self.client.wait_image(needle.getvalue(), confidence=1, timeout=0, initial_delay=False, region_pixels=(4, 4, 8, 8)).center, (5.0, 5.0))
            with self.assertRaises(ValueError): self.client.wait_image(needle.getvalue(), timeout=0, initial_delay=False, region=(0, 0, 1, 1), region_pixels=(0, 0, 8, 8))
        finally:
            self.client.screenshot = original

    def test_image_region_uses_pixels_and_legacy_relative_inputs_warn(self):
        from io import BytesIO
        from PIL import Image
        source = Image.new("RGB", (10, 10), "black"); template = Image.new("RGB", (2, 2), "red"); source.paste(template, (6, 6))
        source_data, template_data = BytesIO(), BytesIO(); source.save(source_data, "PNG"); template.save(template_data, "PNG")
        from asclient import ScreenFrame
        frame = ScreenFrame(source_data.getvalue())
        self.assertEqual((frame.find_image(template_data.getvalue(), confidence=1, region=(5, 5, 10, 10)).x, frame.find_image(template_data.getvalue(), confidence=1, region=(5, 5, 10, 10)).y), (6, 6))
        self.assertEqual((frame.find_image(template_data.getvalue(), confidence=1, region_relative=(.5, .5, 1, 1)).x, frame.find_image(template_data.getvalue(), confidence=1, region_relative=(.5, .5, 1, 1)).y), (6, 6))
        with self.assertWarns(DeprecationWarning):
            self.assertIsNotNone(frame.find_image(template_data.getvalue(), confidence=1, region=(.5, .5, 1., 1.)))
        with self.assertWarns(DeprecationWarning):
            self.assertIsNotNone(frame.find_image(template_data.getvalue(), confidence=1, region_pixels=(5, 5, 10, 10)))
        self.assertIsNone(frame.find_image(template_data.getvalue(), confidence=1, region=(0, 0, 1, 1)))
        with self.assertRaises(ValueError):
            frame.find_image(template_data.getvalue(), confidence=1, region=(5, 5, 10, 10), region_relative=(.5, .5, 1, 1))

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
