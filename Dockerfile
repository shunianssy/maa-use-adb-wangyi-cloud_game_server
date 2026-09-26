# 网易云游戏 MAA 容器镜像
# 一个容器内同时承载: 云游戏 HTTP 服务(server.py) + 官方 Linux MaaCore(一键长草)
# 说明: MAA 核心与资源(约 220MB)不在构建期下载, 由容器首次启动时自动拉取到
#       /app/maa_data(可用卷挂载持久化), 避免镜像体积膨胀。

FROM python:3.12-slim

# 避免 Python 输出缓冲, 便于容器日志实时收集
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 1) 先安装依赖以利用 Docker 层缓存
COPY sdk/requirements.txt ./sdk/requirements.txt
COPY requirements.txt ./

# 系统级依赖:
#   ffmpeg / ca-certificates: aiortc/av 运行时与 HTTPS 校验
#   说明: 官方 libMaaCore.so 及其随包库仅依赖基础 glibc(libc/libdl/libm/libpthread/
#         libgcc_s/librt), 与 python:3.12-slim(Debian) 自带一致, 无需额外安装
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt

# 2) 复制应用代码
#    token / maa_data 不复制(通过卷或环境变量注入, 见 docker-compose.yml)
COPY sdk/ ./sdk/
COPY webui/ ./webui/
COPY server.py ./
COPY maa_core_wrapper.py maa_coordinator.py maa_settings.py netease_login.py ./
COPY fake_adb/ ./fake_adb/
COPY maa_bridge/ ./maa_bridge/
COPY maa_pipeline/ ./maa_pipeline/
COPY scripts/ ./scripts/
COPY entrypoint.sh ./

RUN chmod +x entrypoint.sh fake_adb/adb.sh

# 云游戏 HTTP 服务默认端口
EXPOSE 22888

ENTRYPOINT ["./entrypoint.sh"]