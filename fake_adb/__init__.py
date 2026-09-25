"""假 adb 桥包: 把 MaaCore 的 adb 命令转发到本项目 HTTP 接口。"""

from .fake_adb import dispatch  # noqa: F401