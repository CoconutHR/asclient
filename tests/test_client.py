import base64
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from io import StringIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.parse import quote
from urllib.request import urlopen

from asclient import AScriptClient, Device, DeviceOperationError, IProxyTunnel, Run, connect
from asclient.cli import main
from asclient.config import device_options, load_config, tunnel_options


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

    def test_operation_error(self):
        with self.assertRaises(DeviceOperationError): self.client._ok({"code": -1, "msg": "bad request"})

    def test_status_falls_back_when_the_device_reports_a_status_error(self):
        original = self.client.json
        def fake_json(method, path, **kwargs):
            if path == "/api/status": return {"code": -1, "msg": "ObjCStrInstance object is not callable"}
            if path == "/api/screen/size": return {"code": 1, "data": {"width": 100, "height": 200}}
            if path == "/api/node/package": return {"code": 1, "data": {"bundle_id": "example"}}
            return original(method, path, **kwargs)
        self.client.json = fake_json
        status = self.client.status()
        self.assertTrue(status["available"])
        self.assertEqual(status["screen"]["width"], 100)

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
        button.click()
        lookup = next(call for call in Handler.calls if call[1] == "/api/tool/view/dump")
        selector = json.loads(lookup[2]["selector"][0])
        self.assertEqual(selector["sel"][0], {"key": "label", "params": "Confirm"})
        self.assertIn(b"ascript.ios.action", next(call for call in Handler.calls if call[1] == "/api/gp/eval")[3])

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
        options = tunnel_options({"tunnel": {"iproxy": "custom-iproxy", "local_port": 19096, "remote_port": 9096, "udid": "abc"}})
        tunnel = IProxyTunnel(**{"local_port": int(options["local_port"]), "remote_port": int(options["remote_port"]), "udid": options["udid"], "executable": options["iproxy"]})
        self.assertEqual(tunnel.address, "127.0.0.1:19096")
        self.assertEqual(tunnel.command, ["custom-iproxy", "-u", "abc", "19096", "9096"])

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
            self.assertIn('id="divider-left"', page)
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/snapshot", timeout=2) as response:
                snapshot = json.loads(response.read())
            self.assertEqual(snapshot["tree"]["views"][0]["name"], "confirm")
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
