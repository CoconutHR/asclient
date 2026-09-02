"""循环删除任务示例：通过 USB 隧道右滑调出操作项，逐条删除任务。

流程：
1. 通过 USB 隧道连接手机（AScriptTunnel.from_config 读取 asclient.json 的
   tunnel 段，默认同时映射控制端口 9096 与日志端口 10102）；
2. 后台线程实时回显设备日志；
3. 从 (900, 400) 右滑到 (300, 400)，调出「删除」操作项；
4. 在区域 (800, 320) ~ (1150, 550) 内查找并点击「删除.png」；
5. 在相同区域查找并点击「删除该任务.png」完成确认；
6. 每次查找立即开始、最多等待 5 秒；超时未出现即跳过本轮。
   右滑前会先等待 0.5 秒让上一轮的删除动画走完、界面稳定。
   连续两轮右滑后都找不到「删除」时才判定没有更多可删除项并终止循环。

依赖：
- 需要 Pillow 支持模板匹配，安装 ``pip install "asclient[vision]"``
  （同时装入 OpenCV 加速模糊匹配，未装 OpenCV 时自动降级纯 Pillow）；
- 需要本机已安装 iproxy（libimobiledevice）并已通过 USB 连接、信任手机；
- 模板图片「删除.png」「删除该任务.png」放在本脚本同目录，可用
  ``py -m asclient inspect`` 的「裁剪保存」生成。

使用步骤（在仓库根目录执行）：
1. 准备 asclient.json（``py -m asclient init``），确认真实设备通过 USB
   连接且 UDID 已配置；
2. 将两张模板图片放到 examples/ 目录；
3. 运行::

       py examples\\循环删除任务.py

本脚本会真实滑动并点击手机界面，请先在测试设备与测试账号上运行。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from asclient import AScriptClient, AScriptTunnel

# ―― 可调参数 ――
# 右滑手势：从 (SWIPE_START_X, SWIPE_START_Y) 滑到 (SWIPE_END_X, SWIPE_END_Y)
SWIPE_START_X, SWIPE_START_Y = 900, 400
SWIPE_END_X, SWIPE_END_Y = 300, 400

# 查找区域：用户给定 (1150, 550) 与 (920, 320) 两个对角点，并向左扩展 100px。
# find_image 的 region 采用 (left, top, right, bottom) 且要求 left<right、
# top<bottom，故规范化为 min/max 对角点后，左边界 left 再减 100。
REGION = (800, 320, 1150, 550)

# 模板图片路径（相对本脚本所在目录）
DELETE_IMAGE = Path(__file__).with_name("删除.png")
DELETE_TASK_IMAGE = Path(__file__).with_name("删除该任务.png")

# 模板匹配置信度阈值。
# 模板是从当前界面精确裁剪的，0.95 会先走字节级精确匹配（_find_exact），
# 未命中时再走 OpenCV 模糊匹配（装有 opencv-python-headless 时自动加速）；
# 若漏检可回退到 0.9。
CONFIDENCE = 0.95
# 每次查找图片的轮询间隔（秒）；查找立即开始，不额外等待首查延迟
INTERVAL = 0.5
# 每次查找图片最多等待的时长（秒），超时未出现即跳过本轮
WAIT_TIMEOUT = 5.0
# 连续 MISS_LIMIT 轮右滑后都找不到「删除」时，判定没有更多可删除项并终止
MISS_LIMIT = 2
# 右滑前等待时长（秒）：让上一轮点击「删除该任务」后的删除动画走完、界面稳定，
# 否则列表项仍在移动时右滑会失效，出现隔一轮命中、隔一轮 miss 的抖动。
BEFORE_SWIPE_DELAY = 0.5


def _check_template(template: Path) -> bool:
    """检查脚本同目录下模板图片是否存在，缺 Pillow 时给出可执行提示。"""
    try:
        import PIL  # noqa: F401  # 触发模板匹配依赖检测
    except ImportError:
        print("缺少 Pillow，无法进行模板匹配。请先执行：")
        print('    pip install "asclient[vision]"')
        sys.exit(1)
    if not template.is_file():
        print(f"找不到模板图片：{template}")
        print("请使用 `py -m asclient inspect` 的「裁剪保存」生成对应模板。")
        sys.exit(1)


def _start_log_listener(client: AScriptClient) -> tuple[threading.Thread, threading.Event]:
    """在后台线程实时回显设备日志，返回 (线程, 停止事件)。"""
    stop_event = threading.Event()

    def listen() -> None:
        try:
            for entry in client.logs(stop_event=stop_event):
                print(f"[日志] {entry.message}", flush=True)
        except Exception as exc:  # 日志线程异常不应中断主流程
            print(f"[日志] 回显终止：{type(exc).__name__}: {exc}", flush=True)

    thread = threading.Thread(target=listen, name="log-listener", daemon=True)
    thread.start()
    return thread, stop_event


def main() -> None:
    _check_template(DELETE_IMAGE)
    _check_template(DELETE_TASK_IMAGE)

    # 通过 USB 隧道连接：from_config 读取 asclient.json 的 tunnel 段，
    # with 退出（含异常）时自动停止两条端口映射。
    with AScriptTunnel.from_config() as tunnel:
        print(f"USB 隧道已建立：控制 {tunnel.address}，日志 {tunnel.log_address}")
        client = AScriptClient("127.0.0.1:9096")

        log_thread, stop_event = _start_log_listener(client)

        try:
            round_no = 0
            miss_count = 0
            while True:
                round_no += 1

                # 1. 右滑调出操作项（先等待上一轮的删除动画完成、界面稳定）
                time.sleep(BEFORE_SWIPE_DELAY)
                print(f"\n—— 第 {round_no} 轮 ——")
                print(f"右滑：({SWIPE_START_X}, {SWIPE_START_Y}) -> ({SWIPE_END_X}, {SWIPE_END_Y})")
                client.swipe(SWIPE_START_X, SWIPE_START_Y, SWIPE_END_X, SWIPE_END_Y)

                # 2. 查找「删除」入口：立即开始，最多等 5 秒；超时即本轮无结果
                try:
                    delete = client.wait_image(
                        DELETE_IMAGE,
                        confidence=CONFIDENCE,
                        region=REGION,
                        timeout=WAIT_TIMEOUT,
                        interval=INTERVAL,
                        initial_delay=False,
                    )
                except TimeoutError:
                    miss_count += 1
                    print(f"等待 {WAIT_TIMEOUT:.0f} 秒未找到「删除」（连续 {miss_count} 轮），跳过本轮。")
                    if miss_count >= MISS_LIMIT:
                        print(f"连续 {MISS_LIMIT} 轮未找到「删除」，判定无更多可删除项，终止循环。")
                        break
                    continue
                miss_count = 0
                print(f"找到「删除」({delete.confidence:.3f})，点击中心 {delete.center}")
                client.tap(*delete.center)

                # 3. 查找并点击「删除该任务」完成确认：立即开始，最多等 5 秒
                try:
                    confirm = client.wait_image(
                        DELETE_TASK_IMAGE,
                        confidence=CONFIDENCE,
                        region=REGION,
                        timeout=WAIT_TIMEOUT,
                        interval=INTERVAL,
                        initial_delay=False,
                    )
                except TimeoutError:
                    print(f"等待 {WAIT_TIMEOUT:.0f} 秒未找到「删除该任务」，跳过确认，进入下一轮。")
                    continue
                print(f"找到「删除该任务」({confirm.confidence:.3f})，点击中心 {confirm.center}")
                client.tap(*confirm.center)
        finally:
            stop_event.set()
            log_thread.join(timeout=5)

    print("\n循环结束：右滑后区域内已无「删除」与「删除该任务」。")


if __name__ == "__main__":
    main()
