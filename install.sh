#!/usr/bin/env bash
# msr-armbot Armbian/Debian 一键安装。用法: sudo bash install.sh
set -e
cd "$(dirname "$0")"

have() { command -v "$1" >/dev/null 2>&1; }
find_chrome() {
    for c in chromium-browser chromium google-chrome edge msedge; do
        if have "$c"; then
            command -v "$c"
            return 0
        fi
    done
    return 1
}

echo "== 1/3 依赖检查 =="
have python3 || { echo "缺少 python3，请先安装: apt-get install -y python3"; exit 1; }
if ! have chromium && ! have chromium-browser && ! have google-chrome; then
    echo "安装 chromium 与中文字体..."
    apt-get update -qq && apt-get install -y chromium fonts-wqy-zenhei fonts-wqy-microhei
fi
CHROME_BIN=$(find_chrome || true)
echo "浏览器: ${CHROME_BIN:-未找到 (可后续指定或仅使用API功能)}"

echo "== 2/3 数据目录 =="
MSR_HOME="${MSR_HOME:-$HOME/.msr}"
mkdir -p "$MSR_HOME/profiles"
[ -f "$MSR_HOME/browser.json" ] || cat > "$MSR_HOME/browser.json" << EOF
{
  "binary": "$CHROME_BIN",
  "version": "$(${CHROME_BIN:-true} --version 2>/dev/null | grep -oP '\d+' | head -1 || echo '141')",
  "headless": true
}
EOF
echo "配置: $MSR_HOME/browser.json (可编辑)"

echo "== 3/3 systemd 服务注册 =="
if [ "$(id -u)" = "0" ] && have systemctl; then
    RUN_USER="${SUDO_USER:-root}"
    PY_BIN=$(which python3)
    CUR_DIR=$(pwd)
    # 注册常驻 Web 控制台与内置调度服务 (托管多账号独立每日定时，默认最大2并发)
    cat > /etc/systemd/system/msr-web.service << EOF
[Unit]
Description=MSR-ArmBot Web Console and Scheduler
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$CUR_DIR
ExecStart=$PY_BIN -m msr web --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now msr-web.service
    echo "msr-web.service 已注册并启动 (内置调度引擎托管多账号独立每日定时，默认最大 2 并发)。"
    echo "Web 控制台已就绪: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo '127.0.0.1'):8080"
else
    echo "(无 systemd/root，跳过系统服务注册。可在后台运行: python3 -m msr web --port 8080)"
fi

echo "完成。下一步: 访问 Web 控制台配置账号，或执行: python3 -m msr login <账号名>"
