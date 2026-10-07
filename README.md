# msr-armbot

Microsoft Rewards 自动化, 为 ARM Linux (Armbian) 设计。Python stdlib-only, 无第三方依赖。

## 特性

- **多账号**: 每账号独立 profile(token/cookie/状态/日志互不干扰), 串行执行, 账号间随机等待 15-40 分钟
- **区域自适应**: 区域从 `/dapi/me` 运行时探测(`profile.attributes.country`, 账号属性与出口 IP 无关); 搜索域按区域决定(cn 账号走 cn.bing.com); 浏览器任务按当天实际卡片执行, 卡片不存在自然跳过 —— 零区域硬编码
- **API 侧任务零浏览器**: App签到(type103)/移动阅读(type101)/Edge打卡(动态 offerid) 纯 HTTP
- **浏览器侧**: headless chromium + CDP(纯 stdlib WebSocket 实现, 含断线重连), 无 X11/xdotool 依赖
- **资源占用**: API 阶段 ~30 MB; 浏览器阶段单实例 headless chromium(~400 MB), 跑完即释放
- **多层容错**: API 指数退避重试(5/10/20s); per-task 隔离(单任务失败不连累其他); refresh_token 重试+旧 access_token 降级

## 安装 (Armbian)

```bash
git clone https://github.com/wang25669/msr-armbot.git
cd msr-armbot
sudo bash install.sh
```

install.sh 做三件事: 装 chromium(缺才装) -> 写默认配置 -> 注册 systemd timer。不想用 systemd 就手动跑(见"定时")。

## 登录账号

```bash
python3 -m msr login <账号名>
```

1. 终端显示 `verification_uri` + `user_code`
2. 手机/任意浏览器打开该网址, 输入代码, 登录微软账号(含 2FA)
3. 自动拿到 token 并探测区域, 写入 `~/.msr/profiles/<名>/`

### 有浏览器侧任务的账号, 补一步 cookie 导入

浏览器任务(搜索/每日活动卡)需要 bing.com 登录 cookie:

1. 电脑浏览器登录 bing.com
2. Cookie-Editor 类插件导出 cookies (JSON)
3. `python3 -m msr login <账号名> --cookies cookies.json`

兜底(cookie 导入失败/风控要重新验证): SSH 隧道远程登录

```bash
# Armbian: headless chromium 开调试端口
chromium --headless=new --remote-debugging-port=9222 --user-data-dir=/tmp/x
# 本地: ssh -L 9222:127.0.0.1:9222 user@armbian
# 本地 Chrome 打开 chrome://inspect -> inspect 登录页直接操作
```

refresh_token 90 天滚动续期(每次成功刷新就续), 正常不需要反复登录。失效时报告显示 `token失效需重新登录`。

## 运行

```bash
python3 -m msr run --all          # 全部账号串行(账号间随机等 15-40 分钟)
python3 -m msr run sg             # 只跑一个账号
python3 -m msr run sg --dry-run   # 只打印执行计划, 不执行
python3 -m msr run sg --only=app  # 只跑某阶段: app/read/edge_api/search/quick/earn
python3 -m msr status             # 全部账号状态
python3 -m msr report             # 当日报告
```

## 全部可配置项

### 1. 数据目录

环境变量 `MSR_HOME`(默认 `~/.msr`), 或 `python3 -m msr --home /path ...`。

### 2. 浏览器 `~/.msr/browser.json`

```json
{
  "binary": "chromium",
  "version": "141",
  "search_budget_minutes": 60,
  "cards_budget_minutes": 25,
  "headless": true
}
```

| 键 | 说明 | 环境变量覆盖 |
|---|---|---|
| binary | chromium/google-chrome 或完整路径 | `MSR_CHROMIUM` |
| version | Edge 伪装版本号(建议=实际 chromium 主版本) | `MSR_EDGE_VERSION` |
| search_budget_minutes | 搜索阶段最长用时 | - |
| cards_budget_minutes | 每日活动/日常任务卡阶段最长用时 | - |
| headless | 预留 false(调试需显示器) | - |

install.sh 自动填 binary 和 version, 之后可手改。

### 3. 账号 `~/.msr/accounts.json` (login 自动生成, 可手改)

```json
[
  {"name": "sg", "enabled": true, "offset_minutes": 0},
  {"name": "cn", "enabled": true, "offset_minutes": 45}
]
```

### 4. 通知 `~/.msr/notify.json` (可选, 默认 stdout+日志)

```json
{
  "channels": ["stdout"],
  "on": ["error", "summary"],
  "telegram": {"bot_token": "123:ABC", "chat_id": "xxx"},
  "webhook": {"url": "https://...", "method": "POST"}
}
```

`channels`: stdout | telegram | webhook, 可组合。`on`: error(运行失败)/summary(每日汇总)。

### 5. 定时

systemd(自动配): `systemctl list-timers msr*`; 改时间编辑 `/etc/systemd/system/msr.timer` 的 `OnCalendar` 后 `systemctl daemon-reload`。

或 crontab:
```
30 1 * * * cd /path/to/msr-armbot && python3 -m msr run --all >> ~/.msr/cron.log 2>&1
```

### 6. 账号 profile(自动状态, 不需手改)

```
~/.msr/profiles/<名>/
├── token.json     # refresh_token/access_token (600)
├── region.json    # 探测结果(country/language/promotions/balance)
├── cookie.json    # B1 导入的浏览器 cookie (login --cookies 写入)
├── state.json     # 上次运行结果
├── chrome/        # headless chromium user-data-dir
└── logs/          # 每日日志
```

## 执行计划如何生成

1. `GET /dapi/me` -> `profile.attributes.country`(sg/cn/...)
2. promotions 里实际存在的任务 -> API 侧阶段(app/edge_api/read)
3. 浏览器侧阶段(search/quick/earn)默认尝试 —— 按 rewards dashboard 当天实际卡片执行, cn 账号没有的卡片自然跳过
4. 顺序: API 侧在前(便宜快速), 浏览器侧在后

视觉搜索不移植(中国区本没有; 其他区域如需要, VPS 现网链仍在跑)。

## 真机首跑建议

```
python3 -m msr run <名> --dry-run      # 看计划对不对
python3 -m msr run <名> --only=app     # 先验 API 侧(纯 HTTP, 最便宜)
python3 -m msr run <名>                # 全量
```

## 项目结构

```
msr/__main__.py      CLI 入口
msr/config.py        数据目录 + 多账号 profile
msr/api.py           RewardsPlatform API + token 生命周期
msr/auth.py          登录引导 (device-code + cookie 导入)
msr/probe.py         区域/任务集探测
msr/planner.py       执行计划生成
msr/runner.py        编排 (串行/随机等待/dry-run)
msr/executor.py      阶段执行器 (API 侧 + 浏览器侧)
msr/browser.py       chromium 生命周期 + CDP + Edge伪装 + cookie注入
msr/tasks_browser.py 搜索/每日活动卡/日常任务卡
msr/report.py        日报
msr/notify.py        通知(预留接口)
tests/               单测
install.sh           Armbian 一键安装
```

## 状态

- [x] Task 1-3: 骨架/config/API/auth/probe/planner(sg 账号实跑验证)
- [x] Task 4-5: 浏览器层(本机 headless 实测: 启动/CDP/伪装跨导航持久/导航)
- [x] Task 6: notify 预留 + 全配置项
- [x] Task 7: install.sh + systemd
- [ ] ARM 真机首跑: 搜索计分/卡片点击真机验证
- [ ] cn 账号实测: cn.bing.com 计分协议(需中国网络环境)

## 安全说明

- token.json/cookie.json 权限 600, 存 `$MSR_HOME/profiles/<名>/`, 不要提交到任何仓库
- 本仓库不含任何账号凭据
