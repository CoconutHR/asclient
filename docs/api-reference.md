# ASClient API 使用参考

本文对应 ASClient `0.5.1`。除非特别说明，所有调用均为同步调用，失败时抛出 `AScriptError` 的子类。生产接入说明见 [production-guide.md](production-guide.md)。

## 1. 快速选择接口

| 需求 | 推荐入口 |
| --- | --- |
| UI 控件查询、点击和输入 | `connect()` / `Device` |
| 截图、OCR、图色、项目文件、日志 | `AScriptClient` |
| 交互式检查控件树 | `py -m asclient inspect` |
| 临时调用已确认但未封装的设备端点 | `AScriptClient.request()` 或 CLI `api` |
| 设备端执行 Python | `eval_python()` 或 CLI `eval`，仅限受信任代码 |

```python
from asclient import AScriptClient, connect

client = AScriptClient("192.168.3.17:9096")
device = connect("192.168.3.17:9096")
```

## 2. 地址、连接与异常

### `AScriptClient(address, *, password="", timeout=15.0, retries=1)`

创建低层客户端。

| 参数 | 说明 |
| --- | --- |
| `address` | `HOST` 或 `HOST:PORT`；默认端口为 `9096` |
| `password` | 设备服务密码；客户端以 `airscript` Cookie 发送 |
| `timeout` | 单次 HTTP/WebSocket 建连超时，单位秒 |
| `retries` | 仅连接失败时的重试次数；HTTP 和业务错误不重试 |

```python
client = AScriptClient("192.168.3.17:9096", password="secret", timeout=20, retries=1)
print(client.base_url)  # http://192.168.3.17:9096
```

### `connect(address, **options) -> Device`

`AScriptClient` 的 UI 自动化门面。支持与构造函数相同的 `password`、`timeout`、`retries`。

```python
from asclient import connect
device = connect("192.168.3.17:9096", timeout=20)
```

### `AScriptTunnel`

推荐的 USB 隧道管理器。一次启动 AScript HTTP 服务的 `9096` 映射和日志 WebSocket 的 `10102` 映射，任一映射启动失败时会停止另一条映射。适用于电脑与手机不在同一网段的场景；完整流程见 [USB 隧道运维指南](usb-tunnel.md)。

```python
from asclient import AScriptTunnel, connect

with AScriptTunnel(udid="") as tunnel:
    device = connect(tunnel.address)
    print(tunnel.log_address)  # 127.0.0.1:10102
    print(device.client.status())
```

可配置 `local_port`、`remote_port`、`local_log_port`、`remote_log_port`、`udid`、`executable`、`local_host` 和 `startup_timeout`。`forward_logs=False` 只映射 HTTP 服务。`.address` 是 HTTP 客户端地址，`.log_address` 是日志地址或 `None`，`.start()` / `.stop()` 管理两条映射的生命周期。

### `IProxyTunnel`

管理一个由外部 `iproxy` 提供的 USB 端口转发。适用于电脑与手机不在同一网段的场景；完整流程见 [USB 隧道运维指南](usb-tunnel.md)。

```python
from asclient import IProxyTunnel, connect

with IProxyTunnel(local_port=9096, remote_port=9096, udid="") as tunnel:
    device = connect(tunnel.address)
    print(device.client.status())
```

`IProxyTunnel.start()` 启动隧道，`.stop()` 终止其子进程，`.address` 返回如 `127.0.0.1:9096` 的客户端地址。它要求外部 `iproxy` 已安装；找不到时抛 `IProxyNotFoundError`，启动失败时抛 `TunnelError`。

### 异常

| 异常 | 含义 | 常见处理 |
| --- | --- | --- |
| `DeviceConnectionError` | 无法连接设备服务 | 检查地址、Wi-Fi、端口和服务开关；可在用例边界有限重试 |
| `DeviceResponseError` | HTTP 错误、返回格式不正确 | 检查 `.status` 和 `.body`，保留证据；不要直接无条件重放 |
| `DeviceOperationError` | 设备端成功接收请求但业务执行失败 | 检查项目名、路径、页面状态或设备端日志 |
| `ProtocolError` | WebSocket 日志协议异常 | 检查 10102 端口和服务版本 |
| `ValueError` | 客户端参数非法 | 在本地修正参数；不会向设备发送请求 |

所有异常都继承 `AScriptError`，可以统一捕获：

```python
from asclient import AScriptError

try:
    client.screenshot()
except AScriptError as exc:
    print(f"AScript operation failed: {exc}")
```

## 3. 设备与屏幕 API

### `ping() -> str`

探测设备服务，返回平台字符串，目前为 `"iOS"` 或 `"Android"`。

```python
assert client.ping() == "iOS"
```

### `status() -> dict`

返回设备状态。iOS AScript 4001 的原始 `/api/status` 在部分设备上会报 `ObjCStrInstance` 错误；客户端会降级返回：

```python
{
    "available": True,
    "status_api_error": "...",  # 仅降级时存在
    "platform": "iOS",
    "screen": {"width": 393, "height": 852},
    "current_app": {"bundle_id": "..."},
}
```

不要把所有 status 字段作为跨版本契约。只需将 `available`、`screen` 和 `current_app` 用作连通性与环境记录。

### `scan_subnet(*, workers=64, probe_timeout=1.0) -> list[tuple[DeviceAddress, str]]`

在传入 IPv4 地址所在 `/24` 局域网内探测同端口设备。仅用于受控局域网的设备发现，不支持 IPv6 或任意 CIDR 扫描。

```python
for address, platform in client.scan_subnet():
    print(address, platform)
```

### `current_app() -> dict`

返回当前 App 信息，通常含 `name`、`bundle_id`、`pid`。

```python
app = client.current_app()
assert app["bundle_id"] == "com.example.app"
```

### `packages() -> list`

返回设备端 Python 包列表，通常为 `[名称, 版本]` 对的列表。设备 status 未提供包信息时，客户端会调用设备端 Python 查询；因此该接口需要服务端允许执行该内部查询。

```python
for name, version in client.packages():
    print(name, version)
```

### `screenshot() -> bytes` 与 `save_screenshot(destination) -> Path`

获取 PNG 截图或保存到本地。`save_screenshot` 会自动创建父目录并返回绝对路径。

```python
png = client.screenshot()
artifact = client.save_screenshot("artifacts/current.png")
```

### `capture_artifacts(destination, *, prefix="failure", mode="smart") -> dict[str, Path]`

保存诊断截图、XML 和状态 JSON。每一项独立采集；个别操作失败时，错误会写入 JSON 的 `errors` 字段，其他证据仍会保留。

```python
artifacts = client.capture_artifacts("artifacts/run-42", prefix="login-failed")
print(artifacts)  # screenshot/xml/context 中实际成功写入的路径
```

## 4. 控件树 API

### `ui_xml(*, mode="smart", depth=0, x=0, y=0) -> str`

读取原始 XML 控件树。

| 参数 | 说明 |
| --- | --- |
| `mode` | `smart`、`full`、`point` 或设备端支持的模式；默认 `smart` |
| `depth` | 最大深度；`0` 表示使用服务端默认行为 |
| `x`, `y` | `point` 模式坐标；其他模式通常为 `0` |

```python
xml = client.ui_xml(mode="smart")
Path("artifacts/tree.xml").write_text(xml, encoding="utf-8")
```

### `ui_tree(*, mode="smart", selector=None, x=0, y=0) -> dict`

读取结构化树；无 `selector` 时一般返回：

```python
{
    "config": {"display": {"widthPixels": 393, "heightPixels": 852}, ...},
    "views": [{"type": "XCUIElementTypeApplication", "childs": [...]}],
}
```

传入 `selector` 时，服务端返回匹配元素的扁平 `views`。此参数是设备端协议格式，业务代码优先使用 `Device`/`Selector`，不要手写该 JSON。

### `find_elements(selector, *, mode="smart", x=0, y=0) -> list[dict]`

执行已序列化选择器并返回元素元数据。仅在你需要对接已有 AScript selector JSON 时使用。

```python
elements = client.find_elements({
    "sel": [{"key": "name", "params": "login_button"}],
    "find": 99999,
})
```

常见元素字段：`type`、`name`、`label`、`value`、`enabled`、`selected`、`focused`、`visible`、`index`、`traits`、`x`、`y`、`width`、`height`。字段取决于目标 App 的可访问性实现。

## 5. uiautomator2 风格对象 API

### `Device`

`connect()` 返回 `Device`。两种选取写法等价：

```python
button = device(name="login_button")
button = device.selector().name("login_button")
```

`device(**attributes)` 支持别名：

| 简写 | 对应节点字段 |
| --- | --- |
| `text` | `label` |
| `resource_id` | `name` |
| `description` | `name` |
| `class_name` | `type` |

支持的实际字段：`name`、`label`、`value`、`title`、`type`、`enabled`、`selected`、`focused`、`visible`、`index`、`traits`、`childCount`。传入其他字段会抛出 `ValueError`。

#### `selector(*, mode="smart", **attributes) -> Selector`

构建不可变 selector，不访问设备。`mode` 典型值为 `smart` 或 `full`。

```python
selector = device.selector(mode="full", name="settings").visible(True)
```

#### `find_all(selector) -> list[UiObject]`

立即查询并返回全部匹配元素。

#### `find(selector, *, timeout=0) -> UiObject | None`

轮询直到出现第一个元素或超时。`timeout` 单位为秒。

```python
selector = device.selector().text("登录")
element = device.find(selector, timeout=8)
if element is None:
    raise RuntimeError("login button not found")
```

#### `dump_hierarchy(*, mode="smart") -> str`

等价于 `client.ui_xml()`，用于保存诊断 XML。

#### `screenshot(destination=None) -> bytes | Path`

未传 `destination` 时返回 PNG bytes；传入本地路径时保存并返回 `Path`。

### `Selector`

`Selector` 是不可变对象。每次链式调用都返回一个新 selector，可以安全复用：

```python
base = device.selector().type("XCUIElementTypeButton")
login = base.name("login_button")
cancel = base.name("cancel_button")
```

| 方法 | 含义 |
| --- | --- |
| `.name(value, contains=False)` | 按 accessibility name 精确或包含匹配 |
| `.label(value, contains=False)` | 按 label 匹配 |
| `.text(value, contains=False)` | `.label()` 别名 |
| `.value(value, contains=False)` | 按 value 匹配 |
| `.type(value)` | 按 `XCUIElementType...` 类型匹配 |
| `.enabled(value=True)` | 按 enabled 状态匹配 |
| `.visible(value=True)` | 按 visible 状态匹配 |
| `.selected(value=True)` | 按 selected 状态匹配 |
| `.index(value)` | 按节点 index 匹配；不建议作为唯一生产定位条件 |
| `.at(x, y)` | 使用 point 模式从坐标处探测元素 |
| `.full()` | 使用 full 模式重新构建 selector |
| `.title(value, contains=False)` | 按 title 匹配 |
| `.focused(value=True)` | 按焦点状态匹配 |
| `.traits(value)` | 按可访问性 traits 位匹配 |
| `.child_count(value)` | 按直接子节点数匹配 |
| `.with_limits(max_depth=0, max_children=30)` | 设置服务端树查询上限；`0` 交由服务端按不限处理 |
| `.payload(find=99999)` | 返回设备端 selector JSON；仅调试/互操作使用 |
| `.code()` | 返回可读的 Python 选择器代码字符串 |

`contains=True` 映射到设备端包含匹配。它适合文案带动态后缀的场景，但必须通过 `.count` 确认不会命中多个元素。

### `UiCollection`

`device(...)` 返回 `UiCollection`。属性访问会发起新的设备查询，不会缓存旧页面节点。

| 成员 | 返回/行为 |
| --- | --- |
| `.exists` | `bool`，至少匹配一个节点时为真 |
| `.count` | 匹配元素数量 |
| `.info` | 首个元素 `info`；无匹配时抛 `LookupError` |
| `.all()` | `list[UiObject]` |
| `.get(timeout=0)` | 首个元素或 `None` |
| `.wait_gone(timeout=10)` | 元素消失前轮询；成功返回 `True`，超时返回 `False` |
| `.click()` | 点击首个元素中心；无匹配时抛 `LookupError` |
| `.click_exists(timeout=0)` | 找到即点击并返回 `True`；未找到返回 `False` |
| `.set_text(text, interval_ms=120)` | 点击首个元素后输入文本 |

```python
submit = device(name="checkout_submit")
assert submit.count == 1, f"unexpected submit count: {submit.count}"
submit.click()
```

### `UiObject`

`UiCollection.get()` 或 `.all()` 返回 `UiObject`。

| 成员 | 返回/行为 |
| --- | --- |
| `.info` | 当前查询获得的只读元素字典 |
| `.rect` | `{"x", "y", "width", "height"}` 浮点字典 |
| `.center` | `(x, y)` 矩形中心坐标 |
| `.exists` | 始终为 `True`；对象代表已经解析到的元素 |
| `.click()` | 以中心坐标点击 |
| `.set_text(text, interval_ms=120)` | 先点选再输入 |

`UiObject` 是快照，不会在页面跳转后自动重新定位。页面发生变化后请重新使用 `UiCollection.get()` 或 `device.find()` 查询。

## 6. 可靠执行与工件

### `Run(device, artifacts_root="artifacts", run_id=None)`

为一个 `Device` 或 `AScriptClient` 创建可追溯的运行上下文。每个 Run 创建独立目录：

```text
artifacts/
  20260827_002000_a1b2c3d4/
    manifest.json
    login_failure.png
    login_failure.xml
    login_failure.json
```

```python
from asclient import Run, connect

device = connect("192.168.3.17:9096")
with Run(device, artifacts_root="artifacts") as run:
    login = run.assert_unique(device.selector().name("login_button"))
    run.step("open_login", login.click, capture_before=True, capture_after=True)
    run.wait(device.selector().name("home_screen"), timeout=10, name="wait_home")
```

| 成员 | 行为 |
| --- | --- |
| `step(name, action, capture_before=False, capture_after=False)` | 在设备锁内执行 action，记录耗时与结果；失败时自动采集诊断证据后重新抛出原异常 |
| `capture(label, mode="smart")` | 立即采集截图、XML 和设备上下文 |
| `wait(selector, timeout=10, name="wait")` | 等待元素出现；超时自动采集失败证据 |
| `assert_unique(selector, name="assert_unique")` | 断言 selector 恰好命中一个元素，并返回该元素 |

`manifest.json` 会在每个步骤结束后更新，记录运行 ID、设备地址、开始/结束时间、步骤耗时、结果、异常和实际写入的证据路径。

### `AScriptClient.locked()`

返回同一 Python 进程内、按设备 `HOST:PORT` 共享的可重入互斥上下文。`tap`、`swipe`、`input_text`、`home`、`eval_python`、项目运行/部署、文件写入和删除操作已自动使用它；通常不需要手动调用。

该锁不跨 Python 进程、CI runner 或物理主机。多进程并行执行同一手机时，仍需由 CI 队列、文件锁或外部调度器保证每台设备同时只有一个作业。

## 7. 交互动作与设备端代码

### `tap(x, y, *, duration_ms=20)`

点击 AScript 坐标。坐标单位必须以当前目标设备真机验证；不要直接将 OCR 的物理像素坐标假定为 tap 坐标。

```python
client.tap(200, 600)
```

### `swipe(x1, y1, x2, y2, *, duration_ms=200)`

从起点滑动到终点。

```python
client.swipe(200, 700, 200, 250, duration_ms=350)
```

### `input_text(text, *, interval_ms=120)`

向当前焦点输入文本。应先用 `UiCollection.click()` 或 `UiObject.click()` 取得输入焦点。

### `home()`

执行设备端 Home 动作。

### `eval_python(code, *, image="") -> Any`

在设备端执行 Python 代码并返回 `_result`。代码为空会抛 `ValueError`；服务端返回 JSON 字符串时客户端会自动解析。

```python
result = client.eval_python("_result = 1 + 1")
assert result == 2
```

这是高风险维护接口。禁止将不可信输入拼接到 `code` 中，也不要将其暴露为 Web/CI 参数。

## 8. OCR 与图色 API

### `ocr(rect=None) -> Any`

执行设备端 OCR。`rect` 为设备端图色参数格式，不是 Python 元组。

```python
result = client.ocr()
for item in result.get("data", []):
    print(item["text"], item["rect"], item["confidence"])
```

### `find_colors(colors, *, diff=0.98) -> Any`

查找颜色组合。`colors` 是 AScript 图色工具接受的字符串表达式，`diff` 为相似度。

```python
result = client.find_colors("#FFFFFF,0|10|#000000", diff=0.98)
```

### `compare_colors(colors, *, diff=0.9) -> Any`

比对指定颜色组合，返回设备端工具结果。

### `gp(class_id, params, *, image=None, name="asclient") -> Any`

通用图色工具调用。`ocr`、`find_colors`、`compare_colors` 都是其高层封装。仅在掌握设备端图色类和参数协议时使用。

| 参数 | 说明 |
| --- | --- |
| `class_id` | 图色工具类，例如 OCR 工具类 |
| `params` | 设备端参数字符串 |
| `image` | 设备端图片路径；留空时自动先请求当前截图路径 |
| `name` | 任务名称，默认 `asclient` |

## 9. 项目与远程文件 API

项目名必须是一个单独目录名，不能包含 `/`、`\\`、`.` 或 `..`。相对远程路径同样禁止父目录逃逸。

### 项目管理

| 方法 | 说明 |
| --- | --- |
| `projects() -> list[dict]` | 列出设备项目 |
| `create_project(name)` | 创建项目；已存在时设备可能返回业务错误 |
| `rename_project(name, new_name)` | 重命名项目 |
| `remove_project(name)` | 删除项目，具有破坏性 |
| `project_files(name) -> Any` | 返回设备端项目树原始数据 |
| `run_project(name)` | 执行项目的 `__init__.py` |
| `stop_project()` | 停止当前项目 |

```python
client.create_project("smoke")
client.run_project("smoke")
client.stop_project()
```

### 文件读写

| 方法 | 说明 |
| --- | --- |
| `read_file(remote_path) -> bytes` | 读取设备路径的原始 bytes |
| `save_text(remote_path, content)` | 保存 UTF-8 文本到设备端路径 |
| `create_remote(parent, name, directory=False)` | 创建设备端文件或目录；`directory=True` 创建目录 |
| `remove_remote(path)` | 删除设备端路径，具有破坏性 |

```python
client.save_text("~/modules/demo/config.json", '{"debug": false}')
raw = client.read_file("~/modules/demo/config.json")
```

### 上传、下载与部署

| 方法 | 返回 | 说明 |
| --- | --- | --- |
| `upload_file(project, local_path, remote_path=None)` | `None` | 上传单个文件；必要时先创建项目 |
| `upload_tree(project, directory)` | `int` | 递归上传目录并返回文件数 |
| `download_project(project, destination)` | `list[Path]` | 按设备返回的树下载实际文件 |
| `deploy(project, entry_file, log_seconds=5.0)` | `(list[LogEntry], bytes)` | 上传为 `__init__.py`、运行、收集一段日志并返回截图 |

```python
logs, screenshot = client.deploy("smoke", "smoke.py", log_seconds=5)
Path("artifacts/deploy.png").write_bytes(screenshot)
for entry in logs:
    print(entry.timestamp, entry.kind, entry.message)
```

`deploy()` 会改变设备项目内容和运行状态，禁止对生产业务项目使用临时项目名覆盖。

## 10. 日志 API

### `logs(*, duration=None, stop_event=None, reconnects=0, reconnect_delay=1.0) -> Iterator[LogEntry]`

连接设备 `10102` WebSocket 日志端点。`duration=None` 时会持续迭代，直到服务端关闭或 `stop_event` 被设置。

```python
for entry in client.logs(duration=10):
    print(f"[{entry.kind}] {entry.timestamp} {entry.message}")
```

`LogEntry` 字段：

| 字段 | 说明 |
| --- | --- |
| `message` | 日志文本 |
| `kind` | 服务端日志类别，例如 `i`、`o` |
| `timestamp` | 服务端时间字符串 |

不要在 Web 线程或请求处理线程中无期限迭代日志；应设置 `duration` 或传入可取消的 `threading.Event`。

`reconnects` 表示日志服务意外关闭或网络连接失败后的最大重连次数；`duration` 是包含重连等待在内的总时限。

### `save_logs(destination, *, duration=None, reconnects=0) -> int`

将日志写成 UTF-8 JSON Lines，返回写入事件数。

### `wait_for_log(pattern, *, timeout=10, regex=False, reconnects=1) -> LogEntry | None`

等待首条包含 `pattern` 的日志；`regex=True` 时将 pattern 视为正则表达式。适合部署后等待明确的 `READY` 标记，不应替代业务页面断言。

## 11. 原始 HTTP API

### `request(method, path, *, params=None, form=None, data=None, headers=None, timeout=None) -> bytes`

发送原始 HTTP 请求。`path` 必须以 `/` 开头；`form` 与 `data` 互斥。返回原始 response body。

```python
raw = client.request("GET", "/api/node/package")
```

### `json(method, path, **kwargs) -> dict`

等同 `request()` 后解析 JSON object。返回不是 JSON object 时抛 `DeviceResponseError`。

```python
payload = client.json("GET", "/api/node/package")
```

CLI 透传命令：

```bat
py -m asclient --yes --device 192.168.3.17:9096 api GET /api/node/package
py -m asclient --yes --device 192.168.3.17:9096 api GET /api/node/dump --params "{\"mode\": \"full\"}"
```

原始接口不是稳定兼容层。使用前应在目标 AScript 版本上验证，并在应用代码中封装、测试和记录端点版本。

## 12. CLI 参考

默认从当前目录的 `asclient.json` 读取连接配置。可复制根目录的 `asclient.example.json` 后填写设备信息；命令行参数只用于单次覆盖。所有命令共用：

```bat
py -m asclient [--config FILE] [--device HOST:PORT] [--password PASSWORD] [--timeout SECONDS] <command>
```

任何改变设备状态的命令必须在命令前加 `--yes`。这是非交互式确认，便于脚本审计：

```bat
py -m asclient --yes deploy smoke .\smoke.py --logs 5
py -m asclient --yes remove smoke
```

不带 `--yes` 时客户端在发送请求前失败，并打印目标设备和被拒绝的动作。Python 库 API 不会重复要求确认，因为调用者代码本身已是显式授权边界。

| 命令 | 用法 | 作用 |
| --- | --- | --- |
| `ping` | `ping` | 探测服务 |
| `status` | `status` | 输出设备状态或兼容降级状态 |
| `scan` | `scan` | 扫描默认地址所在 `/24` |
| `pkgs` | `pkgs` | 列出设备 Python 包 |
| `app` | `app` | 当前 App 信息 |
| `shot` | `shot [output.png]` | 保存截图 |
| `dump` | `dump [output.xml] [--mode MODE]` | 保存 XML 控件树 |
| `observe` | `observe [--prefix PREFIX]` | 同时保存截图与 XML |
| `inspect` | `inspect [--host HOST] [--port PORT] [--no-browser]` | 启动本机 Inspector |
| `tunnel` | `tunnel [--local-port PORT] [--remote-port PORT] [--local-log-port PORT] [--remote-log-port PORT] [--no-logs] [--udid UDID] [--iproxy PATH]` | 以前台方式同时管理 HTTP 与日志 USB `iproxy` 隧道；`--no-logs` 仅映射 HTTP |
| `tap` | `--yes tap X Y [--duration MS]` | 坐标点击 |
| `swipe` | `--yes swipe X1 Y1 X2 Y2 [--duration MS]` | 坐标滑动 |
| `input` | `--yes input TEXT [--interval MS]` | 输入文本 |
| `home` | `--yes home` | Home 动作 |
| `ocr` | `ocr [rect]` | OCR |
| `findcolor` | `findcolor COLORS [--diff FLOAT]` | 查找颜色 |
| `compare` | `compare COLORS [--diff FLOAT]` | 比对颜色 |
| `ls` | `ls` | 列项目 |
| `create` | `--yes create PROJECT` | 创建项目 |
| `rename` | `--yes rename PROJECT NEW_NAME` | 重命名项目 |
| `remove` | `--yes remove PROJECT` | 删除项目 |
| `files` | `files PROJECT` | 查看项目文件树 |
| `push` | `--yes push PROJECT SOURCE [REMOTE]` | 上传单文件或目录 |
| `pull` | `pull PROJECT [OUTPUT]` | 下载项目文件 |
| `run` / `stop` | `--yes run PROJECT` / `--yes stop` | 启动/停止项目 |
| `deploy` | `--yes deploy PROJECT ENTRY [--logs SECONDS] [--screenshot FILE]` | 上传、运行、取日志和截图 |
| `log` | `log [SECONDS] [--reconnects N] [--output FILE] [--contains TEXT]` | 输出、筛选或 JSONL 落盘实时日志 |
| `cat` | `cat REMOTE_PATH [OUTPUT]` | 打印或保存远程文件 |
| `eval` | `--yes eval CODE` | 执行受信任设备端 Python |
| `api` | `--yes api METHOD PATH [--params JSON] [--form JSON]` | 原始端点透传 |

`remove`、`run`、`stop`、`deploy`、`push`、`eval`、`tap`、`swipe`、`input`、`home` 和原始 `api` 都要求 `--yes`，因为 AScript 的原始 GET 路由也可能改变状态。将它们放在明确的运维或测试步骤中，避免作为排错时的随手命令。

## 13. Inspector API

CLI `inspect` 使用 [inspector.py](../asclient/inspector.py) 的公开函数：

```python
from asclient import AScriptClient
from asclient.inspector import serve, run_forever

client = AScriptClient("192.168.3.17:9096")
server = serve(client, host="127.0.0.1", port=0, open_browser=True)
print(server.server_port)

# 或阻塞运行到 Ctrl+C。
run_forever(client, host="127.0.0.1", port=0)
```

| 函数 | 说明 |
| --- | --- |
| `serve(client, host="127.0.0.1", port=0, open_browser=True)` | 创建但不启动 `ThreadingHTTPServer`；`port=0` 自动选端口 |
| `run_forever(...) -> str` | 创建并阻塞运行，Ctrl+C 后关闭；返回本地 URL |

Inspector 只应绑定 `127.0.0.1`。它在顶部显示当前 App 的名称、Bundle ID、PID 和当前树节点数量，并提供当前页面截图和节点信息。树、截图和属性面板之间的两条分隔线可拖动调整宽度；中间截图始终按原始宽高比缩放，不会被拉伸。不需要、也不应作为局域网服务使用。

CLI 启动时会打印实际 Inspector URL。浏览器在刷新或关闭页面时取消正在传输的快照属于正常情况，客户端会静默结束该响应，不会影响设备端状态或打印终端堆栈。

页面中的 **Verify selector** 按钮会以只读方式查询当前候选选择器的实际匹配数。只有结果为 `1` 时，才应将其作为代码候选；该按钮不会点击或修改设备状态。
