# AScript 本地客户端

`asclient` 是 AScript iOS 本地设备服务的无第三方依赖 Python 库与命令行客户端。它不修改 IPA、不向手机安装组件，通过设备已有的 `9096` HTTP 服务和 `10102` 日志 WebSocket 提供截图、控件树、坐标操作、项目管理、OCR、日志与自动化能力。

完整中文文档： [生产使用指南](docs/production-guide.md)、[API 使用参考](docs/api-reference.md)、[USB 隧道运维指南](docs/usb-tunnel.md)、[发布与验收流程](docs/release-process.md)。

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

在仓库根目录安装，复制配置模板后填写设备地址。真实 `asclient.json` 已被 Git 忽略，其中的密码、UDID 与内网地址不得提交。

```bat
py -m pip install --user --upgrade .
copy asclient.example.json asclient.json
edit asclient.json
py -m asclient doctor
```

常用配置项：

- `language`：`auto`、`zh-CN` 或 `en`。`auto` 时中文系统输出中文，其他系统输出英文。
- `device.address`：Wi-Fi 场景填写手机服务地址，例如 `192.168.3.17:9096`；USB 场景填写 `127.0.0.1:9096`。
- `device.password`：设备服务密码；留空表示不发送密码 Cookie。
- `tunnel`：USB `iproxy` 的可执行文件、UDID 和端口配置。

单次命令可使用 `--device`、`--password`、`--timeout` 与 `--lang` 覆盖配置文件。

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
device.selector().at(200, 600)

# 也可按当前屏幕的宽高比例定位：屏幕中部偏下。
device.click_rel(0.5, 0.92)
```

坐标规则：`screen_size()`、`action_size()`、控件树、截图、OCR 与 `tap`/`swipe` 都使用物理像素（该设备为 `1179 x 2556`）。比例 API 与 `UiObject.click()` 已自动完成换算，绝对坐标请使用 Inspector 显示的 `Action coordinate`。只有 `status()["logical_screen"]` 会显式提供移动端原始逻辑点，供协议诊断使用。

生产工作流建议使用 `Run`。它会将同一设备的动作串行化，并为每一步写入独立证据目录：

```python
from asclient import Run, connect

device = connect("192.168.3.17:9096")
with Run(device) as run:
    login = run.assert_unique(device.selector().name("login_button"))
    run.step("open_login", login.click, capture_after=True)
```

## Inspector 与真机验收

`py -m asclient inspect` 会启动仅监听本机回环地址的浏览器 Inspector，展示当前截图、控件树、前台 App、控件属性、可复制选择器和真机坐标。拖动三栏分隔线不会影响截图比例或点击坐标映射。

部分 App 或页面不暴露无障碍控件树，此时 Inspector 会正确显示没有语义节点。仍可单独使用截图、OCR、图色与坐标操作；不要假设所有页面都能使用语义选择器。

真机冒烟测试使用独立配置文件，避免环境变量和误操作：

```bat
copy tests\integration.example.json tests\integration.json
edit tests\integration.json
py -m unittest discover -s tests -p test_integration.py -v
```

将 `tests\integration.json` 的 `enabled` 显式设为 `true` 后才会连接真机。该套件默认只读取状态、截图、控件树、日志端口和可选选择器，不执行点击、输入、上传、删除或部署。

## 运行方式与边界

推荐使用 `py -m asclient`，不依赖 Windows Python `Scripts` 目录是否已加入 `PATH`。兼容入口 `py asc.py ...` 仍然可用。

该库仅依赖 Python 标准库。移动端 API 已针对 iOS 4001 IPA 进行静态分析，并已通过真实 USB 连接验证；生产发布前仍应在目标 App、目标 iOS 版本和目标设备上执行集成验收。
