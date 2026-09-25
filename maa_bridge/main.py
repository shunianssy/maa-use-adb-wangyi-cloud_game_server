"""
MaaFramework 桥接命令行入口(用于服务器端/无头环境)。

安装:   pip install -r sdk/requirements.txt 及根目录 requirements.txt
运行:   python -m maa_bridge.main --base-url http://127.0.0.1:22888
可选:   --resource <maa资源目录> --task <任务名> 直接执行 MAA 任务

启动顺序(服务器端):
    1. 先启动云游戏 HTTP 服务:  python server.py
    2. 再启动本桥接:            python -m maa_bridge.main --resource ./maa_resource [--task Main]
"""

import argparse
import logging
import sys
import time
from typing import Optional

# 触发 maa 库中 libMaaFramework 的自动加载(import 时机已在包内处理)
import maa  # noqa: F401  (确保原生库被加载, 需在 controller 之前导入)

logger = logging.getLogger("maa_bridge.main")


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="网易云游戏 MAA 自定义控制器桥接")
    parser.add_argument("--base-url", default="http://127.0.0.1:22888",
                        help="云游戏 HTTP 服务地址, 默认 http://127.0.0.1:22888")
    parser.add_argument("--connect-timeout", type=int, default=180,
                        help="等待云游戏连接就绪的超时秒数, 默认 180")
    parser.add_argument("--resource", default=None,
                        help="MAA pipeline 资源 bundle 目录(加载后才可执行任务)")
    parser.add_argument("--task", default=None,
                        help="要执行的 MAA 任务名(需与 --resource 配合; 缺省则进入宿主模式)")
    parser.add_argument("--width", type=int, default=1280, help="兜底分辨率宽, 默认 1280")
    parser.add_argument("--height", type=int, default=720, help="兜底分辨率高, 默认 720")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="日志级别, 默认 INFO")
    return parser


def load_resource_and_bind(ctrl, resource_path: str):
    """加载 MAA 资源(Resource)并绑定 Tasker。

    Returns:
        tasker, 绑定失败时返回 None。
    """
    from maa.resource import Resource
    from maa.tasker import Tasker

    resource = Resource()
    job = resource.post_bundle(resource_path)
    if not job.wait().status().succeeded:
        logger.error("加载资源失败: %s", resource_path)
        return None

    tasker = Tasker()
    if not tasker.bind(resource, ctrl):
        logger.error("绑定 Tasker 失败")
        return None
    logger.info("资源加载完成: %s", resource_path)
    return tasker


def run_loop(ctrl) -> None:
    """宿主模式:连接就绪后持续驻留, 等待外部信号(如容器停止)。"""
    logger.info("Controller ready at base_url, entering host mode (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Received shutdown signal")


def main(argv: Optional[list] = None) -> int:
    """程序入口, 返回进程退出码。"""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    from maa_bridge.netease_controller import NeteaseCloudGameController

    ctrl = NeteaseCloudGameController(
        base_url=args.base_url,
        width=args.width,
        height=args.height,
        connect_timeout=args.connect_timeout,
    )

    # 1. 连接云游戏(内部会等待就绪)
    logger.info("Connecting to cloud game via %s ...", args.base_url)
    if not ctrl.connect():
        logger.error("Cloud game 连接失败, 请确认 HTTP 服务已启动且已具备有效 token")
        return 1
    logger.info("Cloud game connected, resolution %dx%d", ctrl._width, ctrl._height)

    # 2. 可选:加载资源并执行任务
    if args.resource:
        tasker = load_resource_and_bind(ctrl, args.resource)
        if tasker is None:
            return 1
        if args.task:
            job = tasker.post_task(args.task)
            result = job.wait().get()
            if result.completed:
                logger.info("任务 %s 执行成功", args.task)
                return 0
            logger.error("任务 %s 执行失败: %s", args.task, result)
            return 1
        # 资源已加载但未指定任务:退化为宿主模式
        run_loop(ctrl)
        return 0

    # 3. 纯控制器模式:持续驻留
    run_loop(ctrl)
    return 0


if __name__ == "__main__":
    sys.exit(main())