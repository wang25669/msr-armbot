#!/usr/bin/env bash
# msr-armbot Armbian/Debian 一键安装。用法: sudo bash install.sh
set -e
cd "$(dirname "$0")"

echo "== 1/3 依赖检查 =="
have python3 || { echo "缺少 python3"; exit 1; }
if ! have chromium && ! have chromium-browser && ! have google-chrome; then
    echo "安装 chromium (浏览器任务需要, 纯 API 用户可跳过)..."
    apt-get update -qq && apt-get install -y chromium
fi
CHROME_BIN=$(find_chrome)
echo "浏览器: $CHROME_BIN"

echo "== 2/3 数据目录 =="
MSR_HOME="${MSR_HOME:-$HOME/.msr}"
mkdir -p "$MSR_HOME/profiles"
[ -f "$MSR_HOME/browser.json" ] || cat > "$MSR_HOME/browser.json" << EOF
{
  "binary": "$CHROME_BIN",
  "version": "$("$CHROME_BIN" --version | grep -oP '\\d+' | head -1)",
  "search_budget_minutes": 60,
  "cards_budget_minutes": 25,
  "headless": true
}
EOF
echo "配置: $MSR_HOME/browser.json (可编辑)"

echo "== 3/3 systemd timer =="
if [ "$(id -u)" = "0" ] && have systemctl; then
    RUN_USER="${SUDO_USER:-root}"
    cat > /etc/systemd/system/msr.service << EOF
[Unit]
Description=msr rewards run
After=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
WorkingDirectory=$(pwd)
ExecStart=$(which python3) -m msr run --all
TimeoutStartSec=6h
EOF
    cat > /etc/systemd/system/msr.timer << EOF
[Unit]
Description=msr daily timer

[Timer]
OnCalendar=*-*-* 01:30:00
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable --now msr.timer
    echo "timer 已启用 (每天 01:30 + 随机30分钟抖动). 查看: systemctl list-timers msr*"
else
    echo "(无 systemd/root, 跳过. 可用 crontab: 30 1 * * * cd $(pwd) && python3 -m msr run --all)"
fi

echo "完成. 下一步: python3 -m msr login <账号名>"
