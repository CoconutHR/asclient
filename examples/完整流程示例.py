"""完整流程示例：登录并验证首页。

演示如何把选择器、断言、动作和证据组装成一个可排查的自动化流程。
第 10 节的说明见 docs/从零开始使用教程.md。

使用步骤：

1. 复制 asclient.example.json 为 asclient.json，填入设备地址（Wi-Fi 为
   手机 IP:9096，USB 隧道为 127.0.0.1:9096）；
2. 运行 ``py -m asclient inspect``，在目标 App 的登录页确认下面四个占位
   选择器均为“唯一匹配”，再替换为真实值；
3. 在仓库根目录执行::

       py examples\\完整流程示例.py

本脚本会真实点击和输入手机界面。请先在测试设备与测试账号上运行，
不要对生产业务账号执行。

每次运行都会在 artifacts/ 下创建独立证据目录，内含 manifest.json；
任何步骤失败时自动写入该步骤的截图、控件树 XML 和设备状态。
"""

from asclient import Run, connect
from asclient.config import device_options, load_config

# ―― 占位选择器：用 Inspector 验证为“唯一匹配”后替换 ――
USERNAME_INPUT = "username_input"  # 用户名输入框：name
PASSWORD_INPUT = "password_input"  # 密码输入框：name
LOGIN_BUTTON = "登录"  # 登录按钮：label 文本
HOME_MARK = "home_screen"  # 登录成功后的首页标识：name

# 测试账号：不要在此填写真实个人数据。
TEST_ACCOUNT = ("demo@example.com", "demo-password")


def main() -> None:
    # 与 CLI 共用当前目录的 asclient.json，避免在代码里硬编码地址和密码。
    options = device_options(load_config())
    device = connect(**options)

    with Run(device, artifacts_root="artifacts") as run:
        # 1. 动作之前先断言页面状态：三个关键控件必须存在且唯一。
        #    assert_unique 命中多个或零个都会失败，并自动采集证据。
        #    步骤名可用中文，会直接生成证据文件名（如 断言用户名_failure.png）；
        #    / \ : 等文件名非法字符会被替换为下划线。
        username = run.assert_unique(device.selector().name(USERNAME_INPUT), name="断言用户名输入框")
        password = run.assert_unique(device.selector().name(PASSWORD_INPUT), name="断言密码输入框")
        submit = run.assert_unique(device.selector().text(LOGIN_BUTTON), name="断言登录按钮")

        # 2. 填写表单。set_text 会先点击控件取得焦点再输入；
        #    带参数的动作用 lambda 包装后交给 run.step 记录耗时。
        run.step("填写用户名", lambda: username.set_text(TEST_ACCOUNT[0]))
        run.step("填写密码", lambda: password.set_text(TEST_ACCOUNT[1]))

        # 3. 点击登录并等待首页标识。wait 超时会先采集失败证据再抛出异常，
        #    避免后续步骤在错误页面上继续执行。
        run.step("点击登录", submit.click, capture_after=True)
        run.wait(device.selector().name(HOME_MARK), timeout=15, name="等待首页")

        # 4. 如登录过程有 loading 遮罩，可在点击后用模板等待其消失；
        #    模板图片由 Inspector 的“裁剪保存”生成，放于 assets/ 目录：
        # device.client.wait_image_gone("assets/loading.png", timeout=20)

        print("登录流程通过，证据目录：", run.directory)


if __name__ == "__main__":
    main()
