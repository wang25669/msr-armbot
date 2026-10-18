# MSR-ArmBot 跨平台多架构容器镜像 (支持 ARM64 与 x86_64)
FROM python:3.11-slim-bookworm

LABEL maintainer="wang25669"
LABEL description="Microsoft Rewards automation container with web console"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    MSR_HOME=/data \
    WEB_PORT=8080 \
    CRON_SCHEDULE="30 1 * * *"

# 安装 Chromium、中文字体、cron 定时器及基础依赖 (保留 tzdata 供宿主机挂载或环境变量适配)
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    fonts-wqy-zenhei \
    fonts-wqy-microhei \
    cron \
    ca-certificates \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/local/bin/python3 /usr/bin/python3 \
    && ln -sf /usr/local/bin/python /usr/bin/python

WORKDIR /app

# 复制代码
COPY . /app

RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8080

VOLUME ["/data"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
