# ASClient 生产使用指南

## 1. 目标与边界

ASClient 是 AScript iOS 本地开发服务的 Python 客户端。它不修改 IPA、不会向手机安装组件；所有自动化、文件和日志操作均调用设备现有的 HTTP（默认 `9096`）和 WebSocket（默认 `10102`）服务。

当前版本提供两层能力：

| 层 | 入口 | 用途 |
| --- | --- | --- |
| 传输/API 层 | `AScriptClient` | 截图、控件树、坐标操作、项目部署、日志、OCR 与原始接口调用 |
| 自动化层 | `connect()` / `Device` | 类 uiautomator2 的元素查询、断言、点击与输入 |
| 可视化层 | `py -m asclient inspect` | 本机浏览器中的截图、控件树、属性和选择器检查 |

它不是外部 WebDriver 服务，也不承诺每个 iOS App 都能暴露完整无障碍树。目标 App 必须在当前 AScript/WDA 实现下返回有效的 `/api/tool/view/dump` 数据，语义选择器才可用。控件树为空时，仍可使用截图、OCR、图色和坐标操作，但不能凭空生成可靠的语义控件选择器。

## 2. 支持矩阵

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 连通性、截图、OCR、坐标点击/滑动/输入 | 已在 iOS AScript 4001 真机验证 | 坐标以 AScript 屏幕坐标为准 |
| 项目创建、上传、运行、日志、截图 | 已在 iOS AScript 4001 真机验证 | `deploy` 已完成端到端验证 |
| `status` | 兼容降级 | 已知 iOS 4001 上可能因 `languageCode` 调用错误而降级返回屏幕和当前 App |
| 包列表 | 已有降级实现 | `status` 无包信息时由设备端 Python 查询 |
| 元素树与语义选择器 | 取决于目标 App | 先用 Inspector 验证，再作为生产测试依赖 |
| Inspector | 本机客户端实现 | 默认仅监听 `127.0.0.1`，不改手机端 |

## 3. 安装、更新与版本控制

### 前提

- Windows Python 3.10 或更高版本。
- PC 和 iPhone 在可达网络中，且设备服务已开启。
- 私有仓库访问权限已配置。

首次安装或更新，从仓库根目录执行：

```bat
git pull --ff-only
py -m pip install --user --upgrade .
copy asclient.example.json asclient.json
py -m asclient status
```

编辑 `asclient.json` 中的 `device.address`、`device.password`、`device.timeout` 和 `device.retries`。真实配置已被 `.gitignore` 排除，不应提交。单次临时覆盖可使用 `--device`、`--password` 或 `--timeout`。

使用 `py -m asclient`，不要依赖 Windows `Scripts` 目录是否已加入 `PATH`。`asc` 命令仅在该目录已加入 `PATH` 时可用。

生产环境不应无审查地拉取最新 `main`。推荐流程：

```bat
git fetch origin --tags
git checkout <已验收的提交或标签>
py -m pip install --user --upgrade .
py -m unittest discover -s tests -v
```

发布前记录 Git 提交、包版本、设备型号、iOS 版本、AScript 版本和目标 App 版本；这五项构成问题复现的最小环境信息。

## 4. 连通性验收

每次新设备、网络切换或 AScript 更新后，先执行：

```bat
py -m asclient ping
py -m asclient status
py -m asclient shot evidence\baseline.png
py -m asclient dump evidence\baseline.xml
py -m asclient ocr
```

验收标准：

1. `ping` 成功并返回平台信息。
2. `status` 的 `available` 为 `true`。出现 `status_api_error` 时，它表示已启用 iOS 4001 兼容降级，而不是设备不可用；仍需确认 `screen` 和 `current_app` 存在。
3. 截图文件非空且画面符合预期。
4. XML/Inspector 能反映目标 App 的树。空 Application 根节点表示该页不可使用语义定位，应转为 OCR/图色方案或调整 App 页面。
5. OCR 至少能识别一项预期文本或图像流程能定位预期模板。

不要把“能 ping 通”当作自动化已可用的证明。

真机 smoke 使用独立配置文件，避免环境变量和误操作：复制 `tests\integration.example.json` 为 `tests\integration.json`，填入设备信息与已验证的唯一选择器，再把 `enabled` 改为 `true`：

```bat
py -m unittest discover -s tests -p test_integration.py -v
```

该套件默认只读取设备状态、截图、树、日志端口与可选选择器，并将证据写入 `artifacts\integration`；不执行点击、输入、上传、删除或部署。

## 5. Python 自动化 API

### 5.1 建立连接

```python
from asclient import AScriptClient, connect

# 低层 API：适合文件、日志、OCR、项目管理和经过确认的原始端点。
client = AScriptClient("192.168.3.17:9096", timeout=15, retries=1)

# 高层对象 API：适合 UI 自动化。
device = connect("192.168.3.17:9096", timeout=15, retries=1)
```

`timeout` 是单个网络请求的秒数；`retries` 仅重试连接错误，不重试 HTTP 错误或设备明确返回的业务错误。对于非幂等动作（点击、输入、运行项目），不要依赖客户端自动重试来实现业务重试。

### 5.2 元素查询与动作

```python
from asclient import connect

device = connect("192.168.3.17:9096")

login = device(name="login_button")
if not login.exists:
    raise RuntimeError("login button is absent")
login.click()

account = device(class_name="XCUIElementTypeTextField", name="account")
account.set_text("demo@example.com")

# 文本通常映射到 iOS 节点 label；包含匹配只在精确属性不稳定时使用。
confirm = device.selector().text("确认", contains=True)
element = device.find(confirm, timeout=5)
if element is None:
    raise RuntimeError("confirmation control did not appear within 5 seconds")
element.click()
```

`device(...)` 返回 `UiCollection`：

| 成员 | 行为 |
| --- | --- |
| `.exists` | 重新查询并返回是否至少匹配一个元素 |
| `.count` | 重新查询并返回匹配数 |
| `.info` | 返回第一个元素的元数据；无匹配时抛出 `LookupError` |
| `.all()` | 返回所有匹配元素 |
| `.get(timeout=0)` | 返回第一个元素或 `None`；超时单位为秒 |
| `.click()` | 点击第一个元素的矩形中心；无匹配时抛出 `LookupError` |
| `.set_text(text)` | 点击第一个元素后通过 AScript 输入接口输入文本 |

`UiObject.info` 通常包含 `type`、`name`、`label`、`value`、`enabled`、`visible`、`index`、`traits` 以及 `x/y/width/height`。设备返回字段可能随 App 和 AScript 版本变化，因此测试断言应只依赖实际检查过的字段。

### 5.3 选择器规则

优先级从高到低：

1. 唯一且稳定的 `name`（通常是 accessibility identifier）。
2. 唯一且稳定的 `label` 或 `value`。
3. `type + name/label` 的组合。
4. 仅在视觉位置稳定、语义树不可用时使用 OCR/图色或坐标。

不要把 `index`、完整路径、临时文案、动态数量、随机 ID 当成生产选择器。它们很容易因 A/B 实验、国际化、列表排序或页面重构失效。

```python
# 推荐：稳定标识。
device(name="checkout_submit").click()

# 可接受：类型和固定文本组合。
device(text="继续", class_name="XCUIElementTypeButton").click()

# 仅用于辅助诊断：坐标点探测，不是跨版本稳定定位方案。
node = device.find(device.selector().at(200, 600))
```

每个关键选择器应在验收中验证唯一性：`collection.count == 1`。在失败信息中输出当前 `dump_hierarchy()`、截图路径和 `collection.count`，而不是只报告“点击失败”。

### 5.4 坐标、OCR 与图色

```python
client.tap(200, 600)
client.swipe(300, 700, 300, 250, duration_ms=350)
client.input_text("hello", interval_ms=120)
ocr_result = client.ocr()
```

OCR 返回的坐标可能是物理像素，而节点树和 `tap` 通常使用 AScript 屏幕坐标。不要直接混用。应根据 `status().get("screen")`、截图尺寸及真机点击结果建立一次目标设备的坐标换算，并将换算封装在项目代码中。

## 6. Inspector 工作流

在目标 App 已打开到待分析页面时执行：

```bat
py -m asclient inspect
```

浏览器打开后：

1. 选择 `Smart`，点击 Refresh，确认顶部显示的 App 名称、Bundle ID、PID 与待测 App 一致，且树中有实际节点。
2. 点击截图或左侧树中的节点，核对矩形、`name`、`label`、`type` 和可见状态。
3. 使用页面生成的选择器作为起点，点击 Verify selector，并将结果为唯一匹配的组合写入代码。
4. 在 Python 中调用 `.count`、`.info` 和 `.click()` 进行真机验证。
5. 根据需要拖动两条面板分隔线；中间截图会始终等比缩放。`Smart` 树缺节点时尝试 `Full`；仍为空时记录截图和 XML，并使用 OCR/图色作为降级方案。Inspector 的验证动作只读，不会点击设备。

Inspector 默认使用随机端口并只绑定 `127.0.0.1`。不要通过 `--host 0.0.0.0` 暴露它：Inspector 可代表浏览器向手机发起截图和控件树读取，扩大监听范围没有生产必要。

## 7. 项目部署与日志

```bat
py -m asclient --device 192.168.3.17:9096 deploy smoke .\smoke.py --logs 5
py -m asclient --device 192.168.3.17:9096 files smoke
py -m asclient --device 192.168.3.17:9096 pull smoke .\artifacts\smoke
py -m asclient --device 192.168.3.17:9096 remove smoke
```

建议将每次部署的控制台输出、截图、日志、设备状态和提交 SHA 存入 CI 工件或测试归档。`pull` 只能下载设备服务列出的实际项目文件；如果项目文件树中不包含资源目录，客户端不会猜测或伪造资源文件。

## 8. 安全要求

1. 设备服务应只运行在可信局域网，不应映射到公网或不受控 Wi-Fi。
2. 将密码保存于被 Git 忽略的 `asclient.json`，不要写入源代码、批处理文件、日志或截图。CI 中应由受保护的密钥步骤生成临时配置文件。
3. `eval` 直接执行设备端 Python；它只可用于受信任的维护脚本，禁止接收终端用户输入、网页参数或未审查的 CI 变量。
4. `push`、`remove`、`rename`、`run` 和 `deploy` 会改变设备状态。生产脚本必须明确项目名，禁止由不可信输入拼接项目名或远程路径。
5. Inspector 默认本机监听。保持此默认值，且不要在录屏或日志中泄露页面敏感信息。
6. 自动化账号遵循最小权限原则，测试数据必须可清理、可复现且不包含真实个人数据。

## 9. 错误处理与故障排查

| 现象 | 常见原因 | 处理 |
| --- | --- | --- |
| `cannot reach AScript device` | 地址、网络、端口或服务未启动 | 检查 iPhone IP、Wi-Fi 隔离、服务状态；先跑 `ping` |
| `HTTP 500` | 设备端接口不接受路径或当前版本存在缺陷 | 用 `files` 确认远程文件存在；记录端点和响应，不要静默吞掉 |
| `status_api_error` | iOS 4001 已知 status 实现兼容问题 | 使用降级返回的 `screen/current_app` 验证连通性；不要按 status 全字段做断言 |
| XML 只有空 Application | 当前页面未暴露无障碍树 | 切换目标 App/页面，尝试 Inspector 的 Full；必要时使用 OCR/图色 |
| `.exists` 为 false | 选择器字段不匹配、页面尚未出现或树模式不对 | 在 Inspector 中核对属性，使用 `.get(timeout=...)`，避免盲目增加 sleep |
| 点击位置偏移 | 像素与点坐标混用、横竖屏变化或截图缩放 | 对照节点 `rect` 与截图；建立目标设备坐标换算 |
| 日志无输出 | 项目未运行、日志窗口太短或 WebSocket 端口不可达 | 增加 `--logs` 时长，检查 `run` 结果和 10102 连通性 |

捕获异常时保留类型和设备端响应：

```python
from asclient import DeviceConnectionError, DeviceOperationError, DeviceResponseError

try:
    device(name="checkout_submit").click()
except DeviceConnectionError as exc:
    # 网络/服务不可达：可重试整个测试用例，但要限制次数。
    raise RuntimeError(f"device unavailable: {exc}") from exc
except DeviceResponseError as exc:
    # HTTP/协议问题：记录 status/body，通常不应把同一请求无条件重放。
    raise RuntimeError(f"device HTTP error {exc.status}: {exc.body}") from exc
except (DeviceOperationError, LookupError) as exc:
    # 业务/定位失败：采集截图和树用于定位。
    raise RuntimeError(f"automation action failed: {exc}") from exc
```

## 10. 自动化项目结构建议

```text
automation-project/
  requirements.txt
  config/
    devices.json                 # 不含密码
  pages/
    login.py                     # 页面对象和稳定选择器
    checkout.py
  tests/
    test_login.py
  artifacts/
    <run-id>/                    # 截图、XML、日志、状态
  scripts/
    smoke.py
```

将选择器集中到页面对象中，不要散落在测试用例。每个页面对象操作后应返回可验证的页面状态，而不是只执行点击。例如“提交订单”应等待订单成功标识出现，而不是 `sleep(2)` 后直接判定通过。

## 11. 发布前检查表

- [ ] 已锁定并记录 ASClient Git 提交和包版本。
- [ ] 已在目标 iPhone、目标 iOS、目标 App 版本上执行连通性验收。
- [ ] 所有关键页面已用 Inspector 核验选择器，并确认每个关键选择器唯一。
- [ ] 每条关键流程都保存失败截图、XML/树、日志和设备状态。
- [ ] 已验证网络中断、目标元素缺失、页面加载变慢的失败行为。
- [ ] 未在代码、仓库、工件或控制台中泄露服务密码或业务敏感数据。
- [ ] 已测试安装、升级和回滚到上一已验收提交。
- [ ] 已运行 `py -m unittest discover -s tests -v`，并执行目标 App 真机冒烟测试。

## 12. 回滚

客户端升级出现回归时，回滚 PC 端包即可，不需要修改手机端：

```bat
git log --oneline
git checkout <上一已验收提交>
py -m pip install --user --upgrade .
py -m asclient status
```

回滚前保留失败版本的提交 SHA、命令输出和工件。不要通过手改 `site-packages` 回滚；这会造成版本不可追踪，且下次安装会被覆盖。
