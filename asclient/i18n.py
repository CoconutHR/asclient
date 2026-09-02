"""Small dependency-free localization support for user-facing output."""
from __future__ import annotations

import locale
from contextvars import ContextVar
from typing import Literal


Language = Literal["zh", "en"]
_language: ContextVar[Language | None] = ContextVar("asclient_language", default=None)


def detect_language(value: str | None = None) -> Language:
    """Resolve an explicit setting or the operating system's display locale."""
    if value:
        normalized = value.lower().replace("_", "-")
        if normalized in {"zh", "zh-cn", "zh-hans", "auto"}:
            if normalized != "auto":
                return "zh"
        elif normalized in {"en", "en-us", "en-gb"}:
            return "en"
        elif normalized != "auto":
            raise ValueError(t("language_invalid"))
    try:
        system_locale = locale.getlocale()[0] or ""
    except ValueError:
        system_locale = ""
    normalized_locale = system_locale.lower()
    return "zh" if normalized_locale.startswith("zh") or "chinese" in normalized_locale or "中文" in normalized_locale else "en"


def set_language(value: str | None = None) -> Language:
    """Set the language for the current command invocation or library context."""
    selected = detect_language(value)
    _language.set(selected)
    return selected


def current_language() -> Language:
    return _language.get() or detect_language()


_MESSAGES: dict[str, tuple[str, str]] = {
    "error_prefix": ("错误", "error"),
    "confirmed": ("[已确认] 设备={device} 操作={action}", "[confirmed] device={device} action={action}"),
    "confirmation_required": ("拒绝在设备 {device} 上执行{action}；请加上 --yes 明确确认后重试", "refusing {action} on device {device}; rerun with --yes before the command to confirm"),
    "action_eval": ("执行设备端 Python", "execute device Python"),
    "action_create": ("创建项目 {project!r}", "create project {project!r}"),
    "action_run": ("运行项目 {project!r}", "run project {project!r}"),
    "action_stop": ("停止当前项目", "stop current project"),
    "action_remove": ("删除项目 {project!r}", "remove project {project!r}"),
    "action_rename": ("将项目 {project!r} 重命名为 {new_name!r}", "rename project {project!r} to {new_name!r}"),
    "action_upload": ("上传文件到项目 {project!r}", "upload into project {project!r}"),
    "action_mv": ("将设备端文件 {path!r} 重命名为 {new_name!r}", "rename device-side file {path!r} to {new_name!r}"),
    "action_app_start": ("启动 App {bundle_id!r}", "launch app {bundle_id!r}"),
    "action_app_stop": ("停止 App {bundle_id!r}", "stop app {bundle_id!r}"),
    "action_lock": ("锁定屏幕", "lock the screen"),
    "action_unlock": ("解锁屏幕", "unlock the screen"),
    "action_clipboard_set": ("写入设备剪贴板", "write the device clipboard"),
    "action_open_url": ("打开 {url!r}", "open {url!r}"),
    "action_key": ("发送按键 {key!r}", "press key {key!r}"),
    "action_notification": ("打开通知中心", "open the notification center"),
    "action_deploy": ("部署并运行项目 {project!r}", "deploy and run project {project!r}"),
    "action_tap": ("点击坐标 {coordinates}", "tap at {coordinates}"),
    "action_swipe": ("滑动坐标 {coordinates}", "swipe {coordinates}"),
    "action_input": ("向当前控件输入文本", "input text into focused control"),
    "action_home": ("执行 Home 操作", "press Home"),
    "action_api": ("调用原始 API {method} {path}", "raw API {method} {path}"),
    "tap_requires": ("tap 需要两个参数：X Y", "tap requires X Y"),
    "swipe_requires": ("swipe 需要四个参数：X1 Y1 X2 Y2", "swipe requires X1 Y1 X2 Y2"),
    "relative_ratio_invalid": ("相对坐标比例必须是 0.0 到 1.0 之间的有限数字", "relative coordinate ratios must be finite numbers between 0.0 and 1.0"),
    "image_scroll_match": ("[图像滚动] 第 {attempt} 次匹配：已找到，坐标=({x}, {y})，置信度={confidence:.4f}", "[image scroll] attempt {attempt}: found at ({x}, {y}), confidence={confidence:.4f}"),
    "image_scroll_next": ("[图像滚动] 第 {attempt} 次匹配：未找到；继续滑动", "[image scroll] attempt {attempt}: not found; continuing to swipe"),
    "image_scroll_stop": ("[图像滚动] 第 {attempt} 次匹配：未找到；已达到停止条件", "[image scroll] attempt {attempt}: not found; stop condition reached"),
    "element_scroll_match": ("[控件滚动] 第 {attempt} 轮：已找到 {name}", "[element scroll] attempt {attempt}: found {name}"),
    "element_scroll_next": ("[控件滚动] 第 {attempt} 轮：未找到；继续滑动", "[element scroll] attempt {attempt}: not found; continuing to swipe"),
    "element_scroll_stop": ("[控件滚动] 第 {attempt} 轮：未找到；已达到停止条件", "[element scroll] attempt {attempt}: stop condition reached"),
    "watcher_triggered": ("[监控] 规则 {name} 已命中并执行动作（第 {count} 次）", "[watch] rule {name} triggered and action executed (count {count})"),
    "image_wait_found": ("[图像等待] 第 {attempt} 次检查：已找到，坐标=({x}, {y})，置信度={confidence:.4f}", "[image wait] attempt {attempt}: found at ({x}, {y}), confidence={confidence:.4f}"),
    "image_wait_missing": ("[图像等待] 第 {attempt} 次检查：未找到", "[image wait] attempt {attempt}: not found"),
    "image_wait_gone": ("[图像等待] 第 {attempt} 次检查：图像已消失", "[image wait] attempt {attempt}: image is gone"),
    "image_wait_present": ("[图像等待] 第 {attempt} 次检查：图像仍存在，置信度={confidence:.4f}", "[image wait] attempt {attempt}: image still present, confidence={confidence:.4f}"),
    "selector_wait_found": ("[控件等待] 第 {attempt} 次检查：已找到 {selector}", "[selector wait] attempt {attempt}: found {selector}"),
    "selector_wait_missing": ("[控件等待] 第 {attempt} 次检查：未找到 {selector}", "[selector wait] attempt {attempt}: not found {selector}"),
    "selector_wait_gone": ("[控件等待] 第 {attempt} 次检查：控件已消失 {selector}", "[selector wait] attempt {attempt}: element is gone {selector}"),
    "selector_wait_present": ("[控件等待] 第 {attempt} 次检查：控件仍存在 {selector}", "[selector wait] attempt {attempt}: element still present {selector}"),
    "screen_size_invalid": ("设备返回了无效的屏幕尺寸", "the device returned an invalid screen size"),
    "action_tap_relative": ("点击比例坐标 {coordinates}", "tap relative coordinate {coordinates}"),
    "action_swipe_relative": ("滑动比例坐标 {coordinates}", "swipe relative coordinates {coordinates}"),
    "tap_relative_requires": ("tap-rel 需要两个比例参数：X_RATIO Y_RATIO", "tap-rel requires two ratios: X_RATIO Y_RATIO"),
    "swipe_relative_requires": ("swipe-rel 需要四个比例参数：X1_RATIO Y1_RATIO X2_RATIO Y2_RATIO", "swipe-rel requires four ratios: X1_RATIO Y1_RATIO X2_RATIO Y2_RATIO"),
    "uploaded_count": ("已上传 {count} 个文件", "uploaded {count} file(s)"),
    "inspector_running": ("Inspector 已启动：{url}。按 Ctrl+C 停止。", "Inspector is running at {url}. Press Ctrl+C to stop."),
    "tunnel_running": ("USB 隧道已启动：{routes}。请将 device.address 设置为 {address}。按 Ctrl+C 停止。", "USB tunnel is running: {routes}. Set device.address to {address}. Press Ctrl+C to stop."),
    "help_overview": ("ASClient 使用帮助", "ASClient usage"),
    "help_usage": ("用法", "Usage"),
    "help_config": ("配置文件", "Configuration"),
    "help_commands": ("常用命令", "Common commands"),
    "help_more": ("查看某个命令的详细参数：py -m asclient help <命令>", "View a command's arguments: py -m asclient help <command>"),
    "help_unknown": ("未知命令：{command}。可运行 py -m asclient help 查看可用命令。", "Unknown command: {command}. Run py -m asclient help to list commands."),
    "language_invalid": ("language 只能是 'auto'、'zh-CN' 或 'en'", "language must be 'auto', 'zh-CN', or 'en'"),
    "device_address_empty": ("设备地址不能为空", "device address is empty"),
    "device_address_invalid": ("设备地址必须是 HOST[:PORT]", "device address must be HOST[:PORT]"),
    "path_must_start": ("API 路径必须以 '/' 开头", "path must start with '/'") ,
    "form_data_exclusive": ("form 与 data 不能同时使用", "form and data are mutually exclusive"),
    "cannot_reach_device": ("无法连接 AScript 设备 {address}: {detail}", "cannot reach AScript device at {address}: {detail}"),
    "invalid_json": ("接口 {path} 返回了无效 JSON", "invalid JSON response from {path}"),
    "expected_object": ("接口 {path} 应返回 JSON 对象", "expected object response from {path}"),
    "status_fallback_summary": ("/api/status 存在设备端兼容性问题，客户端已降级探测可用能力", "the device /api/status endpoint has a compatibility issue; the client used capability fallback"),
    "status_compensated_fields": ("已通过设备端 eval 回填只读字段：{fields}", "read-only fields were backfilled via on-device eval: {fields}"),
    "cannot_reach_logs": ("无法连接 AScript 日志服务 {host}:10102: {detail}", "cannot reach AScript log service at {host}:10102: {detail}"),
    "websocket_rejected": ("AScript 日志服务拒绝 WebSocket 连接", "AScript log endpoint rejected WebSocket upgrade"),
    "doctor_title": ("ASClient 环境诊断", "ASClient environment diagnosis"),
    "doctor_ok": ("正常", "ok"),
    "doctor_warning": ("警告", "warning"),
    "doctor_error": ("错误", "error"),
    "doctor_iproxy_found": ("已找到 iproxy：{path}", "iproxy found: {path}"),
    "doctor_iproxy_missing": ("未找到 iproxy。无法自动安装第三方二进制；请安装受信任的 libimobiledevice，或用 doctor --fix-iproxy <路径> 写入已有 iproxy 的绝对路径。", "iproxy was not found. Third-party binaries are never installed automatically; install a trusted libimobiledevice build or use doctor --fix-iproxy <path> to save an existing iproxy path."),
    "doctor_iproxy_missing_optional": ("未找到 iproxy。仅 USB 隧道需要它，而当前 device.address 不是本机回环地址，可忽略本项。如需使用 USB，请安装受信任的 libimobiledevice，或用 doctor --fix-iproxy <路径> 写入已有 iproxy 的绝对路径。", "iproxy was not found. Only USB tunnels need it and device.address is not a loopback address, so this check can be ignored. To use USB, install a trusted libimobiledevice build or use doctor --fix-iproxy <path> to save an existing iproxy path."),
    "doctor_port_available": ("本机端口 {host}:{port} 可用于建立隧道", "local port {host}:{port} is available for a tunnel"),
    "doctor_port_busy": ("本机端口 {host}:{port} 已被占用。不会自动结束其他程序；请关闭占用程序或在配置中使用其他 local_port。", "local port {host}:{port} is already in use. Other processes are never stopped automatically; close the owner or configure a different local_port."),
    "doctor_port_tunnel": ("本机端口 {host}:{port} 正由活动 USB 隧道使用。", "local port {host}:{port} is in use by an active USB tunnel."),
    "doctor_device_ok": ("设备服务可访问，平台：{platform}", "device service is reachable, platform: {platform}"),
    "doctor_device_failed": ("无法访问设备服务：{detail}。请检查 device.address、网络/USB 隧道、AScript 服务开关和密码。", "cannot reach the device service: {detail}. Check device.address, network/USB tunnel, AScript service, and password."),
    "doctor_log_ok": ("日志端口可连接：{host}:{port}", "log port is reachable: {host}:{port}"),
    "doctor_log_failed": ("无法连接日志端口 {host}:{port}：{detail}。Wi-Fi 请检查网络；USB 请确认 tunnel 未使用 --no-logs 且 {port} 已映射。", "cannot reach log port {host}:{port}: {detail}. On Wi-Fi check the network; on USB ensure tunnel was not started with --no-logs and that {port} is forwarded."),
    "doctor_status_fallback": ("设备的 /api/status 存在已知兼容性问题，客户端已使用屏幕与前台应用信息降级；设备仍可用。原始错误：{detail}", "the device /api/status endpoint has a known compatibility issue; the client used screen and foreground-app fallback and the device remains usable. Original error: {detail}"),
    "doctor_status_ok": ("设备状态接口正常", "device status endpoint is healthy"),
    "doctor_fix_plan": ("准备写入配置 {path}：tunnel.iproxy = {executable}", "ready to write configuration {path}: tunnel.iproxy = {executable}"),
    "doctor_fix_confirm": ("是否写入这个本地配置修复？[y/N] ", "Write this local configuration fix? [y/N] "),
    "doctor_fix_declined": ("未写入配置。要执行修复，请在交互终端回答 y，或加入 --yes。", "configuration was not changed. Answer y in an interactive terminal or add --yes to apply the fix."),
    "doctor_fix_done": ("已写入配置修复：{path}", "configuration fix written: {path}"),
    "doctor_fix_invalid": ("指定的 iproxy 路径不是文件：{path}。未写入任何配置。", "the supplied iproxy path is not a file: {path}. No configuration was changed."),
    "doctor_report_saved": ("诊断报告已保存：{path}", "diagnostic report saved: {path}"),
    "iproxy_missing_windows": ("未找到 iproxy 可执行文件: {executable}。\nWindows：请安装包含 iproxy.exe 的可信 libimobiledevice 发行版；然后将其目录加入 PATH 并执行 'where iproxy' 验证，或在 asclient.json 中设置 \"tunnel.iproxy\": \"C:\\\\tools\\\\libimobiledevice\\\\iproxy.exe\"。", "iproxy executable not found: {executable}.\nWindows: install a trusted libimobiledevice build that includes iproxy.exe; then add its directory to PATH and verify with 'where iproxy', or configure the absolute executable path in asclient.json."),
    "iproxy_missing_macos": ("未找到 iproxy 可执行文件: {executable}。\nmacOS：安装 libimobiledevice（例如 'brew install libimobiledevice'）后执行 'which iproxy' 验证，或设置 tunnel.iproxy 为绝对路径。", "iproxy executable not found: {executable}.\nmacOS: install libimobiledevice (for example: 'brew install libimobiledevice') and verify with 'which iproxy', or set tunnel.iproxy to its absolute path."),
    "iproxy_missing_linux": ("未找到 iproxy 可执行文件: {executable}。\nLinux：安装发行版提供的 libimobiledevice 包后执行 'command -v iproxy' 验证，或设置 tunnel.iproxy 为绝对路径。", "iproxy executable not found: {executable}.\nLinux: install your distribution's libimobiledevice package and verify with 'command -v iproxy', or set tunnel.iproxy to its absolute path."),
    "tunnel_exited": ("USB 隧道意外退出：{detail}", "USB tunnel exited unexpectedly: {detail}"),
    "tunnel_route_exited": ("{route} 映射已退出（{address} -> 设备:{remote_port}）：{detail}", "{route} forwarding exited ({address} -> device:{remote_port}): {detail}"),
    "tunnel_executable_deprecated": ("参数 executable 已过时，请改用 iproxy；该别名将在后续版本移除。", "the 'executable' parameter is deprecated; use 'iproxy' instead. The alias will be removed in a future release."),
    "tunnel_config_conflict": ("executable 与 iproxy 不能同时使用；executable 已过时，请只使用 iproxy。", "'executable' and 'iproxy' cannot be combined; 'executable' is deprecated, use only 'iproxy'."),
    "tunnel_config_unknown": ("from_config 不支持的参数：{keys}。可用参数为 iproxy、local_port、remote_port、local_log_port、remote_log_port、forward_logs、udid、local_host、startup_timeout。", "unsupported from_config parameter(s): {keys}. Supported parameters are iproxy, local_port, remote_port, local_log_port, remote_log_port, forward_logs, udid, local_host, and startup_timeout."),
}


def t(key: str, /, **values: object) -> str:
    """Translate a short user-facing message for the active language."""
    try:
        template = _MESSAGES[key][0 if current_language() == "zh" else 1]
    except KeyError as exc:
        raise KeyError(f"unknown translation key: {key}") from exc
    return template.format(**values)
