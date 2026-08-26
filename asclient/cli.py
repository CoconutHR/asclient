"""Command line interface for :mod:`asclient`."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .client import AScriptClient
from .config import device_options, load_config
from .errors import AScriptError


def _client(args: argparse.Namespace) -> AScriptClient:
    options = device_options(load_config(args.config))
    address = args.device or options.get("address", "192.168.3.17:9096")
    password = args.password if args.password is not None else options.get("password", "")
    timeout = args.timeout if args.timeout is not None else options.get("timeout", 15.0)
    retries = options.get("retries", 1)
    return AScriptClient(address, password=password, timeout=timeout, retries=retries)


def _out(value: Any) -> None:
    if isinstance(value, bytes):
        sys.stdout.buffer.write(value)
    elif value is not None:
        print(json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, (dict, list)) else value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AScript local iOS device client")
    parser.add_argument("--config", help="JSON config path; defaults to ./asclient.json when present")
    parser.add_argument("--device", help="HOST[:PORT]; overrides device.address in config")
    parser.add_argument("--password", help="overrides device.password in config")
    parser.add_argument("--timeout", type=float, help="seconds; overrides device.timeout in config")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("ping", "status", "scan", "ls", "stop", "home", "app", "pkgs"):
        commands.add_parser(name)
    shot = commands.add_parser("shot"); shot.add_argument("output", nargs="?", default="screenshot.png")
    dump = commands.add_parser("dump"); dump.add_argument("output", nargs="?", default="dump.xml"); dump.add_argument("--mode", default="smart")
    observe = commands.add_parser("observe"); observe.add_argument("--prefix", default="observe")
    inspect = commands.add_parser("inspect", help="start the local browser UI inspector")
    inspect.add_argument("--host", default="127.0.0.1"); inspect.add_argument("--port", type=int, default=0); inspect.add_argument("--no-browser", action="store_true")
    ev = commands.add_parser("eval"); ev.add_argument("code")
    cat = commands.add_parser("cat"); cat.add_argument("path"); cat.add_argument("output", nargs="?")
    ocr = commands.add_parser("ocr"); ocr.add_argument("rect", nargs="?")
    for name in ("findcolor", "compare"):
        item = commands.add_parser(name); item.add_argument("colors"); item.add_argument("--diff", type=float)
    for name in ("create", "run", "files", "remove"):
        item = commands.add_parser(name); item.add_argument("project")
    ren = commands.add_parser("rename"); ren.add_argument("project"); ren.add_argument("new_name")
    push = commands.add_parser("push"); push.add_argument("project"); push.add_argument("source"); push.add_argument("remote", nargs="?")
    pull = commands.add_parser("pull"); pull.add_argument("project"); pull.add_argument("output", nargs="?", default=".")
    deploy = commands.add_parser("deploy"); deploy.add_argument("project"); deploy.add_argument("entry"); deploy.add_argument("--logs", type=float, default=5.0); deploy.add_argument("--screenshot")
    log = commands.add_parser("log"); log.add_argument("seconds", nargs="?", type=float, default=3.0); log.add_argument("--reconnects", type=int, default=0); log.add_argument("--output"); log.add_argument("--contains")
    for name in ("tap", "swipe"):
        item = commands.add_parser(name); item.add_argument("coordinates", nargs="+", type=float); item.add_argument("--duration", type=int, default=20 if name == "tap" else 200)
    inp = commands.add_parser("input"); inp.add_argument("text"); inp.add_argument("--interval", type=int, default=120)
    raw = commands.add_parser("api", help="call a confirmed but unwrapped API endpoint")
    raw.add_argument("method"); raw.add_argument("path"); raw.add_argument("--params", default="{}"); raw.add_argument("--form", default="{}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    client = _client(args)
    try:
        cmd = args.command
        if cmd == "ping": _out({"platform": client.ping(), "device": str(client.address)})
        elif cmd == "status": _out(client.status())
        elif cmd == "scan": _out([{"device": str(address), "platform": platform} for address, platform in client.scan_subnet()])
        elif cmd == "pkgs": _out(client.packages())
        elif cmd == "shot": print(client.save_screenshot(args.output))
        elif cmd == "dump": Path(args.output).write_text(client.ui_xml(mode=args.mode), encoding="utf-8"); print(Path(args.output).resolve())
        elif cmd == "observe":
            stamp = time.strftime("%Y%m%d_%H%M%S"); image, xml = Path(f"{args.prefix}_{stamp}.png"), Path(f"{args.prefix}_{stamp}.xml")
            print(client.save_screenshot(image)); xml.write_text(client.ui_xml(), encoding="utf-8"); print(xml.resolve())
        elif cmd == "inspect":
            from .inspector import serve
            server = serve(client, host=args.host, port=args.port, open_browser=not args.no_browser)
            print(f"Inspector is running at http://{args.host}:{server.server_port}/. Press Ctrl+C to stop.")
            try: server.serve_forever()
            except KeyboardInterrupt: pass
            finally: server.server_close()
        elif cmd == "eval": _out(client.eval_python(args.code))
        elif cmd == "cat":
            data = client.read_file(args.path)
            if args.output: Path(args.output).write_bytes(data); print(Path(args.output).resolve())
            else: sys.stdout.buffer.write(data)
        elif cmd == "ocr": _out(client.ocr(args.rect))
        elif cmd == "findcolor": _out(client.find_colors(args.colors, diff=args.diff or 0.98))
        elif cmd == "compare": _out(client.compare_colors(args.colors, diff=args.diff or 0.9))
        elif cmd == "ls": _out(client.projects())
        elif cmd == "create": client.create_project(args.project)
        elif cmd == "run": client.run_project(args.project)
        elif cmd == "stop": client.stop_project()
        elif cmd == "remove": client.remove_project(args.project)
        elif cmd == "rename": client.rename_project(args.project, args.new_name)
        elif cmd == "files": _out(client.project_files(args.project))
        elif cmd == "push":
            source = Path(args.source); count = client.upload_tree(args.project, source) if source.is_dir() else (client.upload_file(args.project, source, args.remote) or 1); print(f"uploaded {count} file(s)")
        elif cmd == "pull":
            for target in client.download_project(args.project, args.output): print(target)
        elif cmd == "log":
            output = Path(args.output).open("w", encoding="utf-8") if args.output else None
            try:
                for entry in client.logs(duration=args.seconds, reconnects=args.reconnects):
                    if args.contains and args.contains not in entry.message: continue
                    print(f"[{entry.kind}] {entry.timestamp} {entry.message}")
                    if output: output.write(json.dumps({"message": entry.message, "kind": entry.kind, "timestamp": entry.timestamp}, ensure_ascii=False) + "\n")
            finally:
                if output: output.close()
        elif cmd == "deploy":
            logs, image = client.deploy(args.project, args.entry, log_seconds=args.logs)
            for entry in logs: print(f"[{entry.kind}] {entry.timestamp} {entry.message}")
            path = Path(args.screenshot or f"deploy_{time.strftime('%Y%m%d_%H%M%S')}.png"); path.write_bytes(image); print(path.resolve())
        elif cmd == "tap":
            if len(args.coordinates) != 2: raise ValueError("tap requires X Y")
            _out(client.tap(*args.coordinates, duration_ms=args.duration))
        elif cmd == "swipe":
            if len(args.coordinates) != 4: raise ValueError("swipe requires X1 Y1 X2 Y2")
            _out(client.swipe(*args.coordinates, duration_ms=args.duration))
        elif cmd == "input": _out(client.input_text(args.text, interval_ms=args.interval))
        elif cmd == "home": _out(client.home())
        elif cmd == "app": _out(client.current_app())
        elif cmd == "api":
            params, form = json.loads(args.params), json.loads(args.form)
            raw = client.request(args.method, args.path, params=params or None, form=form or None)
            try: _out(json.loads(raw.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError): sys.stdout.buffer.write(raw)
    except (AScriptError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
