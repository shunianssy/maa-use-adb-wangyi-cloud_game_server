# 网易云游戏 MAA 桥接服务 - 服务器端镜像
# 目标:一个容器内同时承载「云游戏 HTTP 服务(server.py)」与「MAA 桥接(bridge)」

FROM python:3.12-slim

# 避免 Python 输出缓冲, 便于容器日志实时收集
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 1) 先安装依赖以利用 Docker 层缓存
COPY sdk/requirements.txt ./sdk/requirements.txt
COPY requirements.txt ./

# 系统级依赖: aiortc/av 在部分平台需要 ffmpeg 运行时
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt

# 2) 复制应用代码
#    token 文件不复制(通过卷或环境变量注入)
COPY sdk/ ./sdk/
COPY server.py ./
COPY maa_bridge/ ./maa_bridge/
COPY maa_coordinator.py ./
COPY maa_pipeline/ ./maa_pipeline/
COPY entrypoint.sh ./

RUN chmod +x entrypoint.sh

# 云游戏 HTTP 服务默认端口
EXPOSE 22888

ENTRYPOINT ["./entrypoint.sh"]