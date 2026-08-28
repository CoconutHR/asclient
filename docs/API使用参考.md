# ASClient API 使用参考

首次使用请先阅读[从零开始使用教程](从零开始使用教程.md)：它按安装、Wi-Fi/USB 连接、Inspector、首个程序、排错和功能示例组织；本文档专注于完整 API 参数、返回值和异常。按任务查找一行写法可先看仓库 [README](../README.md) 的“任务速查”。

本文对应 ASClient `0.8.0`。除非特别说明，所有调用均为同步调用，失败时抛出 `AScriptError` 的子类。生产接入说明见 [生产使用指南](生产使用指南.md)。

## 1. 快速选择接口

| 需求 | 推荐入口 |
| --- | --- |
| UI 控件查询、点击和输入 | `connect()` / `Device` |
| 截图、OCR、图色、项目文件、日志 | `AScriptClient` |
| 交互式检查控件树 | `py -m asclient inspect` |
| 诊断本机、USB 与设备服务 | `py -m asclient doctor` |
| 临时调用已确认但未封装的设备端点 | `AScriptClient.request()` 或 CLI `api` |
| 设备端执行 Python | `eval_python()` 或 CLI `eval`，仅限受信任代码 |

```python
from asclient import AScriptClient, connect

client = AScriptClient("192.168.3.17:9096")
device = connect("192.168.3.17:9096")
```

## 2. 地址、连接与异常

### `AScriptClient(address, *, password="", timeout=15.0, retries=1, coordinate_cache_ttl=1.0)`

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

推荐的 USB 隧道管理器。一次启动 AScript HTTP 服务的 `9096` 映射和日志 WebSocket 的 `10102` 映射，任一映射启动失败时会停止另一条映射。适用于电脑与手机不在同一网段的场景；完整流程见 [USB 隧道运维指南](USB隧道运维指南.md)。

```python
from asclient import AScriptTunnel, connect

with AScriptTunnel.from_config() as tunnel:   # 读取 asclient.json 的 tunnel 段
    device = connect(tunnel.address)
    print(tunnel.log_address)  # 127.0.0.1:10102
    print(device.client.status())
```

#### `from_config(path=None, **overrides) -> AScriptTunnel`

读取 `asclient.json`（或 `path` 指定的配置文件）的 `tunnel` 段创建隧道；配置文件不存在时退回内置默认值。优先级为“显式参数 > 配置文件 > 内置默认值”，参数名与配置键一致：`iproxy`、`local_port`、`remote_port`、`local_log_port`、`remote_log_port`、`forward_logs`、`udid`、`local_host`、`startup_timeout`。过时别名 `executable` 等效 `iproxy`，使用时发出 `DeprecationWarning`，将在后续版本移除；与 `iproxy` 同时使用或传入未知参数抛出 `ValueError`。

```python
# 配置存了设备 A 的 udid 与 iproxy 路径；临时切换设备 B，其余沿用配置。
with AScriptTunnel.from_config(udid="设备B的UDID") as tunnel:
    device = connect(tunnel.address)
```

直接构造 `AScriptTunnel(...)` 不读取配置文件，字段名也与配置键不同（`executable` 对应 `iproxy`），仅用于完全显式控制的场景。可配置 `local_port`、`remote_port`、`local_log_port`、`remote_log_port`、`udid`、`executable`、`local_host` 和 `startup_timeout`。`forward_logs=False` 只映射 HTTP 服务。`.address` 是 HTTP 客户端地址，`.log_address` 是日志地址或 `None`，`.start()` / `.stop()` 管理两条映射的生命周期。

### `IProxyTunnel`

管理一个由外部 `iproxy` 提供的 USB 端口转发。适用于电脑与手机不在同一网段的场景；完整流程见 [USB 隧道运维指南](USB隧道运维指南.md)。

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
    "health": "degraded",
    "status_api_error": "...",  # 仅降级时存在
    "compatibility": {
        "status_api": {"state": "degraded", "issue": "ios_objc_property_callable"},
        "capabilities": {"screen": "available", "current_app": "available"}
    },
    "platform": "iOS",
    "logical_screen": {"width": 393, "height": 852},
    "screen": {"width": 1179, "height": 2556},
    "current_app": {"bundle_id": "..."},
}
```

`health: "degraded"` 表示客户端发现设备端 `/api/status` 的兼容性问题，但已成功执行降级探测；不是设备不可用。`compatibility.capabilities` 明确列出屏幕和当前应用信息是否成功取得。不要把所有 status 字段作为跨版本契约。

### `doctor` 命令

```bat
py -m asclient doctor
py -m asclient doctor --report artifacts\doctor.json
py -m asclient doctor --fix-iproxy "D:\\tools\\libimobiledevice\\iproxy.exe"
```

诊断会检查 `iproxy`、本地 USB 映射端口、设备 HTTP 服务、`/api/status` 兼容降级和日志端口。默认只读。它从不自动安装第三方二进制、停止其他程序或修改手机。唯一内置修复是验证用户提供的 `iproxy` 文件并写入 `asclient.json` 的 `tunnel.iproxy`；写入前需要交互确认，也可加 `--yes` 供已审查的脚本使用。`--report` 写出不含密码的 JSON 证据文件。

### 语言选择

CLI 默认按操作系统语言输出：中文系统为中文，其他系统为英文。配置文件顶层可设置 `"language": "auto"`、`"zh-CN"` 或 `"en"`；单次命令可用 `--lang zh-CN` 或 `--lang en` 覆盖。运行 `py -m asclient help` 获取中文友好命令速查，`py -m asclient help doctor` 可查看单个命令。

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

### `screenshot_crop_relative(left, top, right, bottom) -> bytes`

抓取截图后按比例裁剪。四个参数均为 `0.0..1.0`，矩形为左上包含、右下排除；必须满足 `left < right`、`top < bottom`。`save_screenshot_crop_relative(destination, left, top, right, bottom)` 会直接保存裁剪结果并返回绝对路径。

```python
# 取得下半屏 PNG 字节或直接保存；比例矩形。
bottom = client.screenshot_crop_relative(0, 0.5, 1, 1)
client.save_screenshot_crop_relative("artifacts/bottom.png", 0, 0.5, 1, 1)

# 物理像素矩形：left, top, right, bottom。
button = client.screenshot_crop(0, 1800, 1179, 2556)
client.save_screenshot_crop("artifacts/bottom.png", 0, 1800, 1179, 2556)
```

实现无第三方依赖，支持移动端截图使用的非隔行 8 位 RGB/RGBA PNG；非标准 PNG 会抛出 `DeviceResponseError`。

### `capture_frame()`、`pixel()` 与 `pixels()`

`capture_frame()` 抓取一张物理像素截图，返回 `ScreenFrame`。同一帧可完成多个取色或找图操作，不会重复抓屏。`PixelColor` 为强类型 RGBA 颜色，默认以 `.rgb` 返回三元组，也可用 `.hex` 获取大写十六进制字符串。

```python
frame = client.capture_frame()
color = frame.pixel(100, 200)             # 绝对物理像素
print(color.rgb)                          # (255, 128, 0)
print(color.hex)                          # "#FF8000"

# 比例坐标：屏幕宽度 50%、高度 92%。
color = frame.pixel_relative(0.5, 0.92)
colors = frame.pixels([(100, 200), (300, 400)])
colors = frame.pixels_relative([(0.1, 0.1), (0.9, 0.9)])
```

`client.pixel()`、`client.pixel_relative()`、`client.pixels()` 和 `client.pixels_relative()` 是便利入口，各自抓取一张新帧；需要多次检查时优先使用同一个 `frame`。绝对点必须在截图边界内，比例坐标范围为 `0..1`，`1.0` 会夹紧到最后一个有效物理像素。

### `find_image()`、`find_images()`、`find_any_image()` 与 `wait_any_image()`

在当前截图中匹配本机模板 PNG/JPEG。`template` 可为本地文件路径或图像字节；返回 `ImageMatch(x, y, width, height, confidence)`，所有结果坐标均为实际物理像素。`confidence` 范围为 `(0, 1]`，值越高要求越接近；默认 `0.9`。

单模板和所有图像等待/点击/滚动 API 都可用物理像素 `region=(left, top, right, bottom)` 或比例 `region_relative=(left, top, right, bottom)` 限制搜索区域，两者不能同时传入。多模板接口的 `regions`、`regions_relative` 为按模板名称映射的同类区域；每张模板可有自己的小区域。旧 `region_pixels` / `regions_pixels` 是物理像素弃用别名，会发出 `DeprecationWarning`。旧版比例 `region` 仅在包含非整数时暂时兼容并发出警告；尤其旧全屏写法 `region=(0, 0, 1, 1)` 必须改为 `region_relative=(0, 0, 1, 1)`，因为新规则下它表示 1×1 物理像素区域。

```python
match = client.find_image("assets/login-icon.png", confidence=0.95)
if match:
    print(match.center)

# 绝对物理像素区域：只搜索底部区域。
match = client.find_image("assets/login-icon.png", region=(0, 1800, 1179, 2556))

# 一帧截图匹配多个模板；每个模板使用自己的比例检测区域。
matches = client.find_images(
    {"success": "assets/success.png", "retry": "assets/retry.png"},
    confidence=0.95,
    regions_relative={"success": (0, 0.2, 1, 0.8), "retry": (0, 0.7, 1, 1)},
)

# 等待成功页或错误页任一出现；返回 (名称, ImageMatch)。
name, match = client.wait_any_image(
    {"success": "assets/success.png", "failure": "assets/failure.png"},
    timeout=20,
)

match = client.wait_image("assets/login-icon.png", confidence=0.95, timeout=15, interval=0.5, log=True)
client.tap(*match.center)
client.wait_image_gone("assets/loading.png", confidence=0.90, timeout=20, log=True)

# 等待后自动点击模板中心。
client.tap_image("assets/continue.png", confidence=0.93, timeout=10)
```

`wait_image()` 与 `wait_image_gone()` 默认先等待一个 `interval`，再进行第一次探测；传入 `initial_delay=False` 可改为立即探测，且首次等待时间仍计入总 `timeout`。

### `scroll_until_image()`

每次匹配失败后沿指定方向滑动，直到模板出现。`direction` 支持 `down`（默认）、`up`、`left`、`right`，也兼容 `下`、`上`、`左`、`右`；其含义是手势移动方向。默认先等待一个 `interval`，再进行首次匹配；传入 `initial_delay=False` 可立即开始。`timeout` 是整个操作的最大时长，默认 20 秒；`max_swipes` 默认 10 次。两项上限任一先到即停止并抛出 `TimeoutError`。`duration_ms` 为每次滑动时长。`log=False` 默认不输出；设为 `True` 会在本机终端打印每次匹配结果、继续滑动或停止的原因。

要完全控制轨迹，传入 `swipe_relative=(x1_ratio, y1_ratio, x2_ratio, y2_ratio)`，其语义和 `swipe_relative(x1_ratio, y1_ratio, x2_ratio, y2_ratio, duration_ms=...)` 相同。元组必须恰好有四项；提供后会覆盖 `direction` 的默认轨迹。也兼容四个独立的比例参数，但不能与该元组同时使用。

```python
target = client.scroll_until_image(
    "assets/target.png", direction="up", confidence=0.95, timeout=30, max_swipes=8,
    region_relative=(0, 0.15, 1, 0.95), log=True,
)
client.tap(*target.center)

# 自定义从 70%,75% 滑到 35%,25%，每次 650 ms。
client.scroll_until_image("assets/target.png", swipe_relative=(0.7, 0.75, 0.35, 0.25), duration=0.65)
```

模板匹配依赖 Pillow，随 `asclient` 一起安装。`wait_image()` 与 `wait_image_gone()` 的 `log=False` 默认静默；设为 `True` 会在本机终端逐轮输出匹配状态。它是视觉定位降级方案：应优先使用唯一的语义选择器；模板必须在同一分辨率和界面缩放条件下采集。

### `capture_artifacts(destination, *, prefix="failure", mode="smart") -> dict[str, Path]`

保存诊断截图、XML 和状态 JSON。每一项独立采集；个别操作失败时，错误会写入 JSON 的 `errors` 字段，其他证据仍会保留。

```python
artifacts = client.capture_artifacts("artifacts/run-42", prefix="login-failed")
print(artifacts)  # screenshot/xml/context 中实际成功写入的路径
```

## 4. 控件树 API

### `ui_xml(*, mode="smart", depth=0, x=0, y=0) -> str`

读取已归一化为物理像素坐标的 XML 控件树。

| 参数 | 说明 |
| --- | --- |
| `mode` | `smart`、`full`、`point` 或设备端支持的模式；默认 `smart` |
| `depth` | 最大深度；`0` 表示使用服务端默认行为 |
| `x`, `y` | `point` 模式坐标；其他模式通常为 `0` |

```python
xml = client.ui_xml(mode="smart")
Path("artifacts/tree.xml").write_text(xml, encoding="utf-8")
```

### `ui_tree(*, mode="smart", selector=None, x=0, y=0, normalize=True) -> dict`

读取结构化树；无 `selector` 时一般返回：

```python
{
    "config": {"display": {"widthPixels": 1179, "heightPixels": 2556}, ...},
    "views": [{"type": "XCUIElementTypeApplication", "childs": [...]}],
}
```

传入 `selector` 时，服务端返回匹配元素的扁平 `views`。此参数是设备端协议格式，业务代码优先使用 `Device`/`Selector`，不要手写该 JSON。

坐标空间（逻辑尺寸 + 截图物理尺寸）按 `coordinate_cache_ttl`（默认 1 秒）短时缓存，轮询查询从每轮 3 次设备往返降到 1 次；旋转屏幕会改变物理尺寸，旋转敏感的流程可设 `coordinate_cache_ttl=0` 关闭缓存。`normalize=False` 跳过坐标空间探测与归一化，整个查询只有一次树请求，返回的节点坐标为设备端逻辑点，适合存在性检查；此模式不能使用点探测参数。

### `find_elements(selector, *, mode="smart", x=0, y=0, normalize=True) -> list[dict]`

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

#### `find_all(selector, *, normalize=True) -> list[UiObject]`

立即查询并返回全部匹配元素。`normalize=False` 走快速路径：整个查询只需一次树请求（省去屏幕尺寸与截图两次往返），节点坐标为设备端逻辑点，适合存在性检查；此时不能使用点探测 selector，也不应点击返回的元素。

#### `scroll_until_element(selectors, *, direction="down", swipe_relative=None, x1_ratio=None, y1_ratio=None, x2_ratio=None, y2_ratio=None, timeout=20, interval=0.5, max_swipes=10, duration=None, duration_ms=None, log=False, initial_delay=True)`

沿 `direction` 手势方向滑动，直到语义控件出现。每轮只读取一次完整控件树并在本地匹配全部候选。传入单个 `Selector` 返回 `UiObject`；传入 `{名称: Selector}` 映射返回 `(命中名称, UiObject)`。`direction` 支持 `down`（默认）/`up`/`left`/`right` 及中文，含义与 `scroll_until_image` 相同；`swipe_relative` 元组可完全自定义轨迹并覆盖 `direction`。`duration` 为每次滑动的秒数；超时或滑动次数用尽抛 `LookupError`。

```python
target = device.scroll_until_element(device.selector().name("checkout_button"), direction="up", timeout=30)
target.click()

name, element = device.scroll_until_element(
    {"成功": device.selector().name("order_success"), "售罄": device.selector().text("已售罄")},
    max_swipes=8,
)
```

#### `watch(*rules, interval=2.0, log=False) -> Watcher`

启动后台轮询监控：每 `interval` 秒读取一次完整控件树，规则命中时在设备锁内执行动作。规则可直接传 `Selector`（等价于命中即点击），或传 `WatchRule(selector, action, max_triggers, name)` 自定义动作（`"click"` 或接收 `UiObject` 的可调用对象）与触发次数上限。

```python
# 权限弹窗自动点击；with 结束自动停止。
with device.watch(device.selector().text("允许"), interval=1.5):
    run_login_flow()

# 只记录不动作、限制触发次数。
with device.watch(
    WatchRule(device.selector().name("ad_close"), action="click", max_triggers=3, name="关广告"),
    interval=2,
) as watcher:
    ...
print(watcher.triggered, watcher.errors)
```

监控线程与主线程动作共用设备锁，不会交叉执行点击；轮询异常记录在 `.errors` 而不是中断后台线程；`.triggered` 记录每次命中的规则名。

#### `find(selector, *, timeout=0) -> UiObject | None`

轮询直到出现第一个元素或超时。`timeout` 单位为秒。

```python
selector = device.selector().text("登录")
element = device.find(selector, timeout=8)
if element is None:
    raise RuntimeError("login button not found")
```

#### `wait(selector, *, timeout=10, interval=0.3, log=False) -> UiObject`

等待元素出现并返回；超时抛出 `LookupError`。`interval` 是每轮设备查询间隔（秒），必须大于零；适合“缺失即为失败”的关键控件，与 `find()` 的返回 `None` 语义互补。

#### `wait_any(selectors, *, timeout=10, interval=0.3, log=False) -> tuple[str, UiObject]`

等待任一命名 selector 出现。`selectors` 是 `{名称: Selector}` 映射，返回命中的名称和元素。每一轮只读取一次 `full` 树并在本地筛选所有 selector，避免候选数量增加时重复请求设备。

```python
name, element = device.wait_any(
    {
        "success": device.selector().name("home_screen"),
        "failure": device.selector().text("登录失败"),
    },
    timeout=15,
    interval=0.5,
)
```

它只保证同一轮内各候选来自同一棵树；在 `element.click()` 前页面仍可能变化，关键动作前需要最新状态时请重新查询。

#### `wait_gone(selector, *, timeout=10, interval=0.3, log=False) -> bool`

等待元素消失；消失返回 `True`，超时返回 `False`。

#### `dump_hierarchy(*, mode="smart") -> str`

等价于 `client.ui_xml()`，用于保存诊断 XML。

#### `screenshot(destination=None) -> bytes | Path`

未传 `destination` 时返回 PNG bytes；传入本地路径时保存并返回 `Path`。

#### `snapshot(*, mode="full") -> UiSnapshot`

读取一次完整控件树并创建本地快照。快照内的选择器和关系查询不再访问设备，适合需要在同一棵树上多次定位的场景。推荐 `mode="full"`，因为关系查询依赖完整的 `childs` 层级。

```python
snapshot = device.snapshot(mode="full")
login_form = snapshot(name="login_form")
submit = login_form.child(device.selector().name("submit_button"))
error_text = login_form.descendant(device.selector().text("密码错误"))
parent = submit.parent()
siblings = submit.sibling()

assert submit.count == 1
submit.get().click()
```

`SnapshotCollection` 支持 `.exists`、`.count`、`.info`、`.all()`、`.get()`，以及 `.child()`（直接子节点）、`.descendant()`（所有后代）、`.parent()`、`.sibling()`。`.where_regex(field, pattern)` 可用 Python 正则表达式在当前快照集合内筛选字段，例如 `snapshot().descendant().where_regex("name", r"item_\d+")`。快照对所有公开 selector 字段（包括 `name`、`label`、`type` 和状态字段）建立精确值倒排索引；精确属性查询先走索引，多条件、contains、正则和关系查询再在候选集上过滤，结果保持原始树顺序。`Selector.at()` / `.at_relative()` 在快照内按已归一化的物理矩形包含关系查询，最小命中矩形优先。关系、正则和点查询只在本地快照中解释，绝不会发送给设备端 selector 协议；快照忽略设备端专用的 `mode`、`max_depth`、`max_children` 限制。快照节点的 `info`、`rect`、`center` 已归一化为物理像素，但页面跳转后可能过期；在动作前需要最新页面状态时，请重新创建快照或使用普通 `device(...)` 查询。

#### 坐标与图片委托

`Device` 也直接暴露以下方法，语义与 `AScriptClient` 同名方法完全一致：

- 绝对物理像素：`tap()`、`swipe()`、`pixel()`、`pixels()`、`screenshot_crop()`、`save_screenshot_crop()`；
- 比例坐标：`click_relative()` / `click_rel()`、`swipe_relative()`、`pixel_relative()`、`pixels_relative()`、`screenshot_crop_relative()`、`save_screenshot_crop_relative()`；
- 视觉：`capture_frame()`、`find_image()`、`find_images()`、`find_any_image()`、`wait_image()`、`wait_any_image()`、`wait_image_gone()`、`tap_image()`、`scroll_until_image()`。

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
| `.at(x, y)` | 使用物理像素坐标从该点探测元素 |
| `.at_relative(x_ratio, y_ratio)` | 按屏幕比例从该点探测元素 |
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
| `.wait_gone(timeout=10, log=False)` | 元素消失前轮询；成功返回 `True`，超时返回 `False`；`log=True` 输出每轮状态 |
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
| `.click()` | 以矩形中心的物理像素点击；空矩形抛 `ValueError` |
| `.click_relative(x_ratio, y_ratio)` | 按元素矩形比例点击；`(0, 0)` 为左上、`(1, 1)` 为矩形内最后一个有效像素 |
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
| `wait(selector, timeout=10, name="wait", log=False)` | 等待元素出现；超时自动采集失败证据 |
| `assert_unique(selector, name="assert_unique")` | 断言 selector 恰好命中一个元素，并返回该元素 |

`manifest.json` 会在每个步骤结束后更新，记录运行 ID、设备地址、开始/结束时间、步骤耗时、结果、异常和实际写入的证据路径。

### `AScriptClient.locked()`

返回同一 Python 进程内、按设备 `HOST:PORT` 共享的可重入互斥上下文。`tap`、`swipe`、`input_text`、`home`、`eval_python`、项目运行/部署、文件写入和删除操作已自动使用它；通常不需要手动调用。

该锁不跨 Python 进程、CI runner 或物理主机。多进程并行执行同一手机时，仍需由 CI 队列、文件锁或外部调度器保证每台设备同时只有一个作业。

## 7. 交互动作与设备端代码

### 高频交互与前台 App 等待

所有 Python 动作的 `duration` 单位为秒；`duration_ms` 是兼容毫秒参数，二者不能同时传入。未传时保留原动作默认值。

```python
client.long_press(600, 1200, duration=0.8)
client.double_tap_relative(0.5, 0.5, duration=0.02)
client.drag_relative(0.2, 0.5, 0.8, 0.5, duration=0.5)

# 等待目标 App 成为前台。
client.wait_current_app("com.example.app", timeout=10)
```

`UiObject`/`SnapshotNode` 提供 `.long_click()`、`.double_click()`、`.drag_to()`、`.drag_to_relative()`；`Device` 提供上述低层绝对和比例动作门面。`device.click_if_unique(selector)` 在同一设备锁内查询 selector、断言恰好一个匹配并立即点击，适合避免“先查后点”期间页面变化的常见竞态。

### 动作时长

Python 动作 API 的 `duration` 单位为**秒**，例如 `duration=0.35`；`duration_ms` 是兼容的毫秒参数。两者不能同时传入，均未传时保留原动作默认值。`timeout`、`interval`、`duration` 均为秒；只有带 `_ms` 后缀的参数才是毫秒。

### `tap(x, y, *, duration=None, duration_ms=None)`

点击物理像素动作坐标。它与 `screen_size()`、截图、OCR 返回的坐标使用同一坐标系；可用 Inspector 点击截图后显示的“动作坐标”取得准确数值。

```python
client.tap(200, 600, duration=0.02)
```

### `screen_size()`、`action_size()`、`relative_point()` 与 `tap_relative()`

`screen_size()` 返回当前真实物理分辨率，且与 `action_size()` 同义；两者都读取当前 PNG 截图头。以 iPhone 393 x 852 点、3 倍截图为例，返回 `1179 x 2556`。`tap`、`swipe`、截图、OCR 与 Inspector 的“动作坐标”都使用物理像素。

客户端对控件树的 `x/y/width/height`、`ui_tree(..., x, y)` 点探测参数和 XML 坐标也统一使用物理像素，`ui_tree()` 在接收树时已完成逻辑点到物理像素的归一化，`UiObject.click()` 可直接使用控件中心点。只有 `status()["logical_screen"]` 显式保留服务端返回的逻辑点尺寸，用于诊断移动端协议。

比例 API 始终依据 `action_size()` 换算，适合固定在“屏幕中部”“底部按钮区域”等相对位置的操作。比例必须是 `0.0` 到 `1.0` 的有限数字：`0.0` 表示左/上边缘，`1.0` 会夹紧到最后一个有效像素，避免越界。换算发生在每次调用时，因此会适应不同设备尺寸和当前横竖屏。`UiObject.click()` 同样会将控件树的逻辑点自动换算为动作像素。

```python
screen = client.screen_size()               # {"width": 1179.0, "height": 2556.0}
x, y = client.relative_point(0.5, 0.92)    # (589.5, 2351.52)
client.tap_relative(0.5, 0.92)             # 点击宽度 50%、高度 92% 的位置
client.swipe_relative(0.5, 0.8, 0.5, 0.2)
```

高层对象 API 提供同等入口：`device.click_relative(0.5, 0.92)`；`device.click_rel(0.5, 0.92)` 是便于迁移的短别名。控件树的 `x/y/width/height` 可直接用于同一物理像素坐标系内的绝对动作。

### `swipe(x1, y1, x2, y2, *, duration=None, duration_ms=None)`

从起点滑动到终点。

```python
client.swipe(200, 700, 200, 250, duration=0.35)
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

### `ocr_raw()`、`ocr()`、`find_ocr_text()` 与 `wait_ocr_text()`

`ocr_raw()` 返回设备端原始 OCR 载荷。`ocr()` 返回 `OcrResult(items, raw)`；每个 `OcrItem` 提供 `text`、`confidence`、物理像素 `rect=(left, top, right, bottom)` 和 `raw`。真机 AScript 4001 已验证 `rect`、`center_x/y`、`confidence` 与 `text` 字段。

```python
result = client.ocr(region=(0, 300, 1179, 1800))
for item in result.items:
    print(item.text, item.rect, item.confidence)

login = client.wait_ocr_text("登录", timeout=10)
```

OCR 区域遵循统一规则：`region` 是物理像素，`region_relative` 是比例区域。

### 本机颜色 API

颜色输入统一接受 `PixelColor`、`(r, g, b)`、`(r, g, b, a)`、`"#RRGGBB"` 或 `"#RRGGBBAA"`；返回统一为 `PixelColor`，使用 `.rgb`、`.rgba`、`.hex` 取得视图。默认比较 RGB，每通道容差由 `tolerance` 指定；`include_alpha=True` 才比较 alpha。

```python
assert client.color_matches(100, 200, "#FFFFFF", tolerance=3)
assert client.color_matches_relative(0.5, 0.92, (255, 255, 255))

frame = client.capture_frame()
point = frame.find_color("#FF0000", region=(0, 1800, 1179, 2556))
count = frame.count_color((255, 255, 255), region_relative=(0, 0.7, 1, 1))
frame.assert_color(100, 200, "#FFFFFF")
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

### 调用方式

同一个 CLI 有三种等价的调用形式，按环境选择其一即可：

| 形式 | 适用环境 | 说明 |
| --- | --- | --- |
| `py -m asclient` | Windows | 推荐；不依赖 `Scripts` 目录是否加入 `PATH` |
| `python3 -m asclient` | macOS / Linux | 等价命令；部分环境也可用 `python -m asclient` |
| `asc <命令>` | 已把 Python 用户 `Scripts` 目录加入 `PATH` 的环境 | 最短写法；目录配置方法见[从零开始使用教程](从零开始使用教程.md)的 2.3 节 |

本文所有示例统一写 `py -m asclient`（Windows 风格）；macOS/Linux 用户将其替换为 `python3 -m asclient` 或 `asc` 即可，参数完全相同。

### 命令共用参数

默认从当前目录的 `asclient.json` 读取连接配置。可复制根目录的 `asclient.example.json` 后填写设备信息；命令行参数只用于单次覆盖。所有命令共用：

```bat
py -m asclient [--config FILE] [--device HOST:PORT] [--password PASSWORD] [--timeout SECONDS] <command>
```

任何改变设备状态的命令必须显式加 `--yes`，可写在命令前或命令后。这是非交互式确认，便于脚本审计：

```bat
py -m asclient --yes deploy smoke .\smoke.py --logs 5
py -m asclient remove smoke --yes
```

不带 `--yes` 时客户端在发送请求前失败，并打印目标设备和被拒绝的动作。Python 库 API 不会重复要求确认，因为调用者代码本身已是显式授权边界。

| 命令 | 用法 | 作用 |
| --- | --- | --- |
| `ping` | `ping` | 探测服务 |
| `help` | `help [COMMAND]` | 输出当前语言下的简明说明，不连接设备 |
| `doctor` | `doctor [--report FILE] [--fix-iproxy PATH] [--yes]` | 诊断本机工具、端口、设备与日志；仅对已验证的 `iproxy` 路径提供经确认的配置修复 |
| `status` | `status` | 输出设备状态或兼容降级状态 |
| `pkgs` | `pkgs` | 列出设备 Python 包 |
| `app` | `app` | 当前 App 信息 |
| `shot` | `shot [output.png] [--crop-rel LEFT TOP RIGHT BOTTOM]` | 保存截图，可按比例裁剪 |
| `dump` | `dump [output.xml] [--mode MODE]` | 保存 XML 控件树 |
| `observe` | `observe [--prefix PREFIX]` | 同时保存截图与 XML |
| `inspect` | `inspect [--host HOST] [--port PORT] [--no-browser]` | 启动本机 Inspector |
| `tunnel` | `tunnel [--local-port PORT] [--remote-port PORT] [--local-log-port PORT] [--remote-log-port PORT] [--no-logs] [--udid UDID] [--iproxy PATH]` | 以前台方式同时管理 HTTP 与日志 USB `iproxy` 隧道；`--no-logs` 仅映射 HTTP |
| `tap` | `--yes tap X Y [--duration MS | --duration-ms MS | --duration-s SECONDS]` | 坐标点击；旧 `--duration` 保持毫秒兼容 |
| `tap-rel` | `--yes tap-rel X_RATIO Y_RATIO [--duration MS | --duration-ms MS | --duration-s SECONDS]` | 按屏幕宽高比例点击 |
| `swipe` | `--yes swipe X1 Y1 X2 Y2 [--duration MS | --duration-ms MS | --duration-s SECONDS]` | 坐标滑动 |
| `swipe-rel` | `--yes swipe-rel X1_RATIO Y1_RATIO X2_RATIO Y2_RATIO [--duration MS | --duration-ms MS | --duration-s SECONDS]` | 按屏幕宽高比例滑动 |
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
| `deploy` | `--yes deploy PROJECT ENTRY [--logs SECONDS] [--screenshot FILE]` | 上传、运行、取日志和截图；`--logs` 默认收集 5 秒 |
| `log` | `log [SECONDS] [--reconnects N] [--output FILE] [--contains TEXT]` | 输出、筛选或 JSONL 落盘实时日志；不传秒数时默认读取 3 秒 |
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
| `serve(client, host="127.0.0.1", port=0, open_browser=True, output_dir=None)` | 创建但不启动 `ThreadingHTTPServer`；`port=0` 自动选端口；`output_dir` 指定“裁剪保存”的落盘目录，默认当前工作目录 |
| `run_forever(...) -> str` | 创建并阻塞运行，Ctrl+C 后关闭；返回本地 URL |

Inspector 只应绑定 `127.0.0.1`。界面所有操作文案均为中文，它在顶部显示当前 App 的名称、Bundle ID、PID 和当前树节点数量，并提供当前页面截图和节点信息。树、截图和属性面板之间的两条分隔线可拖动调整宽度；中间截图始终按原始宽高比缩放，不会被拉伸。顶部“裁剪保存”按钮进入裁剪模式，在截图上拖拽矩形、松开后会生成原始像素 PNG，保存到运行 `inspect` 命令的当前工作目录，且服务端生成安全的时间戳文件名。不需要、也不应作为局域网服务使用。

CLI 启动时会打印实际 Inspector URL。浏览器在刷新或关闭页面时取消正在传输的快照属于正常情况，客户端会静默结束该响应，不会影响设备端状态或打印终端堆栈。

页面中的“验证选择器”按钮会以只读方式查询当前候选选择器的实际匹配数。只有结果为 `1` 时，才应将其作为代码候选；该按钮不会点击或修改设备状态。
