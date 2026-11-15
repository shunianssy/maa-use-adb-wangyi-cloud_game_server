# 网易云游戏 MAA 容器镜像
# 一个容器内同时承载: 云游戏 HTTP 服务(server.py) + 官方 Linux MaaCore(一键长草)
# 说明: MAA 核心与资源(约 220MB)不在构建期下载, 由容器首次启动时自动拉取到
#       /app/maa_data(可用卷挂载持久化), 避免镜像体积膨胀。

FROM python:3.12-slim

# 国内构建加速(可选): 通过 --build-arg 覆盖, 默认使用官方源
#   APT_MIRROR        : Debian APT 镜像主机(如 mirrors.aliyun.com)
#   APT_MIRROR_SCHEME : APT 镜像协议(http/https, 国内建议 https)
#   PIP_INDEX_URL     : PyPI 镜像地址(如 https://mirrors.aliyun.com/pypi/simple/)
#   PIP_TRUSTED_HOST  : PyPI 镜像主机(https 镜像可省略)
# 使用 docker-compose.cn.yml 时会自动传入阿里云镜像参数
ARG APT_MIRROR="deb.debian.org"
ARG APT_MIRROR_SCHEME="http"
ARG PIP_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""

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
#   APT_MIRROR: 传入镜像主机时替换 Debian 默认源(兼容 deb822/debian.sources 与旧 sources.list)
#   同时强制 IPv4 + 重试: 国内 Docker 构建常因 IPv6/DNS 抖动出现 Ign/Err, 强制 IPv4 更稳
RUN set -eux; \
    if [ "${APT_MIRROR}" != "deb.debian.org" ]; then \
        sed -i -E "s|https?://deb\.debian\.org|${APT_MIRROR_SCHEME}://${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null \
        || sed -i -E "s|https?://deb\.debian\.org|${APT_MIRROR_SCHEME}://${APT_MIRROR}|g" \
            /etc/apt/sources.list 2>/dev/null \
        || true; \
        printf 'Acquire::ForceIPv4 "true";\nAcquire::Retries "5";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' \
            > /etc/apt/apt.conf.d/99china-mirror; \
        echo "[build] APT mirror: ${APT_MIRROR_SCHEME}://${APT_MIRROR}"; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# PyPI 镜像: 传入 PIP_INDEX_URL 时走镜像(国内加速), 否则直连官方源
RUN if [ -n "${PIP_INDEX_URL}" ]; then \
        pip install --no-cache-dir -i "${PIP_INDEX_URL}" \
            --trusted-host "${PIP_TRUSTED_HOST:-mirrors.aliyun.com}" -r requirements.txt; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi

# 2) 复制应用代码
#    token / maa_data / 账号配置不复制(通过卷或环境变量注入, 见 docker-compose.yml)
COPY sdk/ ./sdk/
COPY webui/ ./webui/
COPY server.py ./
COPY maa_core_wrapper.py maa_coordinator.py maa_settings.py netease_login.py ./
COPY accounts.py account_settings.py ./
COPY fake_adb/ ./fake_adb/
COPY maa_bridge/ ./maa_bridge/
COPY maa_pipeline/ ./maa_pipeline/
COPY scripts/ ./scripts/
COPY entrypoint.sh ./

RUN chmod +x entrypoint.sh fake_adb/adb.sh

# 云游戏 HTTP 服务默认端口
EXPOSE 22888

ENTRYPOINT ["./entrypoint.sh"]