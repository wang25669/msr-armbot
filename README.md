# msr-armbot

Microsoft Rewards 自动化, 为 ARM Linux (Armbian) 设计。Python stdlib-only, 无第三方依赖。

## 特性

- **多账号**: 每账号独立 profile(token/cookie/状态/日志互不干扰), 串行执行, 账号间随机等待 15-40 分钟
- **区域自适应**: 区域与任务集从 `/dapi/me` 运行时探测(账号属性, 与出口 IP 无关), 不硬编码任何区域差异; 浏览器任务按当天实际卡片执行, 卡片不存在自然跳过
- **API 侧任务零浏览器**: App签到(type103)/移动阅读(type101)/Edge打卡(动态 offerid) 纯 HTTP
- **资源占用**: API 阶段仅一个 python 进程(~30 MB); 浏览器阶段单实例 headless chromium(~400 MB), 串行即释放
- **多层容错**: API 调用指数退避重试(5/10/20s, 5xx/429/超时); per-task 隔离(单任务失败不连累其他); refresh_token 重试+旧 access_token 降级

## 安装 (Armbian)

```bash
# 依赖: python3 (系统自带) + chromium (仅浏览器任务需要)
sudo apt install -y chromium
git clone https://github.com/wang25669/msr-armbot.git
cd msr-armbot
```

## 登录账号

```bash
# 第1步: device-code 授权(手机/任意浏览器完成, 纯 HTTP)
python3 -m msr login <账号名>
#   -> 终端显示 verification_uri + user_code
#   -> 手机打开网址输入代码, 登录微软账号(含2FA)

# 第2步(可选, 有浏览器侧任务的账号): 导入浏览器 cookie
#   在你电脑的浏览器上登录 bing.com, 用 Cookie-Editor 插件导出 JSON, 然后:
python3 -m msr login <账号名> --cookies cookies.json
```

兜底方案(cookie 导入失败/要求重新验证时): Armbian 上起 headless chromium,
本地 `ssh -L 9222:127.0.0.1:9222 user@armbian` 后用 chrome://inspect 远程操作登录页。

## 运行

```bash
python3 -m msr run --all          # 全部账号串行
python3 -m msr run sg --dry-run   # 只看执行计划
python3 -m msr run sg --only=app  # 只跑某一阶段
python3 -m msr status             # 全部账号状态
python3 -m msr report             # 当日报告
```

## 定时 (systemd timer 示例)

```ini
# /etc/systemd/system/msr.timer
[Unit]
Description=msr daily run

[Timer]
OnCalendar=*-*-* 01:30:00
RandomizedDelaySec=900
Persistent=true

[Install]
WantedBy=timers.target
```

## 项目结构

```
msr/__main__.py   CLI 入口
msr/config.py     MSR_HOME 数据目录 + 多账号 profile
msr/api.py        RewardsPlatform API + token 生命周期(device-code/refresh/降级)
msr/auth.py       登录引导
msr/probe.py      区域/任务集探测
msr/planner.py    探测结果 -> 执行计划
msr/runner.py     编排(串行/随机等待/dry-run)
msr/executor.py   阶段执行器(API 侧已落地, 浏览器侧迁移中)
msr/report.py     日报
tests/            单测
```

## 状态

- [x] Task 1-3: 骨架/config/API/auth/probe/planner (已验证: sg 账号实跑 probe+plan)
- [ ] Task 4-5: 浏览器层迁移(headless chromium + CDP, 搜索/每日活动卡)
- [ ] Task 6: notify.py 预留接口(现 stdout+日志)
- [ ] Task 7: install.sh + systemd 一键部署
- [ ] cn 账号首跑实测(cn.bing.com 搜索计分, 需中国网络环境)

## 安全说明

- token.json 权限 600, 存 `$MSR_HOME/profiles/<name>/`, 不要提交到任何仓库
- 本仓库不含任何账号凭据
