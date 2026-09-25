"""
maa_bridge:将 MaaFramework 的自定义控制器(CustomController)
翻译为本项目 server.py 的 HTTP 接口,使 MAA 任务管线能直接运行于网易云游戏画面。

模块:
- netease_controller:基于 HTTP 的云游戏控制器实现
- main:命令行入口(连接云游戏、可选加载资源并执行 MAA 任务)
"""

__version__ = "0.1.0"