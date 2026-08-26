# USB 隧道运维指南

## 目的

当电脑与 iPhone 不在同一局域网、Wi-Fi 受隔离或不希望使用手机热点时，可通过 USB 将电脑本机端口转发到 iPhone 上的 AScript 服务端口。ASClient 使用外部 `iproxy` 完成该转发，并仍按普通 TCP 地址连接：

```text
ASClient -> 127.0.0.1:9096 -> iproxy -> USB -> iPhone:9096
```

同一个 `tunnel` 命令默认还会启动日志回显端口的映射：

```text
ASClient -> 127.0.0.1:10102 -> iproxy -> USB -> iPhone:10102
```

## 前提

1. iPhone 已通过 USB 连接到该电脑并完成系统信任/配对。
2. AScript 本地服务已在手机中开启。
3. 电脑已安装受信任来源的 `iproxy`，并可从终端执行 `iproxy` 或在配置中给出其绝对路径。
4. 本地 `9096`、`10102` 未被其他程序占用。

ASClient 不下载、安装或更新 `iproxy`。这避免客户端静默执行来源不明的原生二进制，也允许团队在受控环境中统一管理 libimobiledevice 版本。

### Windows 安装与定位

安装包含 `iproxy.exe` 的受信任 Windows libimobiledevice 发行版后，在命令提示符执行：

```bat
where iproxy
iproxy --help
```

第一条命令能找到程序时，配置可保持 `"iproxy": "iproxy"`。若没有加入 `PATH`，填写可执行文件的绝对路径；JSON 中反斜杠必须写两次：

```json
{
  "tunnel": {
    "iproxy": "C:\\tools\\libimobiledevice\\iproxy.exe"
  }
}
```

### macOS 与 Linux 安装与定位

macOS 可使用 Homebrew：

```sh
brew install libimobiledevice
which iproxy
iproxy --help
```

Linux 应通过发行版的受信任软件源安装 `libimobiledevice`，再执行：

```sh
command -v iproxy
iproxy --help
```

若二进制不在 `PATH`，同样将绝对路径填入 `tunnel.iproxy`。不要将未知来源的下载文件直接加入自动化或 CI 环境。

## 配置

复制并编辑配置：

```bat
copy asclient.example.json asclient.json
edit asclient.json
```

USB 使用时将 `device.address` 设为本机回环地址：

```json
{
  "device": {
    "address": "127.0.0.1:9096",
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

`udid` 留空时由 `iproxy` 选择其默认设备。多设备环境必须填写 UDID，避免把测试命令转发到错误手机。`asclient.json` 已被 Git 忽略，不得提交密码、UDID 或内网信息。

## CLI 使用

在一个专用终端中保持隧道运行：

```bat
py -m asclient tunnel
```

启动前或出现故障时，先运行只读诊断：

```bat
py -m asclient doctor --report artifacts\usb-doctor.json
```

它会分别报告 `iproxy`、本机 `9096/10102`、设备控制服务和日志端口。端口被占用时不会自动终止其他进程；缺少 `iproxy` 时不会自动下载安装。若已手工安装但未加入 `PATH`，可在审查路径后执行 `py -m asclient doctor --fix-iproxy "D:\\tools\\libimobiledevice\\iproxy.exe"`，再确认写入配置。

成功后会显示：

```text
USB tunnel is running: service=127.0.0.1:9096 -> device:9096; logs=127.0.0.1:10102 -> device:10102.
```

在另一个终端中使用正常命令：

```bat
py -m asclient status
py -m asclient shot artifacts\usb-screen.png
py -m asclient inspect
```

临时覆盖配置：

```bat
py -m asclient tunnel --local-port 19096 --remote-port 9096 --local-log-port 11002 --remote-log-port 10102 --udid <UDID>
py -m asclient --device 127.0.0.1:19096 status
```

需要排查 HTTP 服务而不使用日志时，可传入 `--no-logs`。否则默认应保持 `forward_logs: true`，这样 `py -m asclient log`、`deploy --logs` 和 Python `client.logs()` 都会通过同一条 USB 连接工作。

`tunnel` 在前台运行，按 `Ctrl+C` 会终止由 ASClient 启动的两个 `iproxy` 进程。不要把这些进程作为后台孤儿任务长期保留。

## Python 使用

```python
from asclient import AScriptTunnel, connect

with AScriptTunnel(udid="") as tunnel:
    device = connect(tunnel.address)
    print(device.client.status())
```

`AScriptTunnel` 默认同时映射日志端口，因此日志无需额外处理：

```python
from asclient import AScriptClient, AScriptTunnel

with AScriptTunnel():
    client = AScriptClient("127.0.0.1:9096")
    for entry in client.logs(duration=5):
        print(entry.message)
```

高级调用可继续使用 `IProxyTunnel` 建立单个非标准端口映射。

## 安全边界

- `IProxyTunnel` 只接受 `127.0.0.1` 或 `localhost` 作为本地客户端地址；ASClient 不提供将 USB 隧道绑定到局域网接口的选项。
- 具体 `iproxy` 发行版的监听实现可能不同。生产设备应保持主机防火墙启用，并确认转发端口不对外暴露。
- USB 隧道绕过了 Wi-Fi 网络隔离，但不绕过手机的配对信任、AScript 服务开关或服务密码。
- 隧道仅是传输层，`eval`、项目上传、删除和自动化动作的权限/确认规则仍然有效。

## 故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `未找到 iproxy 可执行文件` / `iproxy executable not found` | 未安装、未加入 PATH 或路径写错 | Windows 执行 `where iproxy`；macOS 执行 `which iproxy`；无结果则安装受信任发行版，或将 `tunnel.iproxy` 设为绝对路径 |
| `iproxy exited during startup` | USB 未连接、设备未信任、端口冲突或 UDID 错误 | 重新插拔/解锁并信任设备；检查端口和 UDID |
| 隧道运行但 `status` 失败 | AScript 服务未开启或远端端口不对 | 在手机确认服务；检查 `remote_port` 默认应为 `9096` |
| `log` 失败 | 日志隧道被关闭、端口冲突或设备端日志服务不可用 | 移除 `--no-logs`，确认 `forward_logs` 为 `true`，检查本机 `10102` 与远端 `10102` |
| 多设备连接到错误手机 | `udid` 留空 | 在配置中固定目标 UDID |

隧道成功只说明本地端口已由 `iproxy` 接管；仍必须执行 `py -m asclient status` 验证 AScript 服务与目标 App 环境。
