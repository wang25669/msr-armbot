#!/bin/bash
set -e

MSR_DATA="${MSR_HOME:-/data}"
mkdir -p "$MSR_DATA"

echo "=== MSR-ArmBot 容器正在启动 ==="
echo "数据目录: $MSR_DATA"
echo "Web 控制台端口: ${WEB_PORT:-8080}"
echo "定时执行表达式: ${CRON_SCHEDULE:-30 1 * * *}"

# 配置系统 Cron 任务
CRON_FILE="/etc/cron.d/msr-cron"
echo "${CRON_SCHEDULE:-30 1 * * *} root cd /app && MSR_HOME=$MSR_DATA python3 -m msr run --all >> $MSR_DATA/cron.log 2>&1" > "$CRON_FILE"
chmod 0644 "$CRON_FILE"
crontab "$CRON_FILE"
service cron start

echo "Cron 定时服务已启动。"
echo "正在启动 MSR-ArmBot Web 控制台..."

# 启动 Web 前端服务作为主进程
exec python3 -m msr web --host 0.0.0.0 --port "${WEB_PORT:-8080}"
