# AScript 本地客户端

`asclient` 是 AScript iOS 本地设备服务的 Python 库与命令行客户端。它不修改 IPA、不向手机安装组件，通过设备已有的 `9096` HTTP 服务和 `10102` 日志 WebSocket 提供截图、控件树、坐标操作、项目管理、OCR、日志与自动化能力；仅本机模板匹配功能依赖 Pillow（可选，模糊匹配还可用 OpenCV 加速）。

完整中文文档：[从零开始使用教程](docs/从零开始使用教程.md)、[生产使用指南](docs/生产使用指南.md)、[API 使用参考](docs/API使用参考.md)、[USB 隧道运维指南](docs/USB隧道运维指南.md)、[发布与验收流程](docs/发布与验收流程.md)、[变更说明](docs/变更说明.md)。

## 最短上手

前提：手机已开启 AScript 服务，且电脑与手机在同一网络（USB 场景见下文“快速诊断与 USB 连接”）。三步走：安装 → 配置 → 验证。

```bat
py -m pip install --user --upgrade .
py -m asclient init
edit asclient.json
```

`init` 会生成一份包含全部配置键的 `asclient.json`，并自动填入已安装的 `iproxy` 路径；它不连接设备，可在手机就绪前先执行。已存在同名文件时会拒绝覆盖，确需重建加 `--force`。也可以照旧 `copy asclient.example.json asclient.json`。

`asclient.json` 中把 `device.address` 改为手机 AScript 页面显示的地址，例如 `192.168.3.17:9096`（USB 隧道场景保持默认的 `127.0.0.1:9096`）。然后验证连接：

```bat
py -m asclient status
```

输出中 `"available": true` 即连接成功（部分设备显示 `health: "degraded"` 属于已知兼容降级，不代表失败；客户端会自动用设备端 eval 回补缺失的只读字段，见生产使用指南的兼容降级根因章节）。接着用五行 Python 完成第一次自动化：

```python
from asclient import connect

device = connect("192.168.3.17:9096")   # 与 asclient.json 中一致
print(device(text="登录").count)         # 先确认恰好命中一个控件
device(text="登录").click()             # 确认无误后再点击
```

## 任务速查

以下 `device` 指 `connect()` 返回的对象，`client` 指低层 `AScriptClient`（可从 `device.client` 取得）；`timeout`、`interval` 单位为秒。完整参数见 [API 使用参考](docs/API使用参考.md)。

| 我想… | 写法 |
| --- | --- |
| 截图留证 | `device.screenshot("evidence/step.png")` |
| 点击文本为“登录”的按钮 | `device(text="登录").click()` |
| 按控件名（accessibility name）定位 | `device(name="login_button")` |
| 确认控件存在 / 数量 | `device(text="登录").exists` / `.count` |
| 等控件出现（最多 10 秒） | `device(text="首页").get(timeout=10)` |
| 等控件出现，超时直接报错 | `device.wait(device.selector().text("首页"), timeout=10)` |
| 等控件消失 | `device(text="弹窗").wait_gone(timeout=10)` |
| 找到才点击，找不到不报错 | `device(text="同意").click_exists(timeout=5)` |
| 输入文本（自动先点击控件取得焦点） | `device(resource_id="username").set_text("hello")` |
| 等图片出现并返回坐标 | `client.wait_image("assets/login.png", confidence=0.95)` |
| 等图片出现后点击中心 | `client.tap_image("assets/继续.png", timeout=10)` |
| 等 loading 图片消失再继续 | `client.wait_image_gone("assets/loading.png", timeout=20)` |
| 滚动列表直到目标图片出现 | `client.scroll_until_image("assets/target.png", direction="up")` |
| 滚动列表直到目标控件出现 | `device.scroll_until_element(device.selector().name("提交"), direction="up")` |
| 弹窗出现自动点击（后台监控） | `with device.watch(device.selector().text("允许"), interval=1.5): ...` |
| 按屏幕比例点击（底部中央） | `device.click_rel(0.5, 0.92)` |
| 按屏幕比例滑动 | `client.swipe_relative(0.5, 0.8, 0.5, 0.2)` |
| 识别屏幕文字 | `client.ocr()` |
| 找含“登录”二字的 OCR 文本（含坐标） | `client.find_ocr_text("登录")` |
| 等屏幕出现指定文字 | `client.wait_ocr_text("登录成功", timeout=10)` |
| 读取一个像素的 RGB/HEX | `client.pixel(100, 200).rgb` / `.hex` |
| 按比例读取像素颜色 | `client.pixel_relative(0.5, 0.92)` |
| 断言某点是某颜色（带容差） | `client.assert_color(100, 200, "#FFFFFF", tolerance=3)` |
| 区域内找/数某种颜色 | `client.find_color("#FF0000", region=(0, 1800, 1179, 2556))` / `client.count_color(...)` |
| 长按 / 双击（绝对坐标） | `client.long_press(600, 1200, duration=0.8)` / `client.double_tap(600, 1200)` |
| 按比例长按 / 双击 | `client.long_press_relative(0.5, 0.5, duration=0.8)` |
| 拖拽（绝对 / 比例） | `client.drag(200, 1000, 900, 1000, duration=0.5)` / `client.drag_relative(0.2, 0.5, 0.8, 0.5)` |
| 元素长按 / 双击 / 拖到某点 | `element.long_click()` / `element.double_click()` / `element.drag_to(800, 1200)` |
| 等目标 App 成为前台 | `client.wait_current_app("com.example.app", timeout=10)` |
| 启动 / 停止 App | `client.app_start("com.example.app")` / `client.app_stop("com.example.app")` |
| 查 App 运行状态 | `client.app_state("com.example.app")["state"]` |
| 锁屏 / 解锁 | `client.lock_screen()` / `client.unlock_screen()` |
| 读写设备剪贴板 | `client.set_clipboard("code")` / `client.get_clipboard()` |
| 查当前屏幕方向 | `client.orientation()` |
| 打开 URL / 深链 | `client.open_url("myapp://page")` |
| 发送按键（home/音量/电源） | `client.press_key("home")` |
| 读取元素文本 | `device(label="标题").get_text()` |
| 清空元素文本 | 暂不可用（设备端限制，见 API 参考 `get_text` 行） |
| 多段轨迹滑动 | `client.slide_path([(1800, 1300), (600, 1300)], durations=[150, 150])` |
| 长按拖拽（三段停留） | `client.touch_and_slide(600, 1300, 1800, 1300)` |
| 矩形内随机点击 | `client.click_random(300, 500, 900, 1000)` |
| 拟人抖动点击 | `client.tap(600, 1300, jitter=5)` |
| SIFT 特征匹配 | `client.find_sift(["~/res/img/x.png"], threshold=0.7)` |
| 二维码/条码识别 | `client.scan_code()` |
| YOLO 目标检测 | `client.yolov_load(p, b, yaml)` → `client.yolov_detect(threshold=0.5)` |
| 设备端整帧缓存 | `client.screen_cache(True)`（批量找色/OCR 前开） |
| 系统通知 | `client.notify("跑完了", title="asclient")` |
| 元素内滚动 / 滚动查找 | `element.scroll("down", 0.8)` / `element.scroll_to(device.selector().name("目标"))` |
| 查电池 / 设备信息 | `client.battery_info()` / `client.device_info()` |
| 查到唯一元素才点击（锁内原子） | `device.click_if_unique(device.selector().name("提交"))` |
| 一帧中找多个模板 | `client.find_images({"成功": "success.png", "失败": "failure.png"})` |
| 等待任意页面结果 | `name, match = client.wait_any_image({...}, timeout=20)` |
| 本地快照关系查询 | `device.snapshot()(name="表单").child(device.selector().text("提交"))` |
| 读取控件树 XML 字符串 | `device.dump_hierarchy()` |
| 每步自动留证据的可靠执行 | `run.step("登录", login.click, capture_after=True)` |
| 等待日志出现标记 | `client.wait_for_log("READY", timeout=10)` |
| 在 Python 中管理 USB 隧道（读配置） | `with AScriptTunnel.from_config() as tunnel:` |

## IDE 提示

包内置类型注解、中文 docstring 和 `py.typed` 标记。安装后在 PyCharm 或 VS Code/Pylance 中输入 `client.`、`device.` 或将鼠标悬停在方法上，可看到参数类型、中文单位说明、默认值与返回类型。参数名保持英文以兼容 Python 生态；`timeout`、`interval`、`duration` 单位为秒，只有带 `_ms` 后缀的参数才使用毫秒。

```python
from asclient import AScriptClient

device = AScriptClient("192.168.3.17:9096")
device.save_screenshot("screen.png")
# tap/swipe 使用截图的物理像素坐标；先用 action_size() 确认尺寸。
device.tap(600, 1800)
device.upload_file("demo", "__init__.py")
device.run_project("demo")
```

## 安装与配置

在仓库根目录安装，用 `init` 生成配置后填写设备地址。真实 `asclient.json` 已被 Git 忽略，其中的密码、UDID 与内网地址不得提交；`init` 在检测到配置未被忽略时会主动警告。

```bat
py -m pip install --user --upgrade .
py -m asclient init
edit asclient.json
py -m asclient doctor
```

需要模板匹配、找图等视觉功能时，改用 `py -m pip install --user --upgrade ".[vision]"` 一并安装可选依赖 Pillow 与 OpenCV（`opencv-python-headless`）。OpenCV 用于模糊匹配加速，未安装时自动降级到纯 Pillow 实现，功能不受影响。

以上为 Windows 写法；macOS/Linux 将 `py` 替换为 `python3`，`edit` 表示用任意文本编辑器打开该文件。安装后 `asc` 命令也可用（需将 Python 用户 `Scripts` 目录加入 `PATH`，见[从零开始使用教程](docs/从零开始使用教程.md)的 2.3 节），例如 `asc doctor`。

常用配置项：

- `language`：`auto`、`zh-CN` 或 `en`。`auto` 时中文系统输出中文，其他系统输出英文。
- `device.address`：Wi-Fi 场景填写手机服务地址，例如 `192.168.3.17:9096`；USB 场景填写 `127.0.0.1:9096`。
- `device.password`：设备服务密码；留空表示不发送密码 Cookie。
- `tunnel`：USB `iproxy` 的可执行文件、UDID 和端口配置。

单次命令可使用 `--device`、`--password`、`--timeout` 与 `--lang` 覆盖配置文件。这些都是全局参数，必须写在子命令之前（例如 `py -m asclient --device 127.0.0.1:9096 status`）；只有 `--yes` 允许写在子命令之后。优先级统一为“命令行参数 > 配置文件 > 内置默认值”。

## 快速诊断与 USB 连接

运行 `py -m asclient help` 查看中文命令速查。`py -m asclient doctor` 会只读检查 `iproxy`、隧道端口、控制服务、日志服务和 `status` 兼容性；默认不修改电脑或手机。

电脑与手机不在同一网络时，安装受信任来源的 `iproxy` 后执行：

```bat
py -m asclient doctor
py -m asclient tunnel
```

`tunnel` 会同时映射 `127.0.0.1:9096 -> 手机:9096` 与 `127.0.0.1:10102 -> 手机:10102`。保持该终端运行，再打开第二个终端执行：

```bat
py -m asclient status
py -m asclient log 10
py -m asclient inspect
```

USB 场景下 `device.address` 必须是 `127.0.0.1:9096`。端口冲突时可用 `tunnel --local-port` / `--local-log-port` 改用其他本地端口，但 `tunnel` 不会自动改写配置，业务命令需同步用 `--device 127.0.0.1:<新端口>` 指向新端口；多设备并行时再用 `--udid` 固定目标手机。隧道的本机监听地址只能是回环地址，不提供绑定到局域网网卡的选项。完整参数表见 [USB 隧道运维指南](docs/USB隧道运维指南.md)。

若已安装 `iproxy.exe` 但未加入 `PATH`，可执行 `py -m asclient doctor --fix-iproxy "D:\\tools\\libimobiledevice\\iproxy.exe"`；工具会显示修改计划，并在确认后才写入本地配置。

## 自动化对象 API

手机服务保持不变；客户端提供类 `uiautomator2` 的自动化 API，通过 AScript 已有的元素树接口解析选择器。

```python
from asclient import connect

device = connect("192.168.3.17:9096")
confirm = device(text="Confirm", class_name="XCUIElementTypeButton")
if confirm.exists:
    print(confirm.info)
    confirm.click()

# 也支持稳定的显式选择器和坐标点探测。
device.selector().name("login_button")
device.selector().at(200, 600)              # 绝对物理像素
device.selector().at_relative(0.5, 0.5)     # 屏幕比例坐标

# 也可按当前屏幕的宽高比例定位：屏幕中部偏下。
device.tap(590, 2352)                       # 绝对物理像素
device.click_rel(0.5, 0.92)                 # 比例坐标
```

坐标规则统一如下：**无后缀方法一律使用截图物理像素绝对坐标**，例如 `tap(x, y)`、`swipe(...)`、`Selector.at(x, y)`、`pixel(x, y)`、`screenshot_crop(left, top, right, bottom)`；**`*_relative` 一律使用 `0..1` 比例坐标**，例如 `tap_relative()`、`Selector.at_relative()`、`pixel_relative()`、`screenshot_crop_relative()`。找图的物理像素区域为 `region` / `regions`，比例区域为 `region_relative` / `regions_relative`；旧 `region_pixels` / `regions_pixels` 是弃用别名。所有矩形均为 `left, top, right, bottom`，左上包含、右下排除。控件树、截图、OCR 与 `tap`/`swipe` 使用物理像素，坐标始终跟随当前屏幕方向（竖屏如 `1179 x 2556`，横屏自动变为 `2556 x 1179`，已在真机横屏验证）；只有 `status()["logical_screen"]` 保留移动端原始逻辑点，供协议诊断使用。

截图支持比例裁剪，矩形采用 `left top right bottom`，范围为 `0..1`：

```python
# 保存屏幕下半部分。
device.save_screenshot_crop_relative("artifacts/bottom.png", 0, 0.5, 1, 1)
```

```bat
py -m asclient shot artifacts\bottom.png --crop-rel 0 0.5 1 1
```

本机模板可等待图标出现/消失，也可指定置信度。对于通常使用的容差匹配（`confidence < 1.0`），客户端会优先读取设备已存在的 HID JPEG 帧；HID 不可用时自动回退 PNG，因此无需为 USB 场景引入另一套 API。精确匹配（`confidence=1.0`）、截图留证与取色仍使用无损 PNG；模板必须接受 JPEG 的有损压缩，并应在目标设备上验收阈值。

```python
match = device.wait_image("assets/login-icon.png", confidence=0.95, timeout=15, log=True)
# 默认先等待一个 interval；需要立即探测时设 initial_delay=False。
match = device.wait_image("assets/login-icon.png", interval=0.5, initial_delay=False)
device.tap(*match.center)
device.wait_image_gone("assets/loading.png", confidence=0.90, timeout=20, log=True)

# 默认向下滑动；任一上限先到即停止。
match = device.scroll_until_image("assets/target.png", direction="down", confidence=0.95, timeout=30, max_swipes=8, log=True)

# 自定义比例手势与每次滑动时长；提供后 direction 不参与轨迹计算。
match = device.scroll_until_image("assets/target.png", swipe_relative=(0.7, 0.75, 0.35, 0.25), duration=0.65)
```

生产工作流建议使用 `Run`。它会将同一设备的动作串行化，并为每一步写入独立证据目录：

```python
from asclient import Run, connect

device = connect("192.168.3.17:9096")
with Run(device) as run:
    login = run.assert_unique(device.selector().name("login_button"))
    run.step("open_login", login.click, capture_after=True)
```

完整可运行的带注释示例见 [examples/完整流程示例.py](examples/完整流程示例.py) 与[从零开始使用教程](docs/从零开始使用教程.md)第 10 节。

## Inspector 与真机验收

`py -m asclient inspect` 会启动仅监听本机回环地址的浏览器 Inspector。界面全部使用中文，展示当前截图、控件树、前台 App、控件属性、可复制选择器和真机坐标。拖动三栏分隔线不会影响截图比例或点击坐标映射。

这里有两个不同的地址，不要混淆：

| 我要改的 | 参数 | 默认值 |
| --- | --- | --- |
| 连接哪台手机 | 全局 `--device HOST[:PORT]`，或配置文件 `device.address` | `192.168.3.17:9096` |
| Inspector 自己监听在哪 | `inspect --host HOST` | `127.0.0.1` |
| Inspector 监听端口 | `inspect --port PORT`（`0` 表示随机端口） | `0` |
| 不自动打开浏览器 | `inspect --no-browser` | 自动打开 |

`--device` 是全局参数，必须写在子命令之前；`--host` 属于 `inspect` 子命令，写在其后：

```bat
py -m asclient --device 192.168.3.25:9096 inspect --port 8765
```

USB 场景下设备地址应为 `127.0.0.1:9096`（先在另一个终端运行 `tunnel`）。启动后终端会打印实际 URL，随机端口时必须从这一行获取端口号。

顶部的“框选区域”会冻结一张原始 PNG 并暂停实时刷新：在截图上拖拽仅更新选区，实时显示物理像素坐标、尺寸、中心点和相对坐标；确认后点击“保存 PNG”（或按 `Enter`）才会保存无损裁剪图及同名 JSON 元数据，按 `Esc` 可取消。文件名形如 `inspect_crop_YYYYMMDD_HHMMSS_x120_y340_w700_h700.png`。CLI 不提供修改该目录的参数；需要指定目录时使用 Python `serve(client, output_dir=...)`。

**不要使用 `--host 0.0.0.0`。** Inspector 的 `/api/*` 接口没有任何鉴权，能读取设备截图与控件树；绑定到非回环地址等于把手机屏幕内容暴露给同网段所有主机。该参数仅为特殊调试场景保留，生产与日常使用应保持默认的 `127.0.0.1`。

部分 App 或页面不暴露无障碍控件树，此时 Inspector 会正确显示没有语义节点。仍可单独使用截图、OCR、图色与坐标操作；不要假设所有页面都能使用语义选择器。

真机冒烟测试使用独立配置文件，避免环境变量和误操作：

```bat
copy tests\integration.example.json tests\integration.json
edit tests\integration.json
py -m unittest discover -s tests -p test_integration.py -v
```

将 `tests\integration.json` 的 `enabled` 显式设为 `true` 后才会连接真机。该套件默认只读取状态、截图、控件树、日志端口和可选选择器，不执行点击、输入、上传、删除或部署。

## 运行方式与边界

CLI 有三种等价调用：Windows 推荐 `py -m asclient`（不依赖 `Scripts` 目录是否加入 `PATH`）；macOS/Linux 使用 `python3 -m asclient`；`Scripts` 目录已加入 `PATH` 时可直接用最短的 `asc <命令>`（配置方法见[从零开始使用教程](docs/从零开始使用教程.md)的 2.3 节）。兼容入口 `py asc.py ...` 仍然可用。

除模板匹配所需的 Pillow（以及可选的 OpenCV 加速）外，该库只依赖 Python 标准库；Pillow 与 OpenCV 均为可选依赖，需要视觉功能时用 `pip install "asclient[vision]"` 安装，模糊匹配在装有 OpenCV 时自动走 `cv2.matchTemplate` 加速、否则降级到纯 Pillow。移动端 API 已针对 iOS 4001 IPA 进行静态分析，并已通过真实 USB 连接验证；生产发布前仍应在目标 App、目标 iOS 版本和目标设备上执行集成验收。
