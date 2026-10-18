#!/bin/bash
set -e

MSR_DATA="${MSR_HOME:-/data}"
mkdir -p "$MSR_DATA"

echo "=== MSR-ArmBot 容器正在启动 ==="
echo "数据目录: $MSR_DATA"
# 如果外部传入了自定义命令，则直接执行
if [ $# -gt 0 ]; then
    exec "$@"
fi

echo "Web 控制台端口: ${WEB_PORT:-8080}"

# 系统级 Cron 默认由 Web 内置调度器托管，避免双重调度竞争 runner.lock
# 如需启用系统级 Cron，设置环境变量 ENABLE_SYSTEM_CRON=true
if [ "${ENABLE_SYSTEM_CRON:-false}" = "true" ]; then
    echo "系统 Cron 调度已启用: ${CRON_SCHEDULE:-30 1 * * *}"
    CRON_FILE="/etc/cron.d/msr-cron"
    cat <<EOF > "$CRON_FILE"
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
${CRON_SCHEDULE:-30 1 * * *} root cd /app && MSR_HOME=$MSR_DATA python3 -m msr run --all >> $MSR_DATA/cron.log 2>&1
EOF
    chmod 0644 "$CRON_FILE"
    crontab "$CRON_FILE"
    service cron start
    echo "系统 Cron 服务已启动。"
else
    echo "每日定时任务由 Web 控制台内置调度器托管（默认每天 01:30 自动唤醒，可于 Web 控制台动态修改与查看实时日志）。"
fi

echo "正在启动 MSR-ArmBot Web 控制台..."
exec python3 -m msr web --host 0.0.0.0 --port "${WEB_PORT:-8080}"
