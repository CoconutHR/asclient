# ASClient 从零开始使用教程

本文适合第一次接触 ASClient 的用户。目标是按顺序完成以下事情：安装环境、连上一台 iPhone、打开 Inspector、运行第一个 Python 程序、知道出错时如何排查，然后再按示例使用各项能力。

完整参数、返回值和全部类成员请查阅 [API 使用参考](api-reference.md)；生产环境规范请查阅[生产使用指南](production-guide.md)。

## 1. 开始前要知道的事

ASClient 是已运行 AScript 本地服务的 iPhone 的客户端，不会修改 IPA，也不会向手机安装组件。它连接两个设备端口：

| 端口 | 用途 | 默认值 |
| --- | --- | --- |
| HTTP 控制服务 | 截图、控件树、点击、文件和项目管理 | `9096` |
| 日志回显服务 | `log`、`deploy --logs` 与 Python 日志读取 | `10102` |

本文假设手机中已经开启 AScript 服务。请先确定采用其中一种连接方式：

1. Wi-Fi 局域网：电脑能直接访问手机的 `IP:9096`。
2. USB 隧道：电脑运行 `iproxy`，ASClient 通过本机 `127.0.0.1:9096` 访问手机。

不要在配置、代码、截图或日志中提交设备密码、UDID 和内网地址。

## 2. 安装 Python 与客户端

### 2.1 安装 Python

Windows 安装 Python 3.10 或更高版本后，重新打开命令提示符，检查：

```bat
py --version
py -m pip --version
```

如果 `py` 不存在，可使用已安装 Python 对应的 `python` 命令；下文以 Windows 推荐的 `py` 为例。macOS/Linux 通常使用：

```sh
python3 --version
python3 -m pip --version
```

### 2.2 取得并安装仓库

先取得私有仓库访问权限，然后克隆仓库或进入已有工作目录：

```bat
git clone https://github.com/CoconutHR/asclient.git
cd asclient
py -m pip install --user --upgrade .
```

`--user` 会安装到当前 Windows 用户目录，不需要管理员权限。验证安装：

```bat
py -m asclient help
```

### 2.3 直接使用 `asc` 命令

安装包提供 `asc` 命令。Windows 必须把 Python 用户 Scripts 目录加入 `PATH` 后才能直接调用它。先显示该目录：

```bat
py -m site --user-base
```

将输出路径后加上 `\Scripts`，例如 `C:\Users\你的用户名\AppData\Roaming\Python\Python312\Scripts`，加入用户 `PATH`。关闭并重新打开终端后验证：

```bat
asc help
```

在 Scripts 未配置、公司电脑限制修改 `PATH`，或希望最稳定兼容时，始终使用：

```bat
py -m asclient help
```

本文的 `py -m asclient` 可等价替换为已可用的 `asc`。例如 `py -m asclient status` 等价于 `asc status`。

### 2.4 更新客户端

在仓库根目录执行：

```bat
git pull --ff-only
py -m pip install --user --upgrade .
py -m asclient help
```

升级后，PyCharm 或 VS Code 如未刷新类型提示，请重启 IDE 的 Python 语言服务。生产设备应固定到已验收的提交或标签，而非无审查地更新 `main`。

## 3. 创建配置文件

ASClient 默认读取当前目录的 `asclient.json`，不依赖环境变量。首次创建：

```bat
copy asclient.example.json asclient.json
edit asclient.json
```

配置模板的关键字段：

```json
{
  "language": "auto",
  "device": {
    "address": "192.168.3.17:9096",
    "password": "",
    "timeout": 20,
    "retries": 1
  },
  "tunnel": {
    "iproxy": "iproxy",
    "local_host": "127.0.0.1",
    "local_port": 9096,
    "remote_port": 9096,
    "local_log_port": 10102,
    "remote_log_port": 10102,
    "forward_logs": true,
    "udid": "",
    "startup_timeout": 8
  }
}
```

`language` 设为 `auto` 时，中文系统显示中文提示，其他系统显示英文；也可固定为 `zh-CN` 或 `en`。`timeout` 的单位是秒。`asclient.json` 已被 Git 忽略，不能提交到仓库。

## 4. 方式一：Wi-Fi 局域网连接

### 4.1 配置设备地址

确认电脑和 iPhone 位于可互访的网络，且手机 AScript 页面显示的开发者地址类似 `192.168.3.17:9096`。编辑配置：

```json
{
  "device": {
    "address": "192.168.3.17:9096",
    "password": ""
  }
}
```

如服务设置了密码，填入 `password`；没有密码则保留空字符串。

### 4.2 验证连接并打开 Inspector

以下命令均为只读操作：

```bat
py -m asclient doctor
py -m asclient status
py -m asclient shot evidence\wifi-screen.png
py -m asclient inspect
```

`status` 中 `available: true` 说明控制服务可访问。某些 iOS 服务版本可能显示 `status_api_error`，但只要仍返回 `screen` 与 `current_app`，即为已知的兼容降级，不代表连接失败。

`inspect` 会打开仅监听本机的中文控件检查器。确认顶部的应用名称、Bundle ID、PID 与手机前台应用一致；点击截图可读取物理像素“动作坐标”。

## 5. 方式二：USB 隧道连接

USB 适用于电脑和手机不在同一网段、不希望使用手机热点或 Wi-Fi 受隔离的情况。隧道会同时映射控制端口和日志端口：

```text
127.0.0.1:9096  -> USB -> iPhone:9096
127.0.0.1:10102 -> USB -> iPhone:10102
```

### 5.1 准备 `iproxy`

先用 USB 连接 iPhone，并在手机上完成“信任此电脑”。ASClient 不会自动下载 `iproxy`，请通过受信任渠道安装 libimobiledevice。

Windows 安装后检查：

```bat
where iproxy
iproxy --help
```

macOS 可使用：

```sh
brew install libimobiledevice
which iproxy
iproxy --help
```

如果 Windows 找不到 `iproxy.exe`，将其绝对路径写入 `asclient.json`。JSON 的反斜杠必须转义：

```json
{
  "tunnel": {
    "iproxy": "C:\\tools\\libimobiledevice\\iproxy.exe"
  }
}
```

也可让诊断命令经确认后写入该路径：

```bat
py -m asclient doctor --fix-iproxy "C:\tools\libimobiledevice\iproxy.exe"
```

### 5.2 改为本机地址并启动隧道

将配置中的地址改为本机回环地址：

```json
{
  "device": {
    "address": "127.0.0.1:9096"
  }
}
```

多台 iPhone 同时连接时，在 `tunnel.udid` 中填入目标设备 UDID。接着打开两个终端。

终端 A 保持隧道运行：

```bat
py -m asclient tunnel
```

终端 B 验证和使用：

```bat
py -m asclient doctor
py -m asclient status
py -m asclient log 10
py -m asclient inspect
```

不要关闭终端 A；按 `Ctrl+C` 会同时停止两条转发。默认转发 `10102` 日志端口，因此不要随意使用 `--no-logs`，否则 `log` 和 `deploy --logs` 无法通过 USB 工作。完整的多设备、端口调整和运维说明见 [USB 隧道运维指南](usb-tunnel.md)。

## 6. 第一个程序：Hello World

先运行一个不会点击、输入、上传或删除任何内容的脚本。新建 `hello_asclient.py`：

```python
from pathlib import Path

from asclient import AScriptClient


def main() -> None:
    # 地址从 asclient.json 读取时可自行填入，也可在此处直接指定。
    client = AScriptClient("192.168.3.17:9096", timeout=15)
    status = client.status()
    print("已连接：", status["available"])
    print("当前应用：", status.get("current_app"))
    print("物理分辨率：", client.screen_size())

    output = Path("evidence") / "hello-screen.png"
    output.parent.mkdir(exist_ok=True)
    print("截图已保存：", client.save_screenshot(output))


if __name__ == "__main__":
    main()
```

将脚本里的地址改为自己的 Wi-Fi 地址；USB 模式则填 `127.0.0.1:9096`，并确保隧道仍在运行。执行：

```bat
py hello_asclient.py
```

成功后会输出当前应用和物理分辨率，并生成 `evidence\hello-screen.png`。如果这一步成功，截图、控件树、OCR、图像匹配和自动化对象 API 的基础连接都已具备。

希望让脚本和 CLI 共用配置时，可在 Python 中读取同一配置：

```python
from asclient import AScriptClient
from asclient.config import device_options, load_config

options = device_options(load_config())
client = AScriptClient(**options)
print(client.status())
```

## 7. 第一次定位控件：使用 Inspector

1. 先在手机上打开要分析的页面。
2. 运行 `py -m asclient inspect`。
3. 顶部确认当前 App 信息；选择“智能”模式并点击“刷新”。
4. 点击截图或控件树节点，查看原始属性、矩形和候选 Python 选择器。
5. 点击“验证选择器”。仅当结果为“唯一匹配”时，才将其作为稳定选择器候选。
6. 需要图片模板时点击“裁剪保存”，在截图上拖拽矩形；PNG 会保存到启动 Inspector 的当前目录。

控件树为空并不表示 ASClient 不可用，只表示当前 App 页面没有暴露可用于语义定位的树。此时应使用截图、OCR、图色或比例坐标。

## 8. 常见错误与排查顺序

遇到问题先运行：

```bat
py -m asclient doctor --report evidence\doctor.json
```

该命令默认只读，不会安装软件、关闭其他程序或修改手机。下面是常见情况：

| 表现 | 检查与处理 |
| --- | --- |
| `No module named asclient` | 回到仓库根目录运行 `py -m pip install --user --upgrade .`，确认 IDE 和终端使用同一个 Python。 |
| `'asc' 不是内部或外部命令` | 使用 `py -m asclient`，或按 2.3 将用户 `Scripts` 目录加入 `PATH`。 |
| 连接超时、`available: false` | Wi-Fi 时核对手机开发者地址、网络互访、服务开关和密码；USB 时确认终端 A 的隧道还在运行。 |
| 找不到 `iproxy` | 先手工安装受信任的 libimobiledevice，再用 `doctor --fix-iproxy` 写入已确认的绝对路径。 |
| 本地端口被占用 | 确认是否已有 ASClient 隧道；否则关闭已知占用者，或更改 `tunnel.local_port` 与 `device.address`。不要盲目结束未知进程。 |
| `status_api_error` | 若同时有 `available: true`、`screen` 和 `current_app`，这是已知兼容降级；仍可继续截图、Inspector 和多数操作。 |
| Inspector 没有控件节点 | 切换到目标 App 的可访问页面，尝试“完整”模式；仍为空则使用 OCR、图片或坐标方案。 |
| 点击位置偏移 | 所有绝对坐标使用截图的物理像素。先用 Inspector 的“动作坐标”或 `screen_size()`，不要使用 `logical_screen`。 |
| 图像找不到 | 确保模板来自相同分辨率、缩放和页面状态；降低 `confidence` 前先检查裁剪区域和图片是否正确。 |

诊断报告不包含设备密码，可作为问题反馈的证据。反馈时还应附上客户端版本、Git 提交、iOS/AScript 版本、目标 App 版本和完整错误文本。

## 9. 功能示例

以下示例默认已建立 `client` 或 `device` 连接。`timeout`、`interval` 的单位都是秒；`duration_ms` 的单位是毫秒。

### 9.1 状态、截图、控件树与 OCR（只读）

```python
from asclient import AScriptClient

client = AScriptClient("192.168.3.17:9096")
print(client.status())
client.save_screenshot("evidence/screen.png")
open("evidence/page.xml", "w", encoding="utf-8").write(client.ui_xml(mode="smart"))
print(client.ocr())
```

CLI 等价示例：

```bat
py -m asclient status
py -m asclient shot evidence\screen.png
py -m asclient dump evidence\page.xml --mode smart
py -m asclient ocr
```

### 9.2 语义控件定位与等待

```python
from asclient import connect

device = connect("192.168.3.17:9096")
login = device(text="登录", class_name="XCUIElementTypeButton")
element = login.get(timeout=10)  # 最多等待 10 秒
print(element.info)

# 仅在已确认业务允许时执行：
element.click()
```

优先使用 Inspector 验证后的 `name`、`label` 或组合条件，避免仅按坐标或索引定位。

### 9.3 物理坐标与比例坐标

```python
from asclient import AScriptClient

client = AScriptClient("192.168.3.17:9096")
print(client.screen_size())  # 例如 (1179, 2556)，物理像素

# 以下会改变手机界面：先确认目标页面正确。
client.tap(590, 2300)
client.tap_relative(0.5, 0.92)
client.swipe_relative(0.5, 0.80, 0.5, 0.25, duration_ms=450)
```

CLI 的改变状态命令必须显式确认：

```bat
py -m asclient --yes tap-rel 0.5 0.92
py -m asclient --yes swipe-rel 0.5 0.80 0.5 0.25 --duration 450
```

### 9.4 比例裁剪截图

```python
client.save_screenshot_crop_relative(
    "evidence/bottom.png", left=0, top=0.5, right=1, bottom=1
)
```

矩形顺序为 `left, top, right, bottom`，范围是 `0..1`；示例保存屏幕下半部分。

### 9.5 等待图片、消失后继续与自动滚动

```python
# 默认先等待一个 interval 再检查；initial_delay=False 表示立刻检查一次。
match = client.wait_image(
    "assets/login.png", confidence=0.92, timeout=15,
    interval=0.5, initial_delay=False, log=True,
)
print(match.center)

# loading 图标消失后再继续；region 为比例区域 left, top, right, bottom。
client.wait_image_gone(
    "assets/loading.png", confidence=0.85, timeout=20,
    interval=0.5, region=(0.75, 0.85, 1, 1), log=True,
)

# 手指向上滑动以翻到下一屏，直到找到目标图；总超时或最大滑动次数任一到达即停止。
target = client.scroll_until_image(
    "assets/target.png", direction="up", confidence=0.90,
    timeout=30, max_swipes=8, interval=0.5, duration_ms=500, log=True,
)
```

模板必须在相同物理分辨率、缩放和界面状态下采集。`log=True` 用于输出每轮匹配情况，排查失败时再开启即可。

### 9.6 输入文本与返回主屏幕

```python
# 以下均会改变手机状态。
client.home()
client.input_text("你好", interval_ms=80)
```

输入前必须先通过点击或语义选择器取得正确焦点；`home()` 会返回系统主屏幕。在生产脚本中，执行动作前先使用选择器、当前 App 或截图验证页面状态，避免因页面跳转而误操作。

### 9.7 项目上传、运行和日志

准备入口文件 `demo.py`：

```python
print("ASCLIENT_DEMO_OK")
```

然后执行：

```bat
py -m asclient --yes deploy demo .\demo.py --logs 5
```

该命令会上传入口文件、运行项目、收集一段日志并保存截图，因会写入手机项目目录而要求 `--yes`。USB 模式需保持日志端口 `10102` 映射。项目文件和目录管理另见 API 参考的“项目与远程文件 API”。

### 9.8 在 Python 中管理 USB 隧道

```python
from asclient import AScriptTunnel, AScriptClient

with AScriptTunnel() as tunnel:
    client = AScriptClient(tunnel.address)
    print(client.status())
    print("日志地址：", tunnel.log_address)
```

`with` 块结束时会停止由当前程序启动的隧道。多设备时传入 `AScriptTunnel(udid="设备UDID")`。

## 10. 下一步

1. 日常脚本：从 [API 使用参考](api-reference.md) 查类、成员、参数、返回值和异常。
2. 稳定自动化：阅读[生产使用指南](production-guide.md)中的选择器、证据、错误处理和发布检查表。
3. USB 多设备、端口冲突和日志回显：阅读 [USB 隧道运维指南](usb-tunnel.md)。
4. 先用 `py -m asclient help` 或 `py -m asclient help <命令>` 查询 CLI，再执行任何会改变手机状态的命令。
