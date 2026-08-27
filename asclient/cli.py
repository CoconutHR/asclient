"""Command line interface for :mod:`asclient`."""
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any

from .client import AScriptClient
from .config import device_options, language_option, load_config, tunnel_options
from .doctor import diagnose, planned_iproxy_fix, save_report, set_iproxy_path
from .errors import AScriptError, TunnelError
from .i18n import current_language, set_language, t
from .tunnel import AScriptTunnel


def _client(args: argparse.Namespace) -> AScriptClient:
    options = device_options(load_config(args.config))
    address = args.device or options.get("address", "192.168.3.17:9096")
    password = args.password if args.password is not None else options.get("password", "")
    timeout = args.timeout if args.timeout is not None else options.get("timeout", 15.0)
    retries = options.get("retries", 1)
    return AScriptClient(address, password=password, timeout=timeout, retries=retries)


def _tunnel(args: argparse.Namespace) -> AScriptTunnel:
    options = tunnel_options(load_config(args.config))
    return AScriptTunnel(
        local_port=int(args.local_port if args.local_port is not None else options.get("local_port", 9096)),
        remote_port=int(args.remote_port if args.remote_port is not None else options.get("remote_port", 9096)),
        local_log_port=int(args.local_log_port if args.local_log_port is not None else options.get("local_log_port", 10102)),
        remote_log_port=int(args.remote_log_port if args.remote_log_port is not None else options.get("remote_log_port", 10102)),
        forward_logs=False if args.no_logs else bool(options.get("forward_logs", True)),
        udid=args.udid if args.udid is not None else str(options.get("udid", "")),
        executable=args.iproxy if args.iproxy is not None else str(options.get("iproxy", "iproxy")),
        local_host=str(options.get("local_host", "127.0.0.1")),
        startup_timeout=float(options.get("startup_timeout", 8)),
    )


def _out(value: Any) -> None:
    if isinstance(value, bytes):
        sys.stdout.buffer.write(value)
    elif value is not None:
        print(json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, (dict, list)) else value)


def _confirm(args: argparse.Namespace, client: AScriptClient, action: str) -> None:
    """Require an explicit acknowledgement for a state-changing CLI operation."""
    if not args.yes:
        raise ValueError(t("confirmation_required", device=client.address, action=action))
    print(t("confirmed", device=client.address, action=action), file=sys.stderr)


_HELP: dict[str, tuple[str, str]] = {
    "status": ("status\n查看设备可用性、屏幕尺寸和当前前台应用。", "status\nShow availability, screen size, and foreground app."),
    "doctor": ("doctor [--fix-iproxy PATH] [--yes]\n诊断本地工具、端口、设备服务和日志服务；仅在 --yes 确认后写入安全配置修复。", "doctor [--fix-iproxy PATH] [--yes]\nDiagnose local tools, ports, device service, and logs; write safe fixes only after --yes confirmation."),
    "tunnel": ("tunnel [--local-port PORT] [--remote-port PORT] [--local-log-port PORT] [--remote-log-port PORT] [--no-logs]\n通过 USB 同时转发控制端口 9096 和日志端口 10102。", "tunnel [--local-port PORT] [--remote-port PORT] [--local-log-port PORT] [--remote-log-port PORT] [--no-logs]\nForward USB control port 9096 and log port 10102."),
    "inspect": ("inspect\n启动本机 Inspector，查看截图、控件树、前台包名和坐标。", "inspect\nStart the local Inspector for screenshots, trees, foreground app, and coordinates."),
    "deploy": ("deploy PROJECT ENTRY [--logs SECONDS]\n上传入口文件、运行项目、收集日志并保存截图。需要 --yes。", "deploy PROJECT ENTRY [--logs SECONDS]\nUpload an entry file, run the project, collect logs, and save a screenshot. Requires --yes."),
    "log": ("log [SECONDS]\n读取设备日志回显；USB 模式需要 tunnel 同时映射 10102。", "log [SECONDS]\nRead device log output; USB mode requires tunnel to forward 10102."),
    "tap": ("tap X Y\n在真机坐标点击。需要 --yes。", "tap X Y\nTap a device coordinate. Requires --yes."),
    "tap-rel": ("tap-rel X_RATIO Y_RATIO\n按屏幕宽高比例点击，例如 0.5 0.92。需要 --yes。", "tap-rel X_RATIO Y_RATIO\nTap using screen ratios, for example 0.5 0.92. Requires --yes."),
}


def _stop_tunnel_on_sigterm(signum: int, frame: object) -> None:
    """Route process termination through the tunnel command's cleanup block."""
    raise KeyboardInterrupt


def _print_help(topic: str | None = None) -> None:
    language = current_language()
    if topic:
        item = _HELP.get(topic)
        if item is None:
            print(t("help_unknown", command=topic), file=sys.stderr)
            return
        print(item[0 if language == "zh" else 1])
        return
    if language == "zh":
        print("""ASClient 使用帮助

用法：py -m asclient [--config 文件] [--device 地址] [--lang zh-CN|en] <命令>

配置文件：默认读取当前目录的 asclient.json。可设置顶层 language 为 auto、zh-CN 或 en。

常用命令：
  doctor     诊断本机、USB 隧道、设备服务和日志服务
  status     查看设备状态与当前前台应用
  tunnel     通过 USB 转发 9096 控制端口及 10102 日志端口
  inspect    打开可视化控件检查器
  shot       保存真机截图
  dump       保存 XML 控件树
  deploy     上传并运行项目，收集日志和截图（需要 --yes）
  log        查看日志回显
  tap/tap-rel/swipe/input/home  执行设备操作（需要 --yes）

查看某个命令的详细参数：py -m asclient help <命令>""")
    else:
        print("""ASClient usage

Usage: py -m asclient [--config FILE] [--device ADDRESS] [--lang zh-CN|en] <command>

Configuration: reads asclient.json from the current directory by default. The top-level language can be auto, zh-CN, or en.

Common commands:
  doctor     Diagnose local tools, USB tunnel, device service, and log service
  status     Show device status and foreground app
  tunnel     Forward USB control port 9096 and log port 10102
  inspect    Open the visual control inspector
  shot       Save a device screenshot
  dump       Save the XML control tree
  deploy     Upload/run a project and collect logs/screenshots (requires --yes)
  log        Read log output
  tap/tap-rel/swipe/input/home  Perform device actions (requires --yes)

View command details: py -m asclient help <command>""")


def _print_doctor(checks: list[Any]) -> None:
    labels = {"ok": t("doctor_ok"), "warning": t("doctor_warning"), "error": t("doctor_error")}
    print(t("doctor_title"))
    for check in checks:
        print(f"[{labels[check.status]}] {check.message}")


def _confirm_repair(args: argparse.Namespace) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print(t("doctor_fix_declined"), file=sys.stderr)
        return False
    try:
        return input(t("doctor_fix_confirm")).strip().lower() in {"y", "yes"}
    except (EOFError, OSError):
        return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AScript 本地 iOS 设备客户端" if current_language() == "zh" else "AScript local iOS device client")
    parser.add_argument("--config", help="JSON config path; defaults to ./asclient.json when present")
    parser.add_argument("--device", help="HOST[:PORT]; overrides device.address in config")
    parser.add_argument("--password", help="overrides device.password in config")
    parser.add_argument("--timeout", type=float, help="seconds; overrides device.timeout in config")
    parser.add_argument("--lang", choices=("auto", "zh-CN", "en"), help="output language; defaults to the OS language or config language")
    parser.add_argument("--yes", action="store_true", help="confirm a state-changing operation")
    commands = parser.add_subparsers(dest="command", required=True)
    help_command = commands.add_parser("help", help="show concise usage help")
    help_command.add_argument("topic", nargs="?")
    doctor = commands.add_parser("doctor", help="diagnose local tools and device connectivity")
    doctor.add_argument("--fix-iproxy", metavar="PATH", help="save a validated absolute iproxy path after confirmation")
    doctor.add_argument("--report", metavar="FILE", help="write a password-free JSON diagnostic report")
    for name in ("ping", "status", "scan", "ls", "stop", "home", "app", "pkgs"):
        commands.add_parser(name)
    shot = commands.add_parser("shot"); shot.add_argument("output", nargs="?", default="screenshot.png"); shot.add_argument("--crop-rel", nargs=4, type=float, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"))
    dump = commands.add_parser("dump"); dump.add_argument("output", nargs="?", default="dump.xml"); dump.add_argument("--mode", default="smart")
    observe = commands.add_parser("observe"); observe.add_argument("--prefix", default="observe")
    inspect = commands.add_parser("inspect", help="start the local browser UI inspector")
    inspect.add_argument("--host", default="127.0.0.1"); inspect.add_argument("--port", type=int, default=0); inspect.add_argument("--no-browser", action="store_true")
    tunnel = commands.add_parser("tunnel", help="run an iproxy USB tunnel until Ctrl+C")
    tunnel.add_argument("--local-port", type=int); tunnel.add_argument("--remote-port", type=int)
    tunnel.add_argument("--local-log-port", type=int); tunnel.add_argument("--remote-log-port", type=int)
    tunnel.add_argument("--no-logs", action="store_true", help="forward only the HTTP service port")
    tunnel.add_argument("--udid"); tunnel.add_argument("--iproxy")
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
    for name in ("tap-rel", "swipe-rel"):
        item = commands.add_parser(name); item.add_argument("coordinates", nargs="+", type=float); item.add_argument("--duration", type=int, default=20 if name == "tap-rel" else 200)
    inp = commands.add_parser("input"); inp.add_argument("text"); inp.add_argument("--interval", type=int, default=120)
    raw = commands.add_parser("api", help="call a confirmed but unwrapped API endpoint")
    raw.add_argument("method"); raw.add_argument("path"); raw.add_argument("--params", default="{}"); raw.add_argument("--form", default="{}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        set_language(args.lang or language_option(config))
        if args.command == "help":
            _print_help(args.topic)
            return 0 if not args.topic or args.topic in _HELP else 1
        client = _client(args)
        cmd = args.command
        if cmd == "doctor":
            checks = diagnose(client, config)
            _print_doctor(checks)
            if args.report:
                print(t("doctor_report_saved", path=save_report(checks, args.report, client=client)))
            if args.fix_iproxy:
                if not Path(args.fix_iproxy).expanduser().is_file():
                    raise ValueError(t("doctor_fix_invalid", path=Path(args.fix_iproxy).expanduser()))
                print(planned_iproxy_fix(args.config, args.fix_iproxy))
                if _confirm_repair(args):
                    print(t("doctor_fix_done", path=set_iproxy_path(config, args.fix_iproxy, path=args.config)))
            return 1 if any(check.status == "error" for check in checks) else 0
        if cmd == "ping": _out({"platform": client.ping(), "device": str(client.address)})
        elif cmd == "status": _out(client.status())
        elif cmd == "scan": _out([{"device": str(address), "platform": platform} for address, platform in client.scan_subnet()])
        elif cmd == "pkgs": _out(client.packages())
        elif cmd == "shot": print(client.save_screenshot_crop_relative(args.output, *args.crop_rel) if args.crop_rel else client.save_screenshot(args.output))
        elif cmd == "dump": Path(args.output).write_text(client.ui_xml(mode=args.mode), encoding="utf-8"); print(Path(args.output).resolve())
        elif cmd == "observe":
            stamp = time.strftime("%Y%m%d_%H%M%S"); image, xml = Path(f"{args.prefix}_{stamp}.png"), Path(f"{args.prefix}_{stamp}.xml")
            print(client.save_screenshot(image)); xml.write_text(client.ui_xml(), encoding="utf-8"); print(xml.resolve())
        elif cmd == "inspect":
            from .inspector import serve
            server = serve(client, host=args.host, port=args.port, open_browser=not args.no_browser)
            print(t("inspector_running", url=f"http://{args.host}:{server.server_port}/"))
            try: server.serve_forever()
            except KeyboardInterrupt: pass
            finally: server.server_close()
        elif cmd == "tunnel":
            tunnel = _tunnel(args).start()
            routes = f"service={tunnel.address} -> device:{tunnel.remote_port}"
            if tunnel.log_address:
                routes += f"; logs={tunnel.log_address} -> device:{tunnel.remote_log_port}"
            print(t("tunnel_running", routes=routes, address=tunnel.address))
            previous_sigterm = signal.signal(signal.SIGTERM, _stop_tunnel_on_sigterm)
            try:
                while tunnel.is_running: time.sleep(0.25)
                raise TunnelError(t("tunnel_exited", detail=tunnel.exit_summary()))
            except KeyboardInterrupt: pass
            finally:
                signal.signal(signal.SIGTERM, previous_sigterm)
                tunnel.stop()
        elif cmd == "eval":
            _confirm(args, client, t("action_eval"))
            _out(client.eval_python(args.code))
        elif cmd == "cat":
            data = client.read_file(args.path)
            if args.output: Path(args.output).write_bytes(data); print(Path(args.output).resolve())
            else: sys.stdout.buffer.write(data)
        elif cmd == "ocr": _out(client.ocr(args.rect))
        elif cmd == "findcolor": _out(client.find_colors(args.colors, diff=args.diff or 0.98))
        elif cmd == "compare": _out(client.compare_colors(args.colors, diff=args.diff or 0.9))
        elif cmd == "ls": _out(client.projects())
        elif cmd == "create": _confirm(args, client, t("action_create", project=args.project)); client.create_project(args.project)
        elif cmd == "run": _confirm(args, client, t("action_run", project=args.project)); client.run_project(args.project)
        elif cmd == "stop": _confirm(args, client, t("action_stop")); client.stop_project()
        elif cmd == "remove": _confirm(args, client, t("action_remove", project=args.project)); client.remove_project(args.project)
        elif cmd == "rename": _confirm(args, client, t("action_rename", project=args.project, new_name=args.new_name)); client.rename_project(args.project, args.new_name)
        elif cmd == "files": _out(client.project_files(args.project))
        elif cmd == "push":
            _confirm(args, client, t("action_upload", project=args.project))
            source = Path(args.source); count = client.upload_tree(args.project, source) if source.is_dir() else (client.upload_file(args.project, source, args.remote) or 1); print(t("uploaded_count", count=count))
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
            _confirm(args, client, t("action_deploy", project=args.project))
            logs, image = client.deploy(args.project, args.entry, log_seconds=args.logs)
            for entry in logs: print(f"[{entry.kind}] {entry.timestamp} {entry.message}")
            path = Path(args.screenshot or f"deploy_{time.strftime('%Y%m%d_%H%M%S')}.png"); path.write_bytes(image); print(path.resolve())
        elif cmd == "tap":
            if len(args.coordinates) != 2: raise ValueError(t("tap_requires"))
            _confirm(args, client, t("action_tap", coordinates=args.coordinates))
            _out(client.tap(*args.coordinates, duration_ms=args.duration))
        elif cmd == "tap-rel":
            if len(args.coordinates) != 2: raise ValueError(t("tap_relative_requires"))
            _confirm(args, client, t("action_tap_relative", coordinates=args.coordinates))
            _out(client.tap_relative(*args.coordinates, duration_ms=args.duration))
        elif cmd == "swipe":
            if len(args.coordinates) != 4: raise ValueError(t("swipe_requires"))
            _confirm(args, client, t("action_swipe", coordinates=args.coordinates))
            _out(client.swipe(*args.coordinates, duration_ms=args.duration))
        elif cmd == "swipe-rel":
            if len(args.coordinates) != 4: raise ValueError(t("swipe_relative_requires"))
            _confirm(args, client, t("action_swipe_relative", coordinates=args.coordinates))
            _out(client.swipe_relative(*args.coordinates, duration_ms=args.duration))
        elif cmd == "input": _confirm(args, client, t("action_input")); _out(client.input_text(args.text, interval_ms=args.interval))
        elif cmd == "home": _confirm(args, client, t("action_home")); _out(client.home())
        elif cmd == "app": _out(client.current_app())
        elif cmd == "api":
            _confirm(args, client, t("action_api", method=args.method.upper(), path=args.path))
            params, form = json.loads(args.params), json.loads(args.form)
            raw = client.request(args.method, args.path, params=params or None, form=form or None)
            try: _out(json.loads(raw.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError): sys.stdout.buffer.write(raw)
    except (AScriptError, ValueError, OSError) as exc:
        print(f"{t('error_prefix')}: {exc}", file=sys.stderr)
        return 1
    return 0
