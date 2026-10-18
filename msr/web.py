"""现代化轻量级 Web 管理控制台与 RESTful API。

纯 Python 标准库实现 (基于 http.server)，零第三方 pip 依赖。
功能矩阵:
1. 账号全景仪表盘:
   - 自动检测并展示登录凭据有效性 (🟢 登录有效 / 🔴 凭据失效)
   - 实时积分余额、区域、今日得分增量、出口代理
   - 最近 7 天每日得分概况柱状图与 7 天累计收益
   - 账号级反检测硬件指纹概况展示与专属配置入口
   - 有效凭据一键导出 Cookie (适配 Cookie-Editor 标准 JSON)
2. 独立任务执行流程说明:
   - 单独展示 9 大标准任务执行阶段与生命周期 (API 极速 -> 后台 Edge 心跳 -> 浏览器流转 -> 智能防冷板搜索 -> 自动核查容错)
3. 网页授权与重登录中心:
   - 方式 A: 微软官方设备码免密授权 (Device Code 流，手机/电脑扫码授权，后台自动轮询落盘)
   - 方式 B: Cookie 快速导入 (JSON 数组或 Raw 请求头，实时校验 _U 凭据)
4. 账号级专属硬件指纹管理:
   - 每个账号独立绑定显卡 (WebGL)、CPU 核心、内存、分辨率、时区与语言
   - 跨会话持久锁定，支持一键重新随机派生与自定义修改
5. 全量集中风控与节奏参数配置:
   - 必应搜索节奏 (防冷却冷板停留、连续不涨分熔断、超时预算、随机点击率、语言)
   - 卡片与打卡任务停留 (活动归因等待、答题卡间隔、总预算)
   - 移动端与 Edge 浏览 API (资讯阅读篇数与间隔、Edge 30分钟打卡心跳频次与间隔)
   - 多账号编排调度 (极速测试模式、账号间等待、阶段间过渡、随机启动抖动、失败重试等待)
   - 浏览器指纹伪装 (无头模式、Edge 版本、平台伪装、核心数、内存、防 WebRTC 代理泄漏、窗口尺寸)
   - 多渠道通知推送 (Server酱微信、Telegram、自定义 Webhook、触发时机配置与一键测试)
6. 任务在线调度与日志:
   - 单账号立即运行 / 一键批量运行全部
   - 实时日志滚屏捕获与自动轮询刷新
"""
import copy
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

LIVE_LOGS = []
LIVE_LOGS_LOCK = threading.Lock()
MAX_LIVE_LOGS = 1000


def append_live_log(msg):
    with LIVE_LOGS_LOCK:
        ts = time.strftime('%H:%M:%S')
        LIVE_LOGS.append(f'[{ts}] {msg}')
        if len(LIVE_LOGS) > MAX_LIVE_LOGS:
            LIVE_LOGS.pop(0)


HTML_CONTENT = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MSR-ArmBot 控制中心</title>
  <style>
    :root {
      --bg: #0b1120; --card: #1e293b; --card-hover: #24344d;
      --text: #f8fafc; --text-sub: #94a3b8; --border: #334155;
      --primary: #38bdf8; --primary-hover: #0ea5e9;
      --success: #22c55e; --warning: #f59e0b; --danger: #ef4444;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif; }
    body { background: var(--bg); color: var(--text); padding: 24px; min-height: 100vh; }
    .container { max-width: 1240px; margin: 0 auto; }
    header { display: flex; justify-content: space-between; align-items: center; padding-bottom: 20px; margin-bottom: 24px; border-bottom: 1px solid var(--border); flex-wrap: wrap; gap: 14px; }
    .logo-area { display: flex; align-items: center; gap: 12px; }
    .logo-title { font-size: 24px; font-weight: 800; color: #fff; letter-spacing: -0.5px; }
    .logo-tag { background: linear-gradient(135deg, #0284c7, #38bdf8); color: #fff; font-size: 12px; padding: 3px 8px; border-radius: 4px; font-weight: 700; }
    .nav-tabs { display: flex; gap: 8px; margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 8px; flex-wrap: wrap; }
    .tab-btn { background: none; border: 1px solid transparent; color: var(--text-sub); font-size: 15px; font-weight: 600; padding: 8px 18px; cursor: pointer; border-radius: 8px; transition: all 0.2s; }
    .tab-btn:hover { color: #fff; background: rgba(255,255,255,0.05); }
    .tab-btn.active { background: var(--card); color: var(--primary); border-color: var(--border); }
    .tab-content { display: none; }
    .tab-content.active { display: block; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 22px; margin-bottom: 24px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.2); }
    .card-title { font-size: 17px; font-weight: 700; margin-bottom: 18px; display: flex; justify-content: space-between; align-items: center; color: #fff; flex-wrap: wrap; gap: 10px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 20px; }
    .acc-card { background: rgba(15, 23, 42, 0.7); border: 1px solid var(--border); border-radius: 10px; padding: 18px; transition: transform 0.15s, border-color 0.15s; display: flex; flex-direction: column; justify-content: space-between; }
    .acc-card:hover { border-color: #475569; transform: translateY(-2px); }
    .acc-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
    .acc-name { font-size: 19px; font-weight: 700; color: #fff; }
    .badge { font-size: 12px; padding: 3px 8px; border-radius: 4px; font-weight: 600; display: inline-flex; align-items: center; gap: 4px; }
    .badge-success { background: rgba(34, 197, 94, 0.15); color: var(--success); border: 1px solid rgba(34, 197, 94, 0.3); }
    .badge-disabled { background: rgba(239, 68, 68, 0.15); color: var(--danger); border: 1px solid rgba(239, 68, 68, 0.3); }
    .badge-warning { background: rgba(245, 158, 11, 0.15); color: var(--warning); border: 1px solid rgba(245, 158, 11, 0.3); }
    .badge-info { background: rgba(56, 189, 248, 0.15); color: var(--primary); border: 1px solid rgba(56, 189, 248, 0.3); }
    .acc-meta { font-size: 13px; color: var(--text-sub); line-height: 1.85; }
    .acc-meta strong { color: #f1f5f9; }
    .acc-meta code { background: rgba(0,0,0,0.3); padding: 2px 5px; border-radius: 4px; font-size: 12px; color: #38bdf8; word-break: break-all; }
    .btn { background: var(--primary); color: #0f172a; border: none; padding: 8px 16px; border-radius: 6px; font-weight: 600; cursor: pointer; transition: 0.15s; font-size: 14px; display: inline-flex; align-items: center; gap: 6px; }
    .btn:hover { background: var(--primary-hover); }
    .btn-secondary { background: #334155; color: #f8fafc; }
    .btn-secondary:hover { background: #475569; }
    .btn-danger { background: rgba(239, 68, 68, 0.2); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.4); }
    .btn-danger:hover { background: var(--danger); color: #fff; }
    .btn-sm { padding: 5px 12px; font-size: 12px; border-radius: 5px; }
    .form-group { margin-bottom: 14px; }
    label { display: block; font-size: 13px; font-weight: 600; color: var(--text-sub); margin-bottom: 6px; }
    input, select, textarea { width: 100%; background: #0f172a; border: 1px solid var(--border); color: #fff; padding: 9px 12px; border-radius: 6px; font-size: 13px; }
    input:focus, textarea:focus, select:focus { border-color: var(--primary); outline: none; }
    .flex-row { display: flex; gap: 10px; align-items: center; }
    pre { background: #070d19; border: 1px solid var(--border); border-radius: 8px; padding: 14px; font-family: "JetBrains Mono", Consolas, monospace; font-size: 12px; color: #94a3b8; max-height: 480px; overflow-y: auto; white-space: pre-wrap; line-height: 1.6; }
    .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.78); display: none; justify-content: center; align-items: center; z-index: 999; backdrop-filter: blur(4px); }
    .modal { background: var(--card); border: 1px solid var(--border); border-radius: 12px; width: 95%; max-width: 620px; padding: 24px; max-height: 90vh; overflow-y: auto; }
    .modal-lg { max-width: 820px; }
    .toast { position: fixed; bottom: 24px; right: 24px; background: #1e293b; border: 1px solid var(--primary); color: #fff; padding: 12px 22px; border-radius: 8px; font-size: 14px; display: none; z-index: 1000; box-shadow: 0 10px 25px rgba(0,0,0,0.6); }
    .pipeline-step { display: flex; align-items: flex-start; gap: 12px; padding: 12px; background: rgba(15, 23, 42, 0.6); border-radius: 8px; margin-bottom: 10px; border-left: 3px solid var(--primary); }
    .pipeline-badge { background: #0284c7; color: #fff; font-size: 11px; font-weight: 700; padding: 2px 6px; border-radius: 4px; white-space: nowrap; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="logo-area">
        <span class="logo-title">MSR-ArmBot</span>
        <span class="logo-tag">WEB 控制台</span>
      </div>
      <div class="flex-row">
        <span id="scheduler-badge" style="font-size:13px; color:var(--text-sub); margin-right:8px; background:rgba(255,255,255,0.05); padding:6px 12px; border-radius:6px; border:1px solid var(--border);">⏰ 每日定时: 加载中...</span>
        <button class="btn btn-secondary" onclick="openPipelineModal()">📋 任务执行流程</button>
        <button class="btn btn-secondary" onclick="fetchStatus()">刷新状态</button>
        <button class="btn" onclick="triggerRunAll()">一键运行全部</button>
      </div>
    </header>

    <div class="nav-tabs">
      <button class="tab-btn active" data-tab="dashboard" onclick="setTab('dashboard')">仪表盘</button>
      <button class="tab-btn" data-tab="accounts" onclick="setTab('accounts')">账号管理</button>
      <button class="tab-btn" data-tab="settings" onclick="setTab('settings')">风控参数配置</button>
      <button class="tab-btn" data-tab="logs" onclick="setTab('logs')">实时日志</button>
    </div>

    <!-- 仪表盘 -->
    <div id="tab-dashboard" class="tab-content active">
      <div class="card">
        <div class="card-title">
          <span>账号运行概览</span>
          <span id="stat-summary" style="font-size:13px; color:var(--text-sub)"></span>
        </div>
        <div id="acc-grid" class="grid">加载中...</div>
      </div>
    </div>

    <!-- 账号管理 -->
    <div id="tab-accounts" class="tab-content">
      <div class="card">
        <div class="card-title">添加 / 编辑微软账号</div>
        <div class="grid" style="grid-template-columns: 1fr 1.6fr 1fr auto; align-items: flex-end;">
          <div class="form-group">
            <label>账号名称 (唯一标识)</label>
            <input type="text" id="acc-name-input" placeholder="例如: sg_user, cn_main">
          </div>
          <div class="form-group">
            <label>独立出口代理 (支持 HTTP / SOCKS5，留空直连)</label>
            <div style="display:flex; gap:6px;">
              <input type="text" id="acc-proxy-input" placeholder="http://user:pass@127.0.0.1:7890">
              <button class="btn btn-secondary btn-sm" style="white-space:nowrap;" onclick="testProxyInForm()">⚡ 测试</button>
            </div>
          </div>
          <div class="form-group">
            <label>调度偏移 (分钟)</label>
            <input type="number" id="acc-offset-input" value="0">
          </div>
          <div class="form-group">
            <button class="btn" onclick="saveNewAccount()">保存账号</button>
          </div>
        </div>
        <div id="proxy-test-box" style="display:none; margin-top:10px; padding:8px 12px; border-radius:6px; font-size:12px;"></div>
      </div>

      <div class="card">
        <div class="card-title">账号列表与授权管理</div>
        <div id="acc-table-list">加载中...</div>
      </div>
    </div>

    <!-- 全量参数配置 -->
    <div id="tab-settings" class="tab-content">
      <div class="card">
        <div class="card-title">
          <div style="display:flex; align-items:center; gap:10px;">
            <span>集中风控与任务执行全量参数配置</span>
            <span style="font-size:12px; color:var(--text-sub); font-weight:normal;">(持久化存储于 ~/.msr/settings.json，所有调度与浏览器执行即时生效)</span>
          </div>
          <div class="flex-row">
            <button class="btn btn-secondary btn-sm" onclick="resetSettingsDefault()">恢复默认配置</button>
            <button class="btn btn-sm" onclick="submitSettings()">保存全部配置</button>
          </div>
        </div>

        <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 20px;">
          <!-- 1. 必应搜索任务 -->
          <div class="acc-card">
            <h4 style="color:var(--primary); margin-bottom:12px; display:flex; align-items:center; gap:6px;">
              🔍 必应搜索任务参数 (防风控与冷板)
            </h4>
            <div class="form-group">
              <label>单次搜索随机停留最小秒数 (防风控，默认 180s = 3分钟)</label>
              <input type="number" id="s-dwell-min">
            </div>
            <div class="form-group">
              <label>单次搜索随机停留最大秒数 (默认 300s = 5分钟)</label>
              <input type="number" id="s-dwell-max">
            </div>
            <div class="form-group">
              <label>连续不涨分重试熔断阈值 (stall_max_retries)</label>
              <input type="number" id="s-stall-max">
            </div>
            <div class="form-group">
              <label>单日搜索最大尝试次数硬上限 (hard_cap_attempts)</label>
              <input type="number" id="s-hard-cap">
            </div>
            <div class="form-group">
              <label>搜索阶段总超时预算 (分钟)</label>
              <input type="number" id="s-budget-min">
            </div>
            <div class="form-group">
              <label>搜索后随机点击结果链接概率 (0.0 ~ 1.0)</label>
              <input type="number" step="0.05" min="0" max="1" id="s-click-prob">
            </div>
            <div class="form-group">
              <label>搜索词语言偏好</label>
              <select id="s-query-lang">
                <option value="auto">auto (自动根据账号区域自适应)</option>
                <option value="zh">zh (中文词库)</option>
                <option value="en">en (英文词库)</option>
              </select>
            </div>
          </div>

          <!-- 2. 卡片与打卡任务 -->
          <div class="acc-card">
            <h4 style="color:var(--primary); margin-bottom:12px; display:flex; align-items:center; gap:6px;">
              🃏 卡片与打卡任务 (Daily Set / More Activities / Quests)
            </h4>
            <div class="form-group">
              <label>卡片点击后停留最小秒数 (等待活动归因，默认 60s)</label>
              <input type="number" id="c-attr-min">
            </div>
            <div class="form-group">
              <label>卡片点击后停留最大秒数 (默认 120s)</label>
              <input type="number" id="c-attr-max">
            </div>
            <div class="form-group">
              <label>卡片阶段总超时预算 (分钟，默认 35)</label>
              <input type="number" id="c-budget-min">
            </div>
            <div class="form-group">
              <label>自动答题卡每题间隔最小秒数 (默认 3s)</label>
              <input type="number" id="c-quiz-min">
            </div>
            <div class="form-group">
              <label>自动答题卡每题间隔最大秒数 (默认 6s)</label>
              <input type="number" id="c-quiz-max">
            </div>
          </div>

          <!-- 3. 移动端与 Edge 浏览 API -->
          <div class="acc-card">
            <h4 style="color:var(--primary); margin-bottom:12px; display:flex; align-items:center; gap:6px;">
              📱 移动端与 Edge 浏览打卡 (Mobile & Edge API)
            </h4>
            <div class="form-group">
              <label>移动端资讯阅读文章目标篇数 (默认 10 篇)</label>
              <input type="number" id="m-read-count">
            </div>
            <div class="form-group">
              <label>每篇资讯阅读最小间隔 (秒，默认 60s)</label>
              <input type="number" id="m-read-min">
            </div>
            <div class="form-group">
              <label>每篇资讯阅读最大间隔 (秒，默认 120s)</label>
              <input type="number" id="m-read-max">
            </div>
            <div class="form-group">
              <label>Edge 30分钟打卡上报次数 (每次约5分钟，默认 6 次)</label>
              <input type="number" id="m-edge-reports">
            </div>
            <div class="form-group">
              <label>两次 Edge 打卡心跳间隔 (秒，默认 305s)</label>
              <input type="number" id="m-edge-interval">
            </div>
          </div>

          <!-- 4. 调度与任务编排 -->
          <div class="acc-card">
            <h4 style="color:var(--primary); margin-bottom:12px; display:flex; align-items:center; gap:6px;">
              ⏱️ 多账号编排与调度参数 (Schedule)
            </h4>
            <div class="form-group">
              <label>每日自动定时执行总开关 (到点自动唤醒拉起浏览器执行全部账号)</label>
              <select id="sch-daily-enabled">
                <option value="true">开启 (每日自动定时循环运行)</option>
                <option value="false">关闭 (仅手动触发)</option>
              </select>
            </div>
            <div class="form-group">
              <label>每日定时启动时间 (24小时制 HH:MM，默认 01:30)</label>
              <input type="text" id="sch-daily-time" placeholder="01:30">
            </div>
            <div class="form-group">
              <label>极速测试模式 (fast_mode, 开启后等待压缩至 3-6 秒)</label>
              <select id="cfg-fast-mode">
                <option value="false">关闭 (真实真人节奏，约2小时/号)</option>
                <option value="true">开启 (极速调试，仅测试流程连通性)</option>
              </select>
            </div>
            <div class="form-group">
              <label>多账号间随机等待最小分钟数 (防批量并发风控，默认 15 分钟)</label>
              <input type="number" id="sch-acc-min">
            </div>
            <div class="form-group">
              <label>多账号间随机等待最大分钟数 (默认 40 分钟)</label>
              <input type="number" id="sch-acc-max">
            </div>
            <div class="form-group">
              <label>阶段间过渡等待最小分钟数 (默认 3 分钟)</label>
              <input type="number" id="sch-stage-min">
            </div>
            <div class="form-group">
              <label>阶段间过渡等待最大分钟数 (默认 5 分钟)</label>
              <input type="number" id="sch-stage-max">
            </div>
            <div class="form-group">
              <label>定时启动随机延迟上限 (分钟，默认 25 分钟防固定时间特征)</label>
              <input type="number" id="sch-start-delay">
            </div>
            <div class="form-group">
              <label>失败任务自动修复重试前等待 (分钟，默认 4 分钟)</label>
              <input type="number" id="sch-retry-wait">
            </div>
          </div>

          <!-- 5. 浏览器与反检测指纹伪装 -->
          <div class="acc-card">
            <h4 style="color:var(--primary); margin-bottom:12px; display:flex; align-items:center; gap:6px;">
              🛡️ 全局浏览器底层参数 (Global Browser)
            </h4>
            <div class="form-group">
              <label>无头模式 (Headless)</label>
              <select id="b-headless">
                <option value="true">开启无头模式 (后台静默运行，推荐)</option>
                <option value="false">关闭无头模式 (前台显示浏览器窗口)</option>
              </select>
            </div>
            <div class="form-group">
              <label>WebRTC UDP 防代理泄漏策略 (disable_non_proxied_udp)</label>
              <select id="b-webrtc">
                <option value="true">开启 (屏蔽直连 UDP，防代理泄漏真实出口 IP)</option>
                <option value="false">关闭</option>
              </select>
            </div>
            <div class="form-group">
              <label>自定义 Chromium/Chrome 执行路径 (留空为自动探测)</label>
              <input type="text" id="b-binary" placeholder="例如: /usr/bin/chromium 或留空">
            </div>
            <div style="font-size:12px; color:var(--text-sub); line-height:1.6; margin-top:8px;">
              💡 提示: 每个账号已默认启用<strong>专属独立的硬件显卡、核心数、分辨率、时区与 Profile 隔离</strong>，可在“仪表盘”或“账号管理”中点击【专属指纹】单独调节。
            </div>
          </div>

          <!-- 6. 消息通知渠道 -->
          <div class="acc-card">
            <h4 style="color:var(--primary); margin-bottom:12px; display:flex; align-items:center; gap:6px;">
              📢 多渠道消息通知配置 (Server酱 / Telegram / Webhook)
            </h4>
            <div class="form-group">
              <label>启用推送通道 (勾选开启)</label>
              <div style="display:flex; gap:14px; flex-wrap:wrap; padding:6px 0;">
                <label style="display:inline-flex; align-items:center; gap:6px; cursor:pointer;"><input type="checkbox" id="n-ch-stdout" style="width:auto;"> 控制台日志 (stdout)</label>
                <label style="display:inline-flex; align-items:center; gap:6px; cursor:pointer;"><input type="checkbox" id="n-ch-serverchan" style="width:auto;"> Server酱 (微信)</label>
                <label style="display:inline-flex; align-items:center; gap:6px; cursor:pointer;"><input type="checkbox" id="n-ch-telegram" style="width:auto;"> Telegram</label>
                <label style="display:inline-flex; align-items:center; gap:6px; cursor:pointer;"><input type="checkbox" id="n-ch-webhook" style="width:auto;"> 自定义 Webhook</label>
              </div>
            </div>
            <div class="form-group">
              <label>通知触发时机</label>
              <div style="display:flex; gap:14px; padding:6px 0;">
                <label style="display:inline-flex; align-items:center; gap:6px; cursor:pointer;"><input type="checkbox" id="n-on-error" style="width:auto;"> 任务异常时 (error)</label>
                <label style="display:inline-flex; align-items:center; gap:6px; cursor:pointer;"><input type="checkbox" id="n-on-summary" style="width:auto;"> 每日运行汇总 (summary)</label>
              </div>
            </div>

            <div style="border-top:1px solid var(--border); padding-top:12px; margin-top:8px;">
              <h5 style="color:#e2e8f0; font-size:13px; margin-bottom:8px;">Server酱 (微信) 配置</h5>
              <div class="form-group">
                <label>Server酱 SendKey (Turbo 版 SCT 或旧版 SCU)</label>
                <input type="text" id="sc-key" placeholder="例如: SCTxxxxxxxx">
              </div>
              <div class="form-group">
                <label>自定义分流通道 channel (选填，如 9=钉钉群, 18=企业微信)</label>
                <input type="text" id="sc-channel" placeholder="留空默认微信模板消息">
              </div>
            </div>

            <div style="border-top:1px solid var(--border); padding-top:12px; margin-top:8px;">
              <h5 style="color:#e2e8f0; font-size:13px; margin-bottom:8px;">Telegram 配置</h5>
              <div style="display:grid; grid-template-columns: 1fr 1fr; gap:10px;">
                <div class="form-group">
                  <label>Bot Token</label>
                  <input type="text" id="tg-token" placeholder="bot123456:ABC...">
                </div>
                <div class="form-group">
                  <label>Chat ID</label>
                  <input type="text" id="tg-chatid" placeholder="例如: -10012345678">
                </div>
              </div>
            </div>

            <div style="border-top:1px solid var(--border); padding-top:12px; margin-top:8px;">
              <h5 style="color:#e2e8f0; font-size:13px; margin-bottom:8px;">自定义 Webhook 配置</h5>
              <div style="display:grid; grid-template-columns: 2fr 1fr; gap:10px;">
                <div class="form-group">
                  <label>Webhook URL</label>
                  <input type="text" id="wh-url" placeholder="https://api.example.com/notify">
                </div>
                <div class="form-group">
                  <label>请求方法</label>
                  <select id="wh-method">
                    <option value="POST">POST (JSON)</option>
                    <option value="GET">GET</option>
                  </select>
                </div>
              </div>
            </div>

            <div style="margin-top:16px;">
              <button class="btn btn-secondary btn-sm" onclick="sendTestPush()">测试发送通知</button>
            </div>
          </div>
        </div>

        <div style="margin-top:20px; display:flex; justify-content:flex-end; gap:12px;">
          <button class="btn btn-secondary" onclick="resetSettingsDefault()">恢复默认配置</button>
          <button class="btn" onclick="submitSettings()">保存全部配置</button>
        </div>
      </div>
    </div>

    <!-- 实时日志 -->
    <div id="tab-logs" class="tab-content">
      <div class="card">
        <div class="card-title">
          <span>实时日志输出 (每 3 秒自动轮询)</span>
          <div class="flex-row">
            <button class="btn btn-secondary btn-sm" onclick="loadLogsNow()">立即刷新</button>
          </div>
        </div>
        <pre id="log-box">正在加载日志...</pre>
      </div>
    </div>
  </div>

  <!-- 独立弹窗 1: 标准任务执行流程说明 -->
  <div id="pipeline-modal" class="modal-overlay">
    <div class="modal modal-lg">
      <div class="card-title">
        <span>📋 标准任务执行流程与编排架构</span>
        <button class="btn btn-secondary btn-sm" onclick="closePipelineModal()">关闭</button>
      </div>
      <div style="font-size:13px; color:var(--text-sub); margin-bottom:16px; line-height:1.6;">
        所有账号均统一遵循标准化流水线执行，各账号按区域自适应过滤，多账号之间在独立代理与随机等待下串行执行，杜绝关联风控：
      </div>

      <div class="pipeline-step">
        <span class="pipeline-badge">阶段 1 · API 极速</span>
        <div>
          <strong style="color:#fff;">移动端必应 App 签到 (appcheckin)</strong>
          <div style="font-size:12px; color:var(--text-sub); margin-top:3px;">基于官方移动端 API 快速签到，1 秒内完成。</div>
        </div>
      </div>

      <div class="pipeline-step">
        <span class="pipeline-badge">阶段 2 · API 篇章</span>
        <div>
          <strong style="color:#fff;">移动端资讯文章阅读 (appread, 目标 10 篇)</strong>
          <div style="font-size:12px; color:var(--text-sub); margin-top:3px;">每篇随机间隔 60-120 秒，模拟真实读者阅读节奏，防风控拦截。</div>
        </div>
      </div>

      <div class="pipeline-step">
        <span class="pipeline-badge" style="background:#8b5cf6;">阶段 3 · 后台并行</span>
        <div>
          <strong style="color:#fff;">Edge 30 分钟浏览打卡 (edge30m)</strong>
          <div style="font-size:12px; color:var(--text-sub); margin-top:3px;">每 5 分钟自动上报心跳（共 6 次），后台独立线程执行，与浏览器任务完全并行，节省 30 分钟耗时。</div>
        </div>
      </div>

      <div class="pipeline-step">
        <span class="pipeline-badge" style="background:#10b981;">阶段 4 · 浏览器卡片</span>
        <div>
          <strong style="color:#fff;">每日活动 3 卡片 (dailyset)</strong>
          <div style="font-size:12px; color:var(--text-sub); margin-top:3px;">启动单浏览器真人会话，按活动归因停留 60-120 秒，答题卡自动完成选择，步骤完即关标签。</div>
        </div>
      </div>

      <div class="pipeline-step">
        <span class="pipeline-badge" style="background:#10b981;">阶段 5 · 探险打卡</span>
        <div>
          <strong style="color:#fff;">专属打卡任务 (quests)</strong>
          <div style="font-size:12px; color:var(--text-sub); margin-top:3px;">智能识别当前生效的 Punchcard (探险/节日打卡)，自适应点击与状态追踪，及时释放标签。</div>
        </div>
      </div>

      <div class="pipeline-step">
        <span class="pipeline-badge" style="background:#10b981;">阶段 6 · 日常活动</span>
        <div>
          <strong style="color:#fff;">日常任务与额外积分 (moreactivities)</strong>
          <div style="font-size:12px; color:var(--text-sub); margin-top:3px;">扫描仪表板全部未领取的更多活动卡片，依次点击并完成积分归因。</div>
        </div>
      </div>

      <div class="pipeline-step">
        <span class="pipeline-badge" style="background:#f59e0b;">阶段 7 · 核心搜索</span>
        <div>
          <strong style="color:#fff;">必应 PC 搜索打卡 (search)</strong>
          <div style="font-size:12px; color:var(--text-sub); margin-top:3px;">根据账号等级动态探测上限，每次搜索随机停顿 3-5 分钟，模拟自然浏览与点击结果，杜绝冷板风控。</div>
        </div>
      </div>

      <div class="pipeline-step">
        <span class="pipeline-badge" style="background:#ec4899;">阶段 8 · 视觉识图</span>
        <div>
          <strong style="color:#fff;">必应视觉搜索识图 (visual)</strong>
          <div style="font-size:12px; color:var(--text-sub); margin-top:3px;">自动化上传图像样本完成识图打卡与奖励入账。</div>
        </div>
      </div>

      <div class="pipeline-step">
        <span class="pipeline-badge" style="background:#06b6d4;">阶段 9 · 容错核查</span>
        <div>
          <strong style="color:#fff;">自动核查与重跑修复 (repair)</strong>
          <div style="font-size:12px; color:var(--text-sub); margin-top:3px;">检测未满分卡片，等待 4 分钟后自动回补重试，完成时通过 Server酱/Telegram 推送报告。</div>
        </div>
      </div>
    </div>
  </div>

  <!-- 独立弹窗 2: Cookie 导出弹窗 -->
  <div id="export-cookie-modal" class="modal-overlay">
    <div class="modal">
      <div class="card-title">
        <span id="export-modal-title">Cookie 导出</span>
        <button class="btn btn-secondary btn-sm" onclick="closeExportModal()">关闭</button>
      </div>
      <div style="font-size:13px; color:var(--text-sub); margin-bottom:10px;" id="export-summary">
        正在读取 Cookie...
      </div>
      <textarea id="export-text" rows="12" readonly style="font-family:monospace; font-size:12px;"></textarea>
      <div style="margin-top:14px; display:flex; justify-content:space-between; align-items:center;">
        <span style="font-size:12px; color:var(--text-sub);">格式: Cookie-Editor 标准 JSON 数组</span>
        <button class="btn btn-sm" onclick="copyExportedCookies()">一键复制 Cookie</button>
      </div>
    </div>
  </div>

  <!-- 独立弹窗 3: 账号授权 / 重新登录弹窗 -->
  <div id="login-modal" class="modal-overlay">
    <div class="modal">
      <div class="card-title">
        <span id="modal-title">账号授权 / 重新登录</span>
        <button class="btn btn-secondary btn-sm" onclick="closeLoginModal()">关闭</button>
      </div>

      <div id="login-auth-hint" style="font-size:13px; padding:8px 12px; border-radius:6px; margin-bottom:14px; background:rgba(56,189,248,0.1); border:1px solid rgba(56,189,248,0.2); color:#e2e8f0;">
        检测凭据状态中...
      </div>

      <div class="nav-tabs" style="margin-bottom:16px;">
        <button id="modal-tab-pwd" class="tab-btn active" onclick="switchModalTab('pwd')">微软账号密码登录 (推荐)</button>
        <button id="modal-tab-ck" class="tab-btn" onclick="switchModalTab('ck')">Cookie 快速导入 (免密备选)</button>
      </div>

      <!-- 账号密码自动化登录与风控接管页面 -->
      <div id="modal-panel-pwd">
        <p style="font-size:13px; color:var(--text-sub); margin-bottom:14px; line-height:1.5;">
          输入微软账号密码，后台 Chromium 将全自动登录并智能应对风控质询（支持 Authenticator 双位数匹配、邮箱验证码输入与人机拼图远程接管）。
        </p>
        <div style="display:grid; grid-template-columns: 1fr 1fr; gap:12px; margin-bottom:12px;">
          <div class="form-group">
            <label>微软账号 (邮箱 / 手机号)</label>
            <input type="text" id="login-username" placeholder="例如: your_name@outlook.com">
          </div>
          <div class="form-group">
            <label>微软密码</label>
            <input type="password" id="login-password" placeholder="请输入微软账号密码">
          </div>
        </div>

        <!-- 手机 Authenticator 匹配数字提示卡片 (条件展示) -->
        <div id="login-auth-push-box" style="display:none; background:rgba(14,165,233,0.15); border:1px solid #0284c7; border-radius:8px; padding:12px; margin-bottom:12px; text-align:center;">
          <div style="font-size:13px; color:#38bdf8; margin-bottom:6px;">📱 微软触发手机 Authenticator 二次验证，请在手机应用上选择数字：</div>
          <div style="font-size:32px; font-weight:800; color:#38bdf8; letter-spacing:4px;" id="auth-code-val">--</div>
          <div style="font-size:12px; color:var(--text-sub); margin-top:4px;">（在手机上点击对应数字后系统将自动继续）</div>
        </div>

        <!-- 安全验证码输入卡片 (条件展示) -->
        <div id="login-otp-box" style="display:none; background:rgba(234,179,8,0.15); border:1px solid #eab308; border-radius:8px; padding:12px; margin-bottom:12px;">
          <div id="login-otp-prompt" style="font-size:13px; color:#facc15; margin-bottom:8px;">✉️ 微软需要安全验证码</div>
          <div style="display:flex; gap:8px;">
            <input type="text" id="login-otp-input" placeholder="请输入 6 位安全验证码" style="flex:1;">
            <button class="btn" onclick="submitSessionOtp()">提交验证码</button>
          </div>
        </div>

        <!-- 实时远程交互窗口 (用于 Arkose 拼图验证码或复杂质询) -->
        <div id="interactive-canvas-box" style="display:none; background:#070d19; border:1px solid var(--border); border-radius:8px; padding:10px; margin-bottom:12px;">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
            <span style="font-size:12px; color:#94a3b8;">🖱️ 实时交互画面 (点击图片可直接点击浏览器网页，如旋转拼图、选择选项):</span>
            <button class="btn btn-secondary btn-sm" onclick="refreshLiveScreenshot()">刷新画面</button>
          </div>
          <div style="text-align:center; overflow:auto; max-height:380px; background:#000; border-radius:6px;">
            <img id="live-screen-img" src="" alt="远程浏览器画面加载中..." style="max-width:100%; cursor:crosshair; display:inline-block;" onclick="handleScreenClick(event)">
          </div>
        </div>

        <div id="login-pwd-status" style="font-size:13px; padding:10px; border-radius:6px; background:#070d19; color:var(--text-sub); margin-bottom:14px; min-height:38px; display:flex; align-items:center;">
          准备就绪。支持账号密码登录、手机 Authenticator 二次验证、安全验证码输入及人机拼图远程交互。
        </div>

        <div class="flex-row" style="justify-content:space-between;">
          <button class="btn btn-secondary btn-sm" onclick="toggleInteractiveCanvas()">🖥️ 远程实时画面</button>
          <div class="flex-row">
            <button id="login-cancel-btn" class="btn btn-secondary" style="display:none; margin-right:8px;" onclick="cancelLoginSession()">取消登录</button>
            <button id="login-pwd-btn" class="btn" onclick="startLoginSession()">一键全自动登录 / 启动接管</button>
          </div>
        </div>
      </div>

      <!-- Cookie 粘贴页面 -->
      <div id="modal-panel-ck" style="display:none;">
        <p style="font-size:13px; color:var(--text-sub); margin-bottom:10px;">
          粘贴 Cookie-Editor 导出的 JSON 数组或请求头 Raw Cookie (name=val; ...)，导入时系统将自动基于该会话换取 Token：
        </p>
        <textarea id="ck-text-input" rows="8" placeholder='[{"name":"_U","value":"..."}, ...]'></textarea>
        <div style="margin-top:12px; display:flex; justify-content:space-between; align-items:center;">
          <span id="ck-result-msg" style="font-size:13px; color:var(--text-sub);"></span>
          <button id="login-ck-btn" class="btn" onclick="submitCookies()">导入 Cookie 并自动激活 Token</button>
        </div>
      </div>
    </div>
  </div>

  <!-- 独立弹窗 4: 账号专属反检测硬件指纹配置 -->
  <div id="fingerprint-modal" class="modal-overlay">
    <div class="modal">
      <div class="card-title">
        <span id="fp-modal-title">账号反检测专属硬件指纹</span>
        <button class="btn btn-secondary btn-sm" onclick="closeFingerprintModal()">关闭</button>
      </div>
      <p style="font-size:13px; color:var(--text-sub); margin-bottom:14px; line-height:1.6;">
        每个账号配置独立的硬件显卡、核心数、分辨率、时区与系统平台，避免多账号特征关联，并在多次运行间持久固化。
      </p>

      <div style="display:grid; grid-template-columns: 1fr 1fr; gap:12px;">
        <div class="form-group">
          <label>操作系统平台</label>
          <select id="fp-os">
            <option value="Windows 11">Windows 11 (Win32, 15.0.0)</option>
            <option value="Windows 10">Windows 10 (Win32, 10.0.0)</option>
          </select>
        </div>
        <div class="form-group">
          <label>Edge 主版本号</label>
          <input type="text" id="fp-edge-ver" value="141">
        </div>
      </div>

      <div class="form-group">
        <label>WebGL 显卡渲染器 (防 SwiftShader 虚拟化特征)</label>
        <select id="fp-gpu">
          <option value="ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)">NVIDIA GeForce RTX 3060</option>
          <option value="ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0)">NVIDIA GeForce RTX 4060</option>
          <option value="ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0)">NVIDIA GeForce GTX 1660 SUPER</option>
          <option value="ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0)">Intel Iris Xe Graphics</option>
          <option value="ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0)">Intel UHD Graphics 630</option>
          <option value="ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0)">AMD Radeon RX 6600</option>
          <option value="ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0)">AMD Radeon Graphics</option>
        </select>
      </div>

      <div style="display:grid; grid-template-columns: 1fr 1fr 1fr; gap:12px;">
        <div class="form-group">
          <label>CPU 核心数</label>
          <input type="number" id="fp-cpu" value="8">
        </div>
        <div class="form-group">
          <label>内存容量 (GB)</label>
          <input type="number" id="fp-mem" value="16">
        </div>
        <div class="form-group">
          <label>屏幕分辨率</label>
          <select id="fp-res">
            <option value="1920,1080">1920 x 1080 (1080P)</option>
            <option value="1600,900">1600 x 900</option>
            <option value="1536,864">1536 x 864</option>
            <option value="1440,900">1440 x 900</option>
            <option value="1366,768">1366 x 768</option>
          </select>
        </div>
      </div>

      <div style="display:grid; grid-template-columns: 1fr 1fr; gap:12px;">
        <div class="form-group">
          <label>伪装时区 (可自由输入，如 Asia/Shanghai, Asia/Singapore)</label>
          <input type="text" id="fp-tz" placeholder="例如: Asia/Singapore">
        </div>
        <div class="form-group">
          <label>语言设置 (可自由输入，如 zh-CN,zh;q=0.9,en;q=0.8)</label>
          <input type="text" id="fp-locale" placeholder="例如: zh-CN,zh;q=0.9,en;q=0.8">
        </div>
      </div>

      <div style="margin: 10px 0 14px; padding: 10px 14px; background: rgba(56,189,248,0.06); border: 1px solid rgba(56,189,248,0.2); border-radius: 8px; font-size: 12px; display: flex; justify-content: space-between; align-items: center;">
        <span style="color: var(--text-sub);">💡 时区与语言均可随意手动修改；也可一键自动探测绑定代理出口并同步：</span>
        <button class="btn btn-secondary btn-sm" onclick="alignFingerprintWithProxy()" style="white-space:nowrap;">🌐 一键匹配绑定代理地区与时区</button>
      </div>

      <div class="form-group">
        <label>User-Agent 伪装标识 (根据当前特征自动拼装)</label>
        <input type="text" id="fp-ua" readonly style="font-size:11px; color:var(--primary); background:rgba(0,0,0,0.4);">
      </div>

      <div style="margin-top:16px; display:flex; justify-content:space-between; align-items:center;">
        <button class="btn btn-secondary btn-sm" onclick="randomizeFingerprint()">🎲 重新随机派生硬件特征</button>
        <button class="btn btn-sm" onclick="saveFingerprintConfig()">保存专属指纹</button>
      </div>
    </div>
  </div>

  <div id="toast" class="toast"></div>

  <script>
    let activeAccountName = '';
    let dcPollTimer = null;
    let logPollTimer = null;
    let currentFpData = null;

    function toast(msg) {
      const t = document.getElementById('toast');
      t.innerText = msg;
      t.style.display = 'block';
      setTimeout(() => { t.style.display = 'none'; }, 3500);
    }

    function setTab(tabName) {
      document.querySelectorAll('.tab-btn').forEach(b => {
        b.classList.toggle('active', b.getAttribute('data-tab') === tabName);
      });
      document.querySelectorAll('.tab-content').forEach(c => {
        c.classList.toggle('active', c.id === 'tab-' + tabName);
      });

      if (logPollTimer) clearInterval(logPollTimer);
      if (tabName === 'logs') {
        loadLogsNow();
        logPollTimer = setInterval(loadLogsNow, 3000);
      } else if (tabName === 'settings') {
        loadSettingsForm();
      }
    }

    async function fetchStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        renderDashboard(data.accounts || []);
        renderManageList(data.accounts || []);
        document.getElementById('stat-summary').innerText = `总账号数: ${data.accounts?.length || 0} | 服务器时间: ${data.time || ''}`;
        if (data.scheduler) {
          const sch = data.scheduler;
          const badge = document.getElementById('scheduler-badge');
          if (badge) {
            badge.innerHTML = sch.daily_enabled
              ? `⏰ 每日定时: <b style="color:var(--primary); font-family:monospace;">${sch.daily_time}</b> <span style="color:#10b981;">(自动唤醒)</span>`
              : `⏰ 每日定时: <span style="color:var(--text-sub);">已暂停</span>`;
          }
        }
      } catch(err) {
        toast('加载状态失败: ' + err);
      }
    }

    function renderDashboard(accs) {
      const grid = document.getElementById('acc-grid');
      grid.innerHTML = '';
      if (!accs.length) {
        grid.innerHTML = '<div style="color:var(--text-sub); padding:16px;">暂无账号。请前往“账号管理”添加账号。</div>';
        return;
      }

      accs.forEach(a => {
        const box = document.createElement('div');
        box.className = 'acc-card';
        const resStr = (a.last_results && a.last_results.length) ? a.last_results.join(' | ') : '尚未执行';
        
        // 登录态检测
        const auth = a.auth || {};
        const isAuthValid = !!auth.valid;
        const authBadge = isAuthValid
          ? `<span class="badge badge-success" title="${auth.summary || '登录有效'}">🟢 登录有效</span>`
          : `<span class="badge badge-disabled" title="${auth.summary || '未检测到凭据'}">🔴 凭据失效</span>`;

        // 7天得分概况
        const h7 = a.history_7d || { total_gained: 0, days: [] };
        let daysHtml = '';
        const maxG = Math.max(...(h7.days || []).map(d => d.gained || 0), 1);
        (h7.days || []).forEach(d => {
          const barH = Math.min(46, Math.max(6, Math.round(((d.gained || 0) / maxG) * 40)));
          const barBg = (d.gained > 0)
            ? 'background: linear-gradient(180deg, #10b981, #059669); box-shadow: 0 0 6px rgba(16,185,129,0.3);'
            : 'background: #334155;';
          const gainText = (d.gained > 0)
            ? `<span style="color:#34d399; font-weight:700; font-size:11px;">+${d.gained}</span>`
            : `<span style="color:#64748b; font-size:11px;">+0</span>`;
          const title = `${d.full_date} (起点: ${d.start}, 终点: ${d.end}, 增量: +${d.gained}, 状态: ${d.status})`;
          daysHtml += `
            <div style="display:flex; flex-direction:column; align-items:center; justify-content:flex-end; height:76px; cursor:pointer;" title="${title}">
              ${gainText}
              <div style="width:14px; height:${barH}px; ${barBg} border-radius:3px; margin:4px 0;"></div>
              <span style="font-size:10px; color:${d.is_today ? 'var(--primary); font-weight:700' : 'var(--text-sub)'};">${d.is_today ? '今天' : d.date}</span>
            </div>
          `;
        });

        const historyBlock = `
          <div style="margin-top:14px; background:rgba(0,0,0,0.25); border:1px solid #1e293b; border-radius:8px; padding:10px 12px;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:2px;">
              <span style="font-size:12px; font-weight:700; color:#e2e8f0; display:flex; align-items:center; gap:5px;">
                📈 最近 7 天得分概况
              </span>
              <span style="font-size:12px; font-weight:700; color:var(--success);">
                近7天累计: +${h7.total_gained || 0} 分
              </span>
            </div>
            <div style="display:grid; grid-template-columns: repeat(7, 1fr); gap:4px; text-align:center; padding-top:6px;">
              ${daysHtml}
            </div>
          </div>
        `;

        // 按钮组: 移除了单一无用的排期预览，依据有效性提供导出或重登
        let actionButtons = `
          <button class="btn btn-sm" onclick="runSingle('${a.name}')">立即运行</button>
          <button class="btn btn-secondary btn-sm" onclick="openFingerprintModal('${a.name}')">专属指纹</button>
        `;
        if (isAuthValid) {
          actionButtons += `
            <button class="btn btn-secondary btn-sm" onclick="exportCookie('${a.name}')">导出 Cookie</button>
            <button class="btn btn-secondary btn-sm" onclick="openLoginModal('${a.name}', true)">重新登录</button>
          `;
        } else {
          actionButtons += `
            <button class="btn btn-sm" style="background:#0284c7; color:#fff;" onclick="openLoginModal('${a.name}', false)">立即登录 / 授权</button>
          `;
        }

        const todayGained = (h7.days && h7.days.length) ? (h7.days[h7.days.length - 1].gained || 0) : 0;

        const diag = a.roaming_diagnosis || { code: 'unknown', text: '⚪ 待探测', desc: '' };
        let diagBadgeClass = 'badge-disabled';
        if (diag.code === 'matched') diagBadgeClass = 'badge-success';
        else if (diag.code === 'roaming') diagBadgeClass = 'badge-danger';
        else if (diag.code === 'error') diagBadgeClass = 'badge-danger';
        else if (diag.code === 'direct') diagBadgeClass = 'badge-info';

        const pGeo = a.proxy_geo || {};
        const proxyGeoStr = (pGeo.status === 'ok')
          ? `${pGeo.country_name || pGeo.country} · ${pGeo.city || pGeo.region} (${pGeo.ip})`
          : (a.proxy ? '代理待测' : '本机直连');

        box.innerHTML = `
          <div>
            <div class="acc-header">
              <span class="acc-name">${a.name}</span>
              <div class="flex-row">
                ${authBadge}
                <span class="badge ${a.enabled ? 'badge-success' : 'badge-disabled'}">${a.enabled ? '启用' : '停用'}</span>
              </div>
            </div>
            <div class="acc-meta">
              <div>💰 当前积分: <strong style="color:var(--primary); font-size:16px;">${a.balance || 0}</strong> <span style="font-size:12px; color:var(--success); margin-left:6px;">(今日: +${todayGained}分)</span></div>

              <!-- 微软三区架构与代理一致性诊断 -->
              <div style="background:rgba(255,255,255,0.02); border:1px solid rgba(255,255,255,0.07); border-radius:6px; padding:8px 10px; margin:7px 0;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:5px;">
                  <span style="font-weight:700; color:#94a3b8; font-size:11px;">🌍 微软业务区域架构与漫游诊断</span>
                  <span class="badge ${diagBadgeClass}" style="font-size:10.5px;">${diag.text}</span>
                </div>
                <div style="display:grid; grid-template-columns: 1fr 1fr 1.3fr; gap:6px; font-size:11px;">
                  <div><span style="color:#64748b;">👤 账户档案:</span> <b>${a.account_country || '待探测'}</b></div>
                  <div><span style="color:#64748b;">🎁 Rewards市场:</span> <b>${a.rewards_market || a.country || '待探测'}</b></div>
                  <div><span style="color:#64748b;">🌐 代理出口:</span> <b>${proxyGeoStr}</b></div>
                </div>
                ${diag.desc ? `<div style="font-size:10.5px; color:#94a3b8; margin-top:4px;">💡 ${diag.desc}</div>` : ''}
              </div>

              <div>⏱️ 上次运行: ${a.last_run || '从未'}</div>
              <div>🔑 凭据就绪: <span style="color:#cbd5e1">${auth.summary || '待检测'}</span></div>
              <div>🖥️ 专属指纹: <code style="font-size:11px; color:#a7f3d0;">${a.fingerprint_summary || '加载中...'}</code></div>
              <div style="margin-top:4px; font-size:12px; color:var(--text-sub);">
                近期结果: <span style="color:#cbd5e1">${resStr}</span>
              </div>
            </div>
            ${historyBlock}
          </div>
          <div style="margin-top:14px; display:flex; gap:8px; flex-wrap:wrap;">
            ${actionButtons}
          </div>
        `;
        grid.appendChild(box);
      });
    }

    function renderManageList(accs) {
      const el = document.getElementById('acc-table-list');
      el.innerHTML = '';
      if (!accs.length) {
        el.innerHTML = '<div style="color:var(--text-sub);">暂无账号</div>';
        return;
      }

      accs.forEach(a => {
        const auth = a.auth || {};
        const isAuthValid = !!auth.valid;
        const authBadge = isAuthValid
          ? `<span class="badge badge-success" style="font-size:11px;">🟢 登录有效</span>`
          : `<span class="badge badge-disabled" style="font-size:11px;">🔴 凭据失效</span>`;

        const diag = a.roaming_diagnosis || { code: 'unknown', text: '⚪ 待探测', desc: '' };
        let diagBadgeClass = 'badge-disabled';
        if (diag.code === 'matched') diagBadgeClass = 'badge-success';
        else if (diag.code === 'roaming') diagBadgeClass = 'badge-danger';
        else if (diag.code === 'error') diagBadgeClass = 'badge-danger';
        else if (diag.code === 'direct') diagBadgeClass = 'badge-info';

        const pGeo = a.proxy_geo || {};
        const pGeoShort = (pGeo.status === 'ok') ? `${pGeo.country_name || pGeo.country} (${pGeo.ip})` : (a.proxy ? '待测' : '直连');

        let authBtns = '';
        if (isAuthValid) {
          authBtns = `
            <button class="btn btn-secondary btn-sm" onclick="exportCookie('${a.name}')">导出 Cookie</button>
            <button class="btn btn-secondary btn-sm" onclick="openLoginModal('${a.name}', true)">重新登录</button>
          `;
        } else {
          authBtns = `
            <button class="btn btn-sm" style="background:#0284c7; color:#fff;" onclick="openLoginModal('${a.name}', false)">立即授权登录</button>
          `;
        }

        const row = document.createElement('div');
        row.style = 'display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--border); padding:12px 0; flex-wrap:wrap; gap:10px;';
        row.innerHTML = `
          <div>
            <div style="display:flex; align-items:center; gap:8px;">
              <strong style="font-size:16px; color:#fff;">${a.name}</strong>
              ${authBadge}
              <span class="badge ${diagBadgeClass}" style="font-size:11px;">${diag.text}</span>
              <span class="badge badge-info" style="font-size:11px;">近7天 +${a.history_7d?.total_gained || 0}分</span>
            </div>
            <div style="color:var(--text-sub); font-size:13px; margin-top:4px;">
              代理: <code>${a.proxy || '直连'}</code> | 👤 资料: <b>${a.account_country || '-'}</b> | 🎁 市场: <b>${a.rewards_market || a.country || '-'}</b> | 🌐 出口: <b>${pGeoShort}</b>
            </div>
            <div style="color:#64748b; font-size:12px; margin-top:2px;">
              指纹: <code style="color:#a7f3d0; font-size:11px;">${a.fingerprint_summary || '默认'}</code>
            </div>
          </div>
          <div class="flex-row">
            ${a.proxy ? `<button class="btn btn-secondary btn-sm" onclick="testAccountProxy('${a.name}', '${a.proxy}')">⚡ 测代理</button>` : ''}
            <button class="btn btn-secondary btn-sm" onclick="openFingerprintModal('${a.name}')">专属指纹</button>
            ${authBtns}
            <button class="btn btn-secondary btn-sm" onclick="toggleAcc('${a.name}', ${!a.enabled})">${a.enabled ? '停用' : '启用'}</button>
            <button class="btn btn-danger btn-sm" onclick="deleteAcc('${a.name}')">删除</button>
          </div>
        `;
        el.appendChild(row);
      });
    }

    async function saveNewAccount() {
      const name = document.getElementById('acc-name-input').value.trim();
      const proxy = document.getElementById('acc-proxy-input').value.trim() || null;
      const offset = parseInt(document.getElementById('acc-offset-input').value || 0);
      if (!name) return toast('请输入账号标识名称');

      await fetch('/api/accounts', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, proxy, offset_minutes: offset, enabled: true})
      });
      toast(`账号 ${name} 已保存`);
      document.getElementById('acc-name-input').value = '';
      document.getElementById('acc-proxy-input').value = '';
      fetchStatus();
    }

    async function toggleAcc(name, enabled) {
      await fetch('/api/accounts/toggle', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, enabled})
      });
      fetchStatus();
    }

    async function deleteAcc(name) {
      if (!confirm(`确定删除账号 ${name} 及其所有本地状态吗？`)) return;
      await fetch(`/api/accounts?name=${encodeURIComponent(name)}`, { method: 'DELETE' });
      toast(`账号 ${name} 已删除`);
      fetchStatus();
    }

    async function runSingle(name) {
      toast(`正在启动账号 ${name} 任务...`);
      await fetch('/api/run', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name})
      });
      setTab('logs');
    }

    async function triggerRunAll() {
      toast('已提交全部账号执行队列');
      await fetch('/api/run', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: '--all'})
      });
      setTab('logs');
    }

    /* 独立弹窗控制 */
    function openPipelineModal() {
      document.getElementById('pipeline-modal').style.display = 'flex';
    }
    function closePipelineModal() {
      document.getElementById('pipeline-modal').style.display = 'none';
    }

    /* Cookie 导出弹窗 */
    async function exportCookie(name) {
      try {
        const res = await fetch(`/api/auth/cookies?name=${encodeURIComponent(name)}`);
        const d = await res.json();
        if (d.status !== 'ok') throw new Error(d.message || '获取失败');
        document.getElementById('export-modal-title').innerText = `Cookie 导出 - [${name}]`;
        document.getElementById('export-summary').innerText = `已读取 ${d.count} 条有效凭据 (含 _U: ${d.has_u ? '是' : '否'})，可直接导入 Cookie-Editor:`;
        document.getElementById('export-text').value = d.json_str || '[]';
        document.getElementById('export-cookie-modal').style.display = 'flex';
      } catch(err) {
        toast('导出 Cookie 失败: ' + err);
      }
    }
    function closeExportModal() {
      document.getElementById('export-cookie-modal').style.display = 'none';
    }
    function copyExportedCookies() {
      const ta = document.getElementById('export-text');
      ta.select();
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(ta.value).then(() => {
          toast('Cookie JSON 已复制到剪贴板！');
        });
      } else {
        document.execCommand('copy');
        toast('Cookie JSON 已复制到剪贴板！');
      }
    }

    /* 账号专属硬件指纹弹窗 */
    async function openFingerprintModal(name) {
      activeAccountName = name;
      document.getElementById('fp-modal-title').innerText = `账号反检测专属硬件指纹: [${name}]`;
      try {
        const res = await fetch(`/api/accounts/fingerprint?name=${encodeURIComponent(name)}`);
        const d = await res.json();
        if (d.status !== 'ok') throw new Error(d.message || '获取失败');
        currentFpData = d.data || {};
        _populateFpForm(currentFpData);
        document.getElementById('fingerprint-modal').style.display = 'flex';
      } catch(e) {
        toast('读取指纹失败: ' + e);
      }
    }

    function _populateFpForm(fp) {
      document.getElementById('fp-os').value = fp.os_name || 'Windows 11';
      document.getElementById('fp-edge-ver').value = fp.edge_version || '141';
      document.getElementById('fp-gpu').value = fp.webgl_renderer || 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)';
      document.getElementById('fp-cpu').value = fp.hardware_concurrency || 8;
      document.getElementById('fp-mem').value = fp.device_memory || 16;
      document.getElementById('fp-res').value = `${fp.screen_width || 1920},${fp.screen_height || 1080}`;
      document.getElementById('fp-tz').value = fp.timezone || 'Asia/Singapore';
      document.getElementById('fp-locale').value = fp.locale || 'zh-CN,zh;q=0.9,en;q=0.8';
      document.getElementById('fp-ua').value = fp.user_agent || '';
    }

    function closeFingerprintModal() {
      document.getElementById('fingerprint-modal').style.display = 'none';
      fetchStatus();
    }

    async function randomizeFingerprint() {
      if (!confirm(`确定为账号 ${activeAccountName} 重新随机派生一套全新的硬件指纹吗？`)) return;
      try {
        const res = await fetch('/api/accounts/fingerprint/generate', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: activeAccountName})
        });
        const d = await res.json();
        if (d.status === 'ok') {
          currentFpData = d.data;
          _populateFpForm(currentFpData);
          toast('已重新随机生成专属硬件指纹！');
        }
      } catch(e) {
        toast('生成失败: ' + e);
      }
    }

    async function saveFingerprintConfig() {
      const resVal = document.getElementById('fp-res').value.split(',');
      const osVal = document.getElementById('fp-os').value;
      const body = {
        name: activeAccountName,
        os_name: osVal,
        platform: 'Win32',
        platform_version: osVal === 'Windows 11' ? '15.0.0' : '10.0.0',
        edge_version: document.getElementById('fp-edge-ver').value.trim() || '141',
        full_version: currentFpData?.full_version || '141.0.3200.50',
        webgl_renderer: document.getElementById('fp-gpu').value,
        webgl_vendor: document.getElementById('fp-gpu').value.includes('Intel') ? 'Google Inc. (Intel)' : (document.getElementById('fp-gpu').value.includes('AMD') ? 'Google Inc. (AMD)' : 'Google Inc. (NVIDIA)'),
        hardware_concurrency: parseInt(document.getElementById('fp-cpu').value) || 8,
        device_memory: parseInt(document.getElementById('fp-mem').value) || 16,
        screen_width: parseInt(resVal[0]) || 1920,
        screen_height: parseInt(resVal[1]) || 1080,
        timezone: document.getElementById('fp-tz').value.trim() || 'Asia/Singapore',
        locale: document.getElementById('fp-locale').value.trim() || 'zh-CN,zh;q=0.9,en;q=0.8',
        user_agent: document.getElementById('fp-ua').value.trim()
      };

      await fetch('/api/accounts/fingerprint', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
      });
      toast('专属指纹已保存');
      closeFingerprintModal();
    }

    async function alignFingerprintWithProxy() {
      if (!activeAccountName) return;
      toast('正在探测绑定代理出口并同步时区与语言...');
      try {
        const res = await fetch('/api/accounts/fingerprint/align-proxy', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: activeAccountName})
        });
        const d = await res.json();
        if (d.status === 'ok') {
          currentFpData = d.data;
          _populateFpForm(currentFpData);
          toast(`✅ 已自动匹配代理地区 [${d.proxy_country}]，时区同步为: ${d.timezone}`);
        } else {
          toast('同步失败: ' + (d.message || '未知错误'));
        }
      } catch(e) {
        toast('请求失败: ' + e);
      }
    }

    async function testProxyInForm() {
      const proxy = document.getElementById('acc-proxy-input').value.trim();
      const box = document.getElementById('proxy-test-box');
      if (!proxy) {
        box.style.display = 'block';
        box.style.background = 'rgba(239,68,68,0.1)';
        box.style.border = '1px solid rgba(239,68,68,0.3)';
        box.style.color = '#ef4444';
        box.innerText = '请先输入代理地址 (如 http://user:pass@127.0.0.1:7890)';
        return;
      }
      box.style.display = 'block';
      box.style.background = 'rgba(56,189,248,0.1)';
      box.style.border = '1px solid rgba(56,189,248,0.3)';
      box.style.color = '#38bdf8';
      box.innerText = '⏳ 正在通过该代理探测出口 IP 与地理位置...';
      try {
        const res = await fetch('/api/accounts/proxy-test', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({proxy})
        });
        const d = await res.json();
        if (d.status === 'ok') {
          const g = d.data;
          box.style.background = 'rgba(16,185,129,0.1)';
          box.style.border = '1px solid rgba(16,185,129,0.3)';
          box.style.color = '#10b981';
          box.innerHTML = `🟢 <b>代理连通成功!</b> 出口 IP: <code>${g.ip}</code> | 地区: <b>${g.country_name || g.country} · ${g.city || ''}</b> | 时区: <b>${g.timezone}</b> | 延迟: <b>${g.latency_ms}ms</b>`;
        } else {
          box.style.background = 'rgba(239,68,68,0.1)';
          box.style.border = '1px solid rgba(239,68,68,0.3)';
          box.style.color = '#ef4444';
          box.innerHTML = `🔴 <b>代理连接失败:</b> ${d.message}`;
        }
      } catch(e) {
        box.style.background = 'rgba(239,68,68,0.1)';
        box.style.border = '1px solid rgba(239,68,68,0.3)';
        box.style.color = '#ef4444';
        box.innerText = '请求失败: ' + e;
      }
    }

    async function testAccountProxy(name, proxy) {
      toast(`正在测试账号 [${name}] 的代理...`);
      try {
        const res = await fetch('/api/accounts/proxy-test', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({proxy, name})
        });
        const d = await res.json();
        if (d.status === 'ok') {
          const g = d.data;
          toast(`🟢 [${name}] 代理正常! ${g.country_name || g.country} (${g.ip}), 延迟 ${g.latency_ms}ms`);
          fetchStatus();
        } else {
          toast(`🔴 [${name}] 代理失败: ${d.message}`);
        }
      } catch(e) {
        toast(`🔴 测试失败: ${e}`);
      }
    }

    /* 全量参数配置装载与保存 */
    async function loadSettingsForm() {
      try {
        const res = await fetch('/api/settings');
        const s = await res.json();
        
        // 1. search
        document.getElementById('s-dwell-min').value = s.search?.dwell_min_seconds ?? 180;
        document.getElementById('s-dwell-max').value = s.search?.dwell_max_seconds ?? 300;
        document.getElementById('s-stall-max').value = s.search?.stall_max_retries ?? 12;
        document.getElementById('s-hard-cap').value = s.search?.hard_cap_attempts ?? 60;
        document.getElementById('s-budget-min').value = s.search?.budget_minutes ?? 120;
        document.getElementById('s-click-prob').value = s.search?.click_result_probability ?? 0.85;
        document.getElementById('s-query-lang').value = s.search?.query_language ?? 'auto';

        // 2. cards
        document.getElementById('c-attr-min').value = s.cards?.attribution_wait_min ?? 60;
        document.getElementById('c-attr-max').value = s.cards?.attribution_wait_max ?? 120;
        document.getElementById('c-budget-min').value = s.cards?.budget_minutes ?? 35;
        document.getElementById('c-quiz-min').value = s.cards?.quiz_step_delay_min ?? 3;
        document.getElementById('c-quiz-max').value = s.cards?.quiz_step_delay_max ?? 6;

        // 3. mobile
        document.getElementById('m-read-count').value = s.mobile?.read_article_count ?? 10;
        document.getElementById('m-read-min').value = s.mobile?.read_interval_min ?? 60;
        document.getElementById('m-read-max').value = s.mobile?.read_interval_max ?? 120;
        document.getElementById('m-edge-reports').value = s.mobile?.edge30m_reports ?? 6;
        document.getElementById('m-edge-interval').value = s.mobile?.edge30m_interval ?? 305;

        // 4. schedule
        document.getElementById('sch-daily-enabled').value = String(s.schedule?.daily_enabled !== false);
        document.getElementById('sch-daily-time').value = s.schedule?.daily_time || '01:30';
        document.getElementById('cfg-fast-mode').value = String(!!s.schedule?.fast_mode);
        document.getElementById('sch-acc-min').value = s.schedule?.inter_account_wait_min ?? 15;
        document.getElementById('sch-acc-max').value = s.schedule?.inter_account_wait_max ?? 40;
        document.getElementById('sch-stage-min').value = s.schedule?.inter_stage_wait_min ?? 3;
        document.getElementById('sch-stage-max').value = s.schedule?.inter_stage_wait_max ?? 5;
        document.getElementById('sch-start-delay').value = s.schedule?.random_start_delay_max ?? 25;
        document.getElementById('sch-retry-wait').value = s.schedule?.retry_wait_minutes ?? 4;

        // 5. browser
        document.getElementById('b-headless').value = String(s.browser?.headless !== false);
        document.getElementById('b-webrtc').value = String(s.browser?.disable_webrtc_leak !== false);
        document.getElementById('b-binary').value = s.browser?.binary || '';

        // 6. notify
        const chs = s.notify?.channels || ['stdout'];
        document.getElementById('n-ch-stdout').checked = chs.includes('stdout');
        document.getElementById('n-ch-serverchan').checked = chs.includes('serverchan');
        document.getElementById('n-ch-telegram').checked = chs.includes('telegram');
        document.getElementById('n-ch-webhook').checked = chs.includes('webhook');

        const ons = s.notify?.on || ['error', 'summary'];
        document.getElementById('n-on-error').checked = ons.includes('error');
        document.getElementById('n-on-summary').checked = ons.includes('summary');

        document.getElementById('sc-key').value = s.notify?.serverchan?.sendkey || '';
        document.getElementById('sc-channel').value = s.notify?.serverchan?.channel || '';
        document.getElementById('tg-token').value = s.notify?.telegram?.bot_token || '';
        document.getElementById('tg-chatid').value = s.notify?.telegram?.chat_id || '';
        document.getElementById('wh-url').value = s.notify?.webhook?.url || '';
        document.getElementById('wh-method').value = s.notify?.webhook?.method || 'POST';

      } catch(err) {
        toast('读取配置失败: ' + err);
      }
    }

    async function submitSettings() {
      const chs = [];
      if (document.getElementById('n-ch-stdout').checked) chs.push('stdout');
      if (document.getElementById('n-ch-serverchan').checked) chs.push('serverchan');
      if (document.getElementById('n-ch-telegram').checked) chs.push('telegram');
      if (document.getElementById('n-ch-webhook').checked) chs.push('webhook');

      const ons = [];
      if (document.getElementById('n-on-error').checked) ons.push('error');
      if (document.getElementById('n-on-summary').checked) ons.push('summary');

      const body = {
        search: {
          dwell_min_seconds: parseInt(document.getElementById('s-dwell-min').value) || 180,
          dwell_max_seconds: parseInt(document.getElementById('s-dwell-max').value) || 300,
          stall_max_retries: parseInt(document.getElementById('s-stall-max').value) || 12,
          hard_cap_attempts: parseInt(document.getElementById('s-hard-cap').value) || 60,
          budget_minutes: parseInt(document.getElementById('s-budget-min').value) || 120,
          click_result_probability: parseFloat(document.getElementById('s-click-prob').value) || 0.85,
          query_language: document.getElementById('s-query-lang').value || 'auto'
        },
        cards: {
          attribution_wait_min: parseInt(document.getElementById('c-attr-min').value) || 60,
          attribution_wait_max: parseInt(document.getElementById('c-attr-max').value) || 120,
          budget_minutes: parseInt(document.getElementById('c-budget-min').value) || 35,
          quiz_step_delay_min: parseInt(document.getElementById('c-quiz-min').value) || 3,
          quiz_step_delay_max: parseInt(document.getElementById('c-quiz-max').value) || 6
        },
        mobile: {
          read_article_count: parseInt(document.getElementById('m-read-count').value) || 10,
          read_interval_min: parseInt(document.getElementById('m-read-min').value) || 60,
          read_interval_max: parseInt(document.getElementById('m-read-max').value) || 120,
          edge30m_reports: parseInt(document.getElementById('m-edge-reports').value) || 6,
          edge30m_interval: parseInt(document.getElementById('m-edge-interval').value) || 305
        },
        schedule: {
          daily_enabled: document.getElementById('sch-daily-enabled').value === 'true',
          daily_time: document.getElementById('sch-daily-time').value.trim() || '01:30',
          fast_mode: document.getElementById('cfg-fast-mode').value === 'true',
          inter_account_wait_min: parseInt(document.getElementById('sch-acc-min').value) || 15,
          inter_account_wait_max: parseInt(document.getElementById('sch-acc-max').value) || 40,
          inter_stage_wait_min: parseInt(document.getElementById('sch-stage-min').value) || 3,
          inter_stage_wait_max: parseInt(document.getElementById('sch-stage-max').value) || 5,
          random_start_delay_max: parseInt(document.getElementById('sch-start-delay').value) || 25,
          retry_wait_minutes: parseInt(document.getElementById('sch-retry-wait').value) || 4
        },
        browser: {
          headless: document.getElementById('b-headless').value === 'true',
          disable_webrtc_leak: document.getElementById('b-webrtc').value === 'true',
          binary: document.getElementById('b-binary').value.trim() || null
        },
        notify: {
          channels: chs.length ? chs : ['stdout'],
          on: ons.length ? ons : ['error', 'summary'],
          serverchan: {
            sendkey: document.getElementById('sc-key').value.trim(),
            channel: document.getElementById('sc-channel').value.trim()
          },
          telegram: {
            bot_token: document.getElementById('tg-token').value.trim(),
            chat_id: document.getElementById('tg-chatid').value.trim()
          },
          webhook: {
            url: document.getElementById('wh-url').value.trim(),
            method: document.getElementById('wh-method').value
          }
        }
      };

      await fetch('/api/settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
      });
      toast('全量风控与通知参数已持久化保存');
    }

    async function resetSettingsDefault() {
      if (!confirm('确定将所有风控与节奏参数恢复为官方出厂默认值吗？')) return;
      await fetch('/api/settings/reset', { method: 'POST' });
      toast('已恢复出厂默认配置');
      loadSettingsForm();
    }

    async function sendTestPush() {
      toast('正在发送测试推送...');
      const res = await fetch('/api/notify-test', { method: 'POST' });
      const d = await res.json();
      toast(d.message || '请求已发送');
    }

    async function loadLogsNow() {
      try {
        const res = await fetch('/api/logs');
        const text = await res.text();
        const box = document.getElementById('log-box');
        box.innerText = text || '暂无日志记录';
        box.scrollTop = box.scrollHeight;
      } catch(e) {}
    }

    /* 登录弹窗逻辑 (根据当前有效性展示提示) */
    function openLoginModal(name, isReLogin) {
      activeAccountName = name;
      document.getElementById('modal-title').innerText = isReLogin
        ? `账号重新登录 / 凭据更新: [${name}]`
        : `账号授权登录: [${name}]`;
      
      const hintEl = document.getElementById('login-auth-hint');
      if (isReLogin) {
        hintEl.innerHTML = `<span style="color:var(--success)">🟢 该账号当前登录凭据有效。</span> 如需更换微软账户或刷新会话，请在下方直接输入新账号密码或导入新 Cookie。`;
      } else {
        hintEl.innerHTML = `<span style="color:var(--danger)">🔴 该账号尚未登录或凭据失效。</span> 推荐在下方输入微软账号密码一键全自动登录，亦可粘贴 Cookie 激活。`;
      }

      document.getElementById('login-username').value = name.includes('@') ? name : '';
      document.getElementById('login-password').value = '';
      document.getElementById('login-pwd-status').innerHTML = '准备就绪。支持账号密码登录、手机 Authenticator 二次验证、安全验证码输入及人机拼图远程交互。';
      document.getElementById('login-pwd-btn').disabled = false;
      document.getElementById('login-cancel-btn').style.display = 'none';
      document.getElementById('login-auth-push-box').style.display = 'none';
      document.getElementById('login-otp-box').style.display = 'none';
      document.getElementById('interactive-canvas-box').style.display = 'none';
      isCanvasOpen = false;
      if (loginPollTimer) { clearInterval(loginPollTimer); loginPollTimer = null; }
      document.getElementById('ck-text-input').value = '';
      document.getElementById('ck-result-msg').innerText = '';
      document.getElementById('login-modal').style.display = 'flex';
      switchModalTab('pwd');
    }

    let loginPollTimer = null;
    let isCanvasOpen = false;

    function closeLoginModal() {
      if (loginPollTimer) { clearInterval(loginPollTimer); loginPollTimer = null; }
      document.getElementById('login-modal').style.display = 'none';
      fetchStatus();
    }

    function switchModalTab(tab) {
      document.getElementById('modal-tab-pwd').classList.toggle('active', tab === 'pwd');
      document.getElementById('modal-tab-ck').classList.toggle('active', tab === 'ck');
      document.getElementById('modal-panel-pwd').style.display = tab === 'pwd' ? 'block' : 'none';
      document.getElementById('modal-panel-ck').style.display = tab === 'ck' ? 'block' : 'none';
    }

    function toggleInteractiveCanvas() {
      isCanvasOpen = !isCanvasOpen;
      const box = document.getElementById('interactive-canvas-box');
      box.style.display = isCanvasOpen ? 'block' : 'none';
      if (isCanvasOpen) {
        refreshLiveScreenshot();
      }
    }

    function refreshLiveScreenshot() {
      if (!activeAccountName) return;
      const img = document.getElementById('live-screen-img');
      img.src = `/api/auth/session/screenshot?name=${encodeURIComponent(activeAccountName)}&_t=${Date.now()}`;
    }

    async function handleScreenClick(e) {
      if (!activeAccountName) return;
      const img = e.target;
      const scaleX = (img.naturalWidth || img.clientWidth) / img.clientWidth;
      const scaleY = (img.naturalHeight || img.clientHeight) / img.clientHeight;
      const x = Math.round(e.offsetX * scaleX);
      const y = Math.round(e.offsetY * scaleY);
      try {
        await fetch('/api/auth/session/mouse', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: activeAccountName, x, y})
        });
        setTimeout(refreshLiveScreenshot, 500);
      } catch(err) {
        console.error('Click error:', err);
      }
    }

    async function startLoginSession() {
      const u = document.getElementById('login-username').value.trim();
      const p = document.getElementById('login-password').value;
      if (!u || !p) return toast('请输入微软账号与密码');

      const btn = document.getElementById('login-pwd-btn');
      const cancelBtn = document.getElementById('login-cancel-btn');
      const statusEl = document.getElementById('login-pwd-status');
      btn.disabled = true;
      cancelBtn.style.display = 'inline-block';
      statusEl.innerHTML = '<span style="color:var(--primary)">⏳ 正在启动安全 Chromium 实例并连接微软登录网关...</span>';

      try {
        const res = await fetch('/api/auth/session/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: activeAccountName, username: u, password: p})
        });
        const d = await res.json();
        if (d.status !== 'ok') {
          throw new Error(d.message || '启动失败');
        }
        if (loginPollTimer) clearInterval(loginPollTimer);
        loginPollTimer = setInterval(pollLoginSession, 1200);
      } catch(err) {
        statusEl.innerHTML = `<span style="color:var(--danger)">❌ 启动失败: ${err}</span>`;
        btn.disabled = false;
        cancelBtn.style.display = 'none';
      }
    }

    async function pollLoginSession() {
      if (!activeAccountName) return;
      try {
        const res = await fetch(`/api/auth/session/status?name=${encodeURIComponent(activeAccountName)}`);
        const s = await res.json();
        const statusEl = document.getElementById('login-pwd-status');
        const authPushBox = document.getElementById('login-auth-push-box');
        const otpBox = document.getElementById('login-otp-box');

        if (s.status === 'success') {
          clearInterval(loginPollTimer);
          loginPollTimer = null;
          statusEl.innerHTML = `<strong style="color:var(--success)">🎉 登录成功！Cookie 与 Token 已自动换取并固化（积分: ${s.result?.balance || 0}, 区域: ${s.result?.country || '未知'}）。</strong>`;
          authPushBox.style.display = 'none';
          otpBox.style.display = 'none';
          document.getElementById('login-pwd-btn').disabled = false;
          document.getElementById('login-cancel-btn').style.display = 'none';
          toast('微软账号登录成功');
          fetchStatus();
          return;
        }

        if (s.status === 'error') {
          clearInterval(loginPollTimer);
          loginPollTimer = null;
          statusEl.innerHTML = `<span style="color:var(--danger)">❌ 登录失败: ${s.message || s.error_msg || '未知错误'}</span>`;
          document.getElementById('login-pwd-btn').disabled = false;
          document.getElementById('login-cancel-btn').style.display = 'none';
          toast('登录中断: ' + (s.message || ''));
          return;
        }

        statusEl.innerHTML = `<span style="color:var(--primary)">⏳ ${s.message || '正在处理中...'}</span>`;

        if (s.status === 'wait_auth_push' && s.auth_code) {
          authPushBox.style.display = 'block';
          document.getElementById('auth-code-val').innerText = s.auth_code;
        } else {
          authPushBox.style.display = 'none';
        }

        if (s.status === 'wait_otp') {
          otpBox.style.display = 'block';
          document.getElementById('login-otp-prompt').innerText = s.otp_prompt || '微软要求输入安全验证码';
        } else {
          otpBox.style.display = 'none';
        }

        if (s.status === 'wait_interactive' && !isCanvasOpen) {
          toggleInteractiveCanvas();
        }

        if (isCanvasOpen) {
          refreshLiveScreenshot();
        }

      } catch(err) {
        console.error('Poll error:', err);
      }
    }

    async function submitSessionOtp() {
      const code = document.getElementById('login-otp-input').value.trim();
      if (!code) return toast('请输入验证码');
      toast('正在提交安全验证码...');
      try {
        const res = await fetch('/api/auth/session/submit-otp', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: activeAccountName, code})
        });
        const d = await res.json();
        if (d.status === 'ok') {
          document.getElementById('login-otp-input').value = '';
          toast('验证码已提交');
        } else {
          toast('提交失败: ' + (d.message || ''));
        }
      } catch(e) {
        toast('提交异常: ' + e);
      }
    }

    async function cancelLoginSession() {
      if (!confirm('确定取消当前登录流程吗？')) return;
      try {
        await fetch('/api/auth/session/cancel', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: activeAccountName})
        });
      } catch(e) {}
      if (loginPollTimer) { clearInterval(loginPollTimer); loginPollTimer = null; }
      document.getElementById('login-pwd-btn').disabled = false;
      document.getElementById('login-cancel-btn').style.display = 'none';
      document.getElementById('login-pwd-status').innerHTML = '登录流程已取消。';
      document.getElementById('login-auth-push-box').style.display = 'none';
      document.getElementById('login-otp-box').style.display = 'none';
    }

    const submitCredentialLogin = startLoginSession;

    async function submitCookies() {
      const content = document.getElementById('ck-text-input').value.trim();
      if (!content) return toast('请输入 Cookie 内容');

      const btn = document.getElementById('login-ck-btn');
      const msgEl = document.getElementById('ck-result-msg');
      btn.disabled = true;
      msgEl.innerHTML = '<span style="color:var(--primary)">⏳ 正在解析并尝试静默生成 Token...</span>';

      try {
        const res = await fetch('/api/auth/cookies', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: activeAccountName, cookies_raw: content})
        });
        const d = await res.json();
        if (d.status === 'ok') {
          const tokMsg = d.has_token ? '已自动生成并激活 Token' : '已就绪 (可跑搜索任务)';
          msgEl.innerHTML = `<span style="color:var(--success)">✅ 导入成功 (${d.count}条, ${tokMsg})</span>`;
          toast('Cookie 导入成功');
          fetchStatus();
        } else {
          msgEl.innerHTML = `<span style="color:var(--danger)">❌ 校验失败: ${d.message}</span>`;
        }
      } catch(e) {
        msgEl.innerHTML = `<span style="color:var(--danger)">❌ 请求失败: ${e}</span>`;
      } finally {
        btn.disabled = false;
      }
    }

    // 页面启动
    window.addEventListener('DOMContentLoaded', () => {
      fetchStatus();
    });
  </script>
</body>
</html>
"""


class WebHandler(BaseHTTPRequestHandler):
    def send_cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_cors()
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path

        if path in ('/', '/index.html'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_cors()
            self.end_headers()
            self.wfile.write(HTML_CONTENT.encode('utf-8'))
            return

        if path == '/api/status':
            self._handle_status()
            return

        if path == '/api/settings':
            from msr import settings
            st = settings.load_settings()
            self._json_resp(st)
            return

        if path == '/api/logs':
            self._handle_logs()
            return

        if path == '/api/auth/cookies':
            params = urllib.parse.parse_qs(u.query)
            name = params.get('name', [None])[0]
            if not name:
                self._json_resp({'status': 'error', 'message': '缺少账号参数 name'}, code=400)
                return
            from msr import config
            cks = config.load_json(config.pfile(name, 'cookie.json'), []) or []
            has_u = any(c.get('name') == '_U' for c in cks) if isinstance(cks, list) else False
            self._json_resp({
                'status': 'ok',
                'name': name,
                'count': len(cks) if isinstance(cks, list) else 0,
                'has_u': has_u,
                'cookies': cks,
                'json_str': json.dumps(cks, indent=2, ensure_ascii=False)
            })
            return

        if path == '/api/accounts/fingerprint':
            params = urllib.parse.parse_qs(u.query)
            name = params.get('name', [None])[0]
            if not name:
                self._json_resp({'status': 'error', 'message': '缺少账号参数 name'}, code=400)
                return
            from msr import fingerprint
            fp = fingerprint.load_or_create(name)
            self._json_resp({'status': 'ok', 'data': fp})
            return

        if path == '/api/auth/session/status':
            params = urllib.parse.parse_qs(u.query)
            name = params.get('name', [None])[0]
            if not name:
                self._json_resp({'status': 'error', 'message': '缺少账号参数 name'}, code=400)
                return
            from msr import auth
            sess = auth.get_session(name)
            if not sess:
                self._json_resp({'status': 'idle', 'name': name})
            else:
                self._json_resp(sess.to_dict())
            return

        if path == '/api/auth/session/screenshot':
            params = urllib.parse.parse_qs(u.query)
            name = params.get('name', [None])[0]
            from msr import auth
            img_bytes = auth.get_session_screenshot(name) if name else None
            if img_bytes:
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
                self.send_cors()
                self.end_headers()
                self.wfile.write(img_bytes)
            else:
                self.send_response(200)
                self.send_header('Content-Type', 'image/gif')
                self.send_header('Cache-Control', 'no-store, no-cache')
                self.send_cors()
                self.end_headers()
                self.wfile.write(b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')
            return

        self.send_error(404, 'Not Found')

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode('utf-8') if length > 0 else '{}'
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {}

        if path == '/api/settings':
            from msr import settings
            cur = settings.load_settings()
            merged = settings._deep_merge(cur, payload)
            settings.save_settings(merged)
            append_live_log('Web 控制台更新了全局风控与执行配置')
            self._json_resp({'status': 'ok'})
            return

        if path == '/api/settings/reset':
            from msr import settings
            settings.save_settings(settings.DEFAULT_SETTINGS)
            append_live_log('Web 控制台已将配置重置为官方默认值')
            self._json_resp({'status': 'ok'})
            return

        if path == '/api/accounts':
            from msr import config
            accs = config.accounts()
            name = (payload.get('name') or '').strip()
            if not name:
                self._json_resp({'status': 'error', 'message': '账号名不能为空'}, code=400)
                return
            target = next((a for a in accs if a['name'] == name), None)
            if target:
                target.update(payload)
            else:
                accs.append(payload)
            config.save_accounts(accs)
            append_live_log(f'保存账号配置: {name}')
            self._json_resp({'status': 'ok'})
            return

        if path == '/api/accounts/fingerprint':
            from msr import fingerprint
            name = payload.get('name')
            if not name:
                self._json_resp({'status': 'error', 'message': '缺少账号参数 name'}, code=400)
                return
            fingerprint.save_fingerprint(name, payload)
            append_live_log(f'更新账号 [{name}] 的专属反检测硬件指纹配置')
            self._json_resp({'status': 'ok'})
            return

        if path == '/api/accounts/fingerprint/generate':
            from msr import fingerprint, config
            name = payload.get('name')
            if not name:
                self._json_resp({'status': 'error', 'message': '缺少账号参数 name'}, code=400)
                return
            rg = config.load_json(config.pfile(name, 'region.json'), {}) or {}
            c = rg.get('country') or 'SG'
            new_fp = fingerprint.generate_fingerprint(name, country=c, randomize=True)
            fingerprint.save_fingerprint(name, new_fp)
            append_live_log(f'重新随机生成了账号 [{name}] 的专属反检测硬件指纹')
            self._json_resp({'status': 'ok', 'data': new_fp})
            return

        if path == '/api/accounts/toggle':
            from msr import config
            accs = config.accounts()
            name = payload.get('name')
            enabled = bool(payload.get('enabled'))
            for a in accs:
                if a['name'] == name:
                    a['enabled'] = enabled
            config.save_accounts(accs)
            append_live_log(f'切换账号状态: {name} -> {"启用" if enabled else "停用"}')
            self._json_resp({'status': 'ok'})
            return

        if path == '/api/notify-test':
            from msr import notify
            append_live_log('触发通知测试推送...')
            notify.notify('MSR-ArmBot Web 控制台测试', '恭喜！通知通道配置正常，Server酱 / Webhook 推送成功！', level='info')
            self._json_resp({'status': 'ok', 'message': '推送测试请求已发送'})
            return

        if path == '/api/auth/credential':
            self._handle_credential_login(payload)
            return

        if path == '/api/auth/session/start':
            from msr import auth, config
            name = payload.get('name')
            username = payload.get('username')
            password = payload.get('password')
            if not name or not username or not password:
                self._json_resp({'status': 'error', 'message': '账号、用户名和密码不能为空'}, code=400)
                return
            acc_cfg = config.get_account(name) or {}
            proxy = acc_cfg.get('proxy')
            sess = auth.start_session(name, username, password, proxy=proxy,
                                      log_cb=lambda m: append_live_log(f'[{name}] {m}'))
            self._json_resp({'status': 'ok', 'message': '登录会话已启动', 'session': sess.to_dict()})
            return

        if path == '/api/auth/session/submit-otp':
            from msr import auth
            name = payload.get('name')
            code = (payload.get('code') or '').strip()
            if not name or not code:
                self._json_resp({'status': 'error', 'message': '账号与验证码不能为空'}, code=400)
                return
            res = auth.submit_session_otp(name, code)
            self._json_resp(res)
            return

        if path == '/api/auth/session/mouse':
            from msr import auth
            name = payload.get('name')
            x = payload.get('x', 0)
            y = payload.get('y', 0)
            res = auth.session_mouse_click(name, x, y)
            self._json_resp(res)
            return

        if path == '/api/auth/session/cancel':
            from msr import auth
            name = payload.get('name')
            if name:
                auth.cancel_session(name)
            self._json_resp({'status': 'ok'})
            return

        if path == '/api/auth/cookies':
            self._handle_cookies_import(payload)
            return

        if path == '/api/accounts/proxy-test':
            from msr import geo
            proxy = payload.get('proxy')
            name = payload.get('name')
            p_geo = geo.probe_proxy_geo(proxy)
            if name and p_geo.get('status') == 'ok':
                from msr import config
                config.save_json(config.pfile(name, 'proxy_geo.json'), {
                    'proxy': proxy,
                    'probed_time': time.time(),
                    'geo': p_geo
                })
            if p_geo.get('status') == 'ok':
                self._json_resp({'status': 'ok', 'data': p_geo})
            else:
                self._json_resp({'status': 'error', 'message': p_geo.get('message', '连接失败')})
            return

        if path == '/api/accounts/fingerprint/align-proxy':
            from msr import fingerprint, config
            name = payload.get('name')
            if not name:
                self._json_resp({'status': 'error', 'message': '缺少账号参数 name'}, code=400)
                return
            acc_cfg = config.get_account(name) or {}
            try:
                new_fp, p_geo = fingerprint.align_with_proxy(name, acc_cfg.get('proxy'))
                append_live_log(f'[{name}] 已将专属指纹时区与语言自动对齐至代理出口: {p_geo.get("country_name")} ({p_geo.get("timezone")})')
                self._json_resp({
                    'status': 'ok',
                    'data': new_fp,
                    'geo': p_geo,
                    'proxy_country': p_geo.get('country_name') or p_geo.get('country'),
                    'timezone': new_fp.get('timezone')
                })
            except Exception as ex:
                self._json_resp({'status': 'error', 'message': str(ex)}, code=500)
            return

        if path == '/api/run':
            self._handle_run(payload, u)
            return

        self.send_error(404, 'Not Found')

    def do_DELETE(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == '/api/accounts':
            from msr import config
            params = urllib.parse.parse_qs(u.query)
            name = params.get('name', [None])[0]
            if name:
                accs = [a for a in config.accounts() if a['name'] != name]
                config.save_accounts(accs)
                append_live_log(f'删除账号: {name}')
            self._json_resp({'status': 'ok'})
            return
        self.send_error(404, 'Not Found')

    def _handle_status(self):
        from msr import config, auth, history, fingerprint, geo
        accs = config.accounts()
        detailed = []
        for a in accs:
            name = a['name']
            st = config.load_json(config.pfile(name, 'state.json'), {}) or {}
            rg = config.load_json(config.pfile(name, 'region.json'), {}) or {}
            bal = st.get('balance', rg.get('balance', 0))
            auth_info = auth.get_auth_status(name)
            h7 = history.get_7day_history(name, current_balance=bal)
            fp = fingerprint.load_or_create(name, country=rg.get('country'), proxy=a.get('proxy'))
            gpu_short = fp.get('webgl_renderer', '').split('Direct3D')[0].replace('ANGLE (', '').split(',')[0].strip()
            fp_summary = f"{fp.get('os_name', 'Win11')} | {gpu_short} | {fp.get('hardware_concurrency', 8)}核{fp.get('device_memory', 16)}G | {fp.get('screen_width', 1920)}x{fp.get('screen_height', 1080)}"

            # 三区信息提炼
            account_country = (rg.get('account_country') or rg.get('country') or '未知').upper()
            rewards_market = (rg.get('rewards_market') or rg.get('country') or '未知').upper()
            proxy_geo = geo.get_cached_proxy_geo(name, a.get('proxy'))

            # 漫游风控诊断
            diag_code = 'unknown'
            diag_text = '⚪ 待探测'
            diag_desc = '尚未获取出口 IP 地理信息'
            if proxy_geo and proxy_geo.get('status') == 'ok':
                p_c = (proxy_geo.get('country') or '').upper()
                if rewards_market != '未知' and p_c == rewards_market:
                    diag_code = 'matched'
                    diag_text = '🟢 专线一致'
                    diag_desc = f'代理出口({p_c})与Rewards市场({rewards_market})一致，无异地漫游风险'
                elif rewards_market != '未知':
                    diag_code = 'roaming'
                    diag_text = '⚠️ 异地漫游'
                    diag_desc = f'代理出口({p_c})与Rewards市场({rewards_market})不同，可能触发“正在路上”漫游锁分'
                else:
                    diag_code = 'matched'
                    diag_text = '🟢 代理在线'
                    diag_desc = f'出口地区: {proxy_geo.get("country_name", p_c)}'
            elif proxy_geo and proxy_geo.get('status') == 'error':
                diag_code = 'error'
                diag_text = '🔴 代理失效'
                diag_desc = proxy_geo.get('message', '连接失败')
            elif not a.get('proxy'):
                diag_code = 'direct'
                diag_text = '🌐 本机直连'
                diag_desc = '未配置代理，使用本机网络出口'

            detailed.append({
                'name': name,
                'enabled': a.get('enabled', True),
                'proxy': a.get('proxy'),
                'offset_minutes': a.get('offset_minutes', 0),
                'country': rg.get('country'),
                'account_country': account_country,
                'rewards_market': rewards_market,
                'proxy_geo': proxy_geo,
                'roaming_diagnosis': {
                    'code': diag_code,
                    'text': diag_text,
                    'desc': diag_desc
                },
                'language': rg.get('language'),
                'balance': bal,
                'last_run': st.get('last_run'),
                'last_results': st.get('last_results', []),
                'auth': auth_info,
                'history_7d': h7,
                'fingerprint': fp,
                'fingerprint_summary': fp_summary
            })
        from msr import settings
        st_cfg = settings.load_settings()
        sch = st_cfg.get('schedule', {})
        scheduler_info = {
            'daily_enabled': sch.get('daily_enabled', True),
            'daily_time': sch.get('daily_time', '01:30'),
            'status': '运行中' if sch.get('daily_enabled', True) else '已暂停'
        }
        self._json_resp({'accounts': detailed, 'scheduler': scheduler_info, 'time': time.strftime('%F %T')})

    def _handle_credential_login(self, payload):
        from msr import auth, config
        name = payload.get('name')
        username = payload.get('username')
        password = payload.get('password')
        if not name or not username or not password:
            self._json_resp({'status': 'error', 'message': '账号、用户名和密码不能为空'}, code=400)
            return

        acc_cfg = config.get_account(name) or {}
        proxy = acc_cfg.get('proxy')
        try:
            append_live_log(f'[{name}] 开始微软账号全自动登录 (账号: {username})...')
            res = auth.login_with_credential(name, username, password, proxy=proxy,
                                             log_cb=lambda m: append_live_log(f'[{name}] {m}'))
            self._json_resp(res)
        except Exception as ex:
            append_live_log(f'[{name}] 登录失败: {ex}')
            self._json_resp({'status': 'error', 'message': str(ex)}, code=500)

    def _handle_cookies_import(self, payload):
        from msr import config, cookies, auth
        name = payload.get('name')
        raw = payload.get('cookies_raw', '')
        try:
            cks = cookies.import_content(raw)
            if not cks:
                self._json_resp({'status': 'error', 'message': '无法识别 Cookie 内容，请确保是合法的 JSON 数组或键值对字符串'}, code=400)
                return
            diag = cookies.validate_cookies(cks)
            config.save_json(config.pfile(name, 'cookie.json'), cks)
            append_live_log(f'[{name}] 导入了 {len(cks)} 条 Cookie (含 _U={diag["has_auth_u"]})')

            acc_cfg = config.get_account(name) or {}
            proxy = acc_cfg.get('proxy')
            append_live_log(f'[{name}] 正在基于导入的会话自动尝试换取 OAuth2 Token...')
            tok = auth.exchange_token_from_cookies(name, proxy=proxy, log_cb=lambda m: append_live_log(f'[{name}] {m}'))

            has_tok = bool(tok)
            self._json_resp({'status': 'ok', 'count': len(cks), 'has_u': diag['has_auth_u'], 'has_token': has_tok})
        except Exception as ex:
            self._json_resp({'status': 'error', 'message': str(ex)}, code=500)

    def _handle_run(self, payload, u):
        import subprocess
        name = payload.get('name') or urllib.parse.parse_qs(u.query).get('name', [None])[0]
        dry = 'dry_run' in u.query or payload.get('dry_run')

        if dry:
            cmd = [sys.executable, '-m', 'msr', 'run', name or '--all', '--dry-run']
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            self._json_resp({'status': 'ok', 'output': p.stdout})
            return

        def bg_runner():
            append_live_log(f'>>> 开始执行任务: {name or "--all"}')
            cmd = [sys.executable, '-m', 'msr', 'run', name or '--all']
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in iter(proc.stdout.readline, ''):
                line = line.strip()
                if line:
                    append_live_log(line)
            proc.stdout.close()
            proc.wait()
            append_live_log(f'<<< 任务执行完毕 (exit={proc.returncode})')

        t = threading.Thread(target=bg_runner)
        t.daemon = True
        t.start()
        self._json_resp({'status': 'ok', 'message': '后台执行进程已启动'})

    def _handle_logs(self):
        from msr import config
        lines = []
        with LIVE_LOGS_LOCK:
            if LIVE_LOGS:
                lines.append('=== 实时内存日志 (最新) ===')
                lines.extend(LIVE_LOGS[-200:])
                lines.append('')

        today = time.strftime('%Y-%m-%d')
        for acc in config.accounts():
            name = acc['name']
            log_f = os.path.join(config.pfile(name, 'logs'), f'{today}.log')
            if os.path.isfile(log_f):
                lines.append(f'=== 账号 [{name}] 今日落盘日志 ===')
                try:
                    with open(log_f, 'r', encoding='utf-8', errors='ignore') as f:
                        lines.extend(f.readlines()[-60:])
                except Exception:
                    pass
                lines.append('')

        text = '\n'.join(lines) if lines else '今日暂无执行日志。'
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_cors()
        self.end_headers()
        self.wfile.write(text.encode('utf-8'))

    def _json_resp(self, data, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_cors()
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))


def _start_scheduler_daemon():
    """轻量级内置每日定时调度引擎。

    即便在无 crontab 的裸机环境(Windows/macOS/无cron的Linux)，也能保证每天到达配置的时间(如 01:30)
    自动唤醒全账号串行执行，执行完毕后彻底杀除 Chromium 并释放内存。
    """
    last_trigger_date = None

    def scheduler_loop():
        nonlocal last_trigger_date
        import subprocess
        from msr import settings
        while True:
            try:
                time.sleep(20)
                st = settings.load_settings()
                sch = st.get('schedule', {})
                if not sch.get('daily_enabled', True):
                    continue

                daily_time = sch.get('daily_time', '01:30')
                now_hm = time.strftime('%H:%M')
                today_str = time.strftime('%Y-%m-%d')

                if now_hm == daily_time and last_trigger_date != today_str:
                    last_trigger_date = today_str
                    append_live_log(f'⏰ [定时调度] 到达每日预定时间 {daily_time}，正在自动唤醒全账号任务流程...')

                    jitter_max = int(sch.get('random_start_delay_max', 0))
                    if jitter_max > 0 and not sch.get('fast_mode'):
                        jitter_sec = random.uniform(0, jitter_max * 60)
                        append_live_log(f'⏰ [定时调度] 开启随机抖动，等待 {jitter_sec / 60:.1f} 分钟后开始执行...')
                        time.sleep(jitter_sec)

                    cmd = [sys.executable, '-m', 'msr', 'run', '--all']
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                    for line in iter(proc.stdout.readline, ''):
                        line = line.strip()
                        if line:
                            append_live_log(line)
                    proc.stdout.close()
                    proc.wait()
                    append_live_log(f'⏰ [定时调度] 全账号任务已完成 (exit={proc.returncode})，浏览器已完全退出并释放系统内存。下次将在明日 {daily_time} 自动执行。')
            except Exception as ex:
                append_live_log(f'⏰ [定时调度异常]: {ex}')

    t = threading.Thread(target=scheduler_loop, daemon=True, name='DailyScheduler')
    t.start()


def run_web(host='0.0.0.0', port=8080):
    """启动轻量级 Web 控制台服务器。"""
    addr = (host, int(port))
    server = HTTPServer(addr, WebHandler)
    _start_scheduler_daemon()
    print(f'MSR-ArmBot Web 控制台已启动: http://{host}:{port}', flush=True)
    append_live_log(f'Web 控制台服务已启动于 http://{host}:{port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nWeb 控制台正在关闭...', flush=True)
        server.shutdown()


start_server = run_web
