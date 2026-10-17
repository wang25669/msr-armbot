"""现代化轻量级 Web 管理控制台与 RESTful API。

纯 Python 标准库实现 (基于 http.server)，零第三方 pip 依赖。
功能矩阵:
1. 账号全景仪表盘: 实时积分余额、区域、今日增量、代理配置、上次结果与运行状态
2. 账号管理: 在线添加/编辑账号，独立代理(HTTP/SOCKS5)配置，一键启用/禁用/删除
3. 网页授权中心:
   - 方式 A: 微软官方设备码授权 (生成 Device Code，展示授权链接与验证码，后台自动轮询入库)
   - 方式 B: 浏览器 Cookie 快速粘贴 (支持 Cookie-Editor 导出的 JSON 或 Raw Header，在线校验 _U 凭据)
4. 集中风控与节奏参数配置:
   - 搜索单次停留时间、冷却熔断重试次数
   - 卡片归因停留时间
   - Server酱 (Turbo/老版) SendKey、通道与一键推送测试
   - 极速调试模式 (fast_mode) 开关
5. 任务在线调度:
   - 单账号立即运行 / 排期预览 (dry-run)
   - 全部账号批量运行
   - 实时日志滚屏捕获与自动刷新
"""
import copy
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

# 内存日志缓冲区，用于 Web 实时滚屏
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
    .container { max-width: 1200px; margin: 0 auto; }
    header { display: flex; justify-content: space-between; align-items: center; padding-bottom: 20px; margin-bottom: 24px; border-bottom: 1px solid var(--border); }
    .logo-area { display: flex; align-items: center; gap: 12px; }
    .logo-title { font-size: 24px; font-weight: 800; color: #fff; letter-spacing: -0.5px; }
    .logo-tag { background: linear-gradient(135deg, #0284c7, #38bdf8); color: #fff; font-size: 12px; padding: 3px 8px; border-radius: 4px; font-weight: 700; }
    .nav-tabs { display: flex; gap: 8px; margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }
    .tab-btn { background: none; border: 1px solid transparent; color: var(--text-sub); font-size: 15px; font-weight: 600; padding: 8px 18px; cursor: pointer; border-radius: 8px; transition: all 0.2s; }
    .tab-btn:hover { color: #fff; background: rgba(255,255,255,0.05); }
    .tab-btn.active { background: var(--card); color: var(--primary); border-color: var(--border); }
    .tab-content { display: none; }
    .tab-content.active { display: block; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 22px; margin-bottom: 24px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.2); }
    .card-title { font-size: 17px; font-weight: 700; margin-bottom: 18px; display: flex; justify-content: space-between; align-items: center; color: #fff; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 18px; }
    .acc-card { background: rgba(15, 23, 42, 0.7); border: 1px solid var(--border); border-radius: 10px; padding: 18px; transition: transform 0.15s, border-color 0.15s; }
    .acc-card:hover { border-color: #475569; transform: translateY(-2px); }
    .acc-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
    .acc-name { font-size: 18px; font-weight: 700; color: #fff; }
    .badge { font-size: 12px; padding: 3px 8px; border-radius: 4px; font-weight: 600; }
    .badge-success { background: rgba(34, 197, 94, 0.15); color: var(--success); border: 1px solid rgba(34, 197, 94, 0.3); }
    .badge-disabled { background: rgba(239, 68, 68, 0.15); color: var(--danger); border: 1px solid rgba(239, 68, 68, 0.3); }
    .badge-info { background: rgba(56, 189, 248, 0.15); color: var(--primary); border: 1px solid rgba(56, 189, 248, 0.3); }
    .acc-meta { font-size: 13px; color: var(--text-sub); line-height: 1.9; }
    .acc-meta strong { color: #f1f5f9; }
    .acc-meta code { background: rgba(0,0,0,0.3); padding: 2px 5px; border-radius: 4px; font-size: 12px; color: #38bdf8; word-break: break-all; }
    .btn { background: var(--primary); color: #0f172a; border: none; padding: 9px 18px; border-radius: 6px; font-weight: 600; cursor: pointer; transition: 0.15s; font-size: 14px; display: inline-flex; align-items: center; gap: 6px; }
    .btn:hover { background: var(--primary-hover); }
    .btn-secondary { background: #334155; color: #f8fafc; }
    .btn-secondary:hover { background: #475569; }
    .btn-danger { background: rgba(239, 68, 68, 0.2); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.4); }
    .btn-danger:hover { background: var(--danger); color: #fff; }
    .btn-sm { padding: 5px 12px; font-size: 12px; border-radius: 5px; }
    .form-group { margin-bottom: 16px; }
    label { display: block; font-size: 13px; font-weight: 600; color: var(--text-sub); margin-bottom: 6px; }
    input, select, textarea { width: 100%; background: #0f172a; border: 1px solid var(--border); color: #fff; padding: 10px 14px; border-radius: 6px; font-size: 14px; }
    input:focus, textarea:focus { border-color: var(--primary); outline: none; }
    .flex-row { display: flex; gap: 10px; align-items: center; }
    pre { background: #070d19; border: 1px solid var(--border); border-radius: 8px; padding: 14px; font-family: "JetBrains Mono", Consolas, monospace; font-size: 12px; color: #94a3b8; max-height: 480px; overflow-y: auto; white-space: pre-wrap; line-height: 1.6; }
    .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.75); display: none; justify-content: center; align-items: center; z-index: 999; backdrop-filter: blur(4px); }
    .modal { background: var(--card); border: 1px solid var(--border); border-radius: 12px; width: 95%; max-width: 580px; padding: 24px; max-height: 90vh; overflow-y: auto; }
    .toast { position: fixed; bottom: 24px; right: 24px; background: #1e293b; border: 1px solid var(--primary); color: #fff; padding: 12px 22px; border-radius: 8px; font-size: 14px; display: none; z-index: 1000; box-shadow: 0 10px 25px rgba(0,0,0,0.6); }
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
        <div class="grid" style="grid-template-columns: 1fr 1.5fr 1fr auto; align-items: flex-end;">
          <div class="form-group">
            <label>账号名称 (唯一标识)</label>
            <input type="text" id="acc-name-input" placeholder="例如: sg_user, cn_main">
          </div>
          <div class="form-group">
            <label>独立出口代理 (支持 HTTP / SOCKS5，留空直连)</label>
            <input type="text" id="acc-proxy-input" placeholder="http://user:pass@127.0.0.1:7890">
          </div>
          <div class="form-group">
            <label>调度偏移 (分钟)</label>
            <input type="number" id="acc-offset-input" value="0">
          </div>
          <div class="form-group">
            <button class="btn" onclick="saveNewAccount()">保存账号</button>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">账号列表与授权管理</div>
        <div id="acc-table-list">加载中...</div>
      </div>
    </div>

    <!-- 参数配置 -->
    <div id="tab-settings" class="tab-content">
      <div class="card">
        <div class="card-title">
          <span>全局节奏与风控配置</span>
          <button class="btn" onclick="submitSettings()">保存全部配置</button>
        </div>

        <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 24px;">
          <div>
            <h4 style="color:var(--primary); margin-bottom:14px;">🔍 搜索节奏设置 (防冷却冷板)</h4>
            <div class="form-group">
              <label>单次搜索随机停留最小秒数 (默认 180s = 3分钟防风控)</label>
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
              <label>搜索总超时预算 (分钟)</label>
              <input type="number" id="s-budget-min">
            </div>
          </div>

          <div>
            <h4 style="color:var(--primary); margin-bottom:14px;">📢 Server酱微信推送配置</h4>
            <div class="form-group">
              <label>Server酱 SendKey (Turbo 版 SCT 或旧版 SCU)</label>
              <input type="text" id="sc-key" placeholder="例如: SCTxxxxxxxx">
            </div>
            <div class="form-group">
              <label>自定义分流通道 channel (选填，如 9=钉钉群, 18=企业微信)</label>
              <input type="text" id="sc-channel" placeholder="留空默认微信模板消息">
            </div>
            <div class="form-group">
              <label>极速测试模式 (fast_mode, 开启后等待压缩至 3-6 秒)</label>
              <select id="cfg-fast-mode">
                <option value="false">关闭 (真实真人节奏，约2小时/号)</option>
                <option value="true">开启 (极速调试，仅测试流程连通性)</option>
              </select>
            </div>
            <div class="form-group" style="margin-top:20px;">
              <button class="btn btn-secondary btn-sm" onclick="sendTestPush()">测试发送通知</button>
            </div>
          </div>
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

  <!-- 登录授权弹窗 -->
  <div id="login-modal" class="modal-overlay">
    <div class="modal">
      <div class="card-title">
        <span id="modal-title">账号授权登录</span>
        <button class="btn btn-secondary btn-sm" onclick="closeLoginModal()">关闭</button>
      </div>

      <div class="nav-tabs" style="margin-bottom:16px;">
        <button id="modal-tab-dc" class="tab-btn active" onclick="switchModalTab('dc')">设备码免密授权</button>
        <button id="modal-tab-ck" class="tab-btn" onclick="switchModalTab('ck')">Cookie 快速粘贴</button>
      </div>

      <!-- 设备码授权页面 -->
      <div id="modal-panel-dc">
        <p style="font-size:13px; color:var(--text-sub); margin-bottom:12px;">
          官方 OAuth2 设备流：点击下方按钮生成设备码，手机或电脑浏览器打开网址输入代码即可完成授权，支持 2FA 与免密。
        </p>
        <div style="text-align:center; padding:16px; background:#070d19; border-radius:8px; margin-bottom:14px;">
          <div id="dc-code-display" style="font-size:28px; font-weight:800; color:var(--primary); letter-spacing:4px;">------</div>
          <div id="dc-status-text" style="font-size:13px; color:var(--text-sub); margin-top:8px;">等待获取代码...</div>
        </div>
        <div class="flex-row" style="justify-content:center;">
          <button id="dc-gen-btn" class="btn" onclick="startDeviceCodeFlow()">获取授权代码</button>
          <a id="dc-open-link" href="#" target="_blank" class="btn btn-secondary" style="display:none;">打开微软授权页面</a>
        </div>
      </div>

      <!-- Cookie 粘贴页面 -->
      <div id="modal-panel-ck" style="display:none;">
        <p style="font-size:13px; color:var(--text-sub); margin-bottom:10px;">
          粘贴 Cookie-Editor 导出的 JSON 数组或请求头 Raw Cookie (name=val; ...):
        </p>
        <textarea id="ck-text-input" rows="8" placeholder='[{"name":"_U","value":"..."}, ...]'></textarea>
        <div style="margin-top:12px; display:flex; justify-content:space-between; align-items:center;">
          <span id="ck-result-msg" style="font-size:13px; color:var(--text-sub);"></span>
          <button class="btn" onclick="submitCookies()">导入并验证 Cookie</button>
        </div>
      </div>
    </div>
  </div>

  <div id="toast" class="toast"></div>

  <script>
    let activeAccountName = '';
    let dcPollTimer = null;
    let logPollTimer = null;

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
        box.innerHTML = `
          <div class="acc-header">
            <span class="acc-name">${a.name}</span>
            <span class="badge ${a.enabled ? 'badge-success' : 'badge-disabled'}">${a.enabled ? '已启用' : '已停用'}</span>
          </div>
          <div class="acc-meta">
            <div>🌍 区域: <strong>${a.country || '待探测'}</strong> (${a.language || '-'})</div>
            <div>💰 当前积分: <strong style="color:var(--primary); font-size:16px;">${a.balance || 0}</strong></div>
            <div>🌐 代理: <code>${a.proxy || '直连'}</code></div>
            <div>⏱️ 上次运行: ${a.last_run || '从未'}</div>
            <div style="margin-top:6px; font-size:12px; color:var(--text-sub);">
              近期结果: <span style="color:#cbd5e1">${resStr}</span>
            </div>
          </div>
          <div style="margin-top:14px; display:flex; gap:8px;">
            <button class="btn btn-sm" onclick="runSingle('${a.name}')">立即运行</button>
            <button class="btn btn-secondary btn-sm" onclick="openLoginModal('${a.name}')">登录/Cookie</button>
            <button class="btn btn-secondary btn-sm" onclick="previewDryRun('${a.name}')">排期预览</button>
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
        const row = document.createElement('div');
        row.style = 'display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--border); padding:12px 0;';
        row.innerHTML = `
          <div>
            <strong style="font-size:15px; color:#fff;">${a.name}</strong>
            <span style="color:var(--text-sub); font-size:13px; margin-left:14px;">
              代理: <code>${a.proxy || '直连'}</code> | 偏移: ${a.offset_minutes || 0}分钟
            </span>
          </div>
          <div class="flex-row">
            <button class="btn btn-secondary btn-sm" onclick="openLoginModal('${a.name}')">授权/Cookie</button>
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

    async function previewDryRun(name) {
      toast('正在探测生成排期计划...');
      const res = await fetch(`/api/run?dry_run=1&name=${encodeURIComponent(name)}`, { method: 'POST' });
      const d = await res.json();
      alert(`[${name}] 执行排期计划预览:\n\n` + (d.output || '无输出'));
    }

    async function loadSettingsForm() {
      try {
        const res = await fetch('/api/settings');
        const s = await res.json();
        document.getElementById('s-dwell-min').value = s.search?.dwell_min_seconds || 180;
        document.getElementById('s-dwell-max').value = s.search?.dwell_max_seconds || 300;
        document.getElementById('s-stall-max').value = s.search?.stall_max_retries || 12;
        document.getElementById('s-budget-min').value = s.search?.budget_minutes || 120;
        document.getElementById('sc-key').value = s.notify?.serverchan?.sendkey || '';
        document.getElementById('sc-channel').value = s.notify?.serverchan?.channel || '';
        document.getElementById('cfg-fast-mode').value = String(!!s.schedule?.fast_mode);
      } catch(err) {
        toast('读取配置失败: ' + err);
      }
    }

    async function submitSettings() {
      const body = {
        search: {
          dwell_min_seconds: parseInt(document.getElementById('s-dwell-min').value),
          dwell_max_seconds: parseInt(document.getElementById('s-dwell-max').value),
          stall_max_retries: parseInt(document.getElementById('s-stall-max').value),
          budget_minutes: parseInt(document.getElementById('s-budget-min').value)
        },
        schedule: {
          fast_mode: document.getElementById('cfg-fast-mode').value === 'true'
        },
        notify: {
          channels: document.getElementById('sc-key').value ? ['stdout', 'serverchan'] : ['stdout'],
          serverchan: {
            sendkey: document.getElementById('sc-key').value.trim(),
            channel: document.getElementById('sc-channel').value.trim()
          }
        }
      };

      await fetch('/api/settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
      });
      toast('风控与通知参数已持久化保存');
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

    /* 登录弹窗逻辑 */
    function openLoginModal(name) {
      activeAccountName = name;
      document.getElementById('modal-title').innerText = `账号授权登录: [${name}]`;
      document.getElementById('dc-code-display').innerText = '------';
      document.getElementById('dc-status-text').innerText = '点击下方按钮生成授权代码';
      document.getElementById('dc-open-link').style.display = 'none';
      document.getElementById('ck-text-input').value = '';
      document.getElementById('ck-result-msg').innerText = '';
      document.getElementById('login-modal').style.display = 'flex';
      switchModalTab('dc');
    }

    function closeLoginModal() {
      if (dcPollTimer) clearInterval(dcPollTimer);
      document.getElementById('login-modal').style.display = 'none';
      fetchStatus();
    }

    function switchModalTab(tab) {
      document.getElementById('modal-tab-dc').classList.toggle('active', tab === 'dc');
      document.getElementById('modal-tab-ck').classList.toggle('active', tab === 'ck');
      document.getElementById('modal-panel-dc').style.display = tab === 'dc' ? 'block' : 'none';
      document.getElementById('modal-panel-ck').style.display = tab === 'ck' ? 'block' : 'none';
    }

    async function startDeviceCodeFlow() {
      if (!activeAccountName) return;
      document.getElementById('dc-status-text').innerText = '正在向微软官方请求设备代码...';
      try {
        const res = await fetch('/api/auth/device-code', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: activeAccountName})
        });
        const d = await res.json();
        if (d.status !== 'ok') throw new Error(d.message || '请求失败');

        const uri = d.data.verification_uri || 'https://microsoft.com/devicelogin';
        const code = d.data.user_code;
        document.getElementById('dc-code-display').innerText = code;
        document.getElementById('dc-status-text').innerHTML = `请打开链接并输入代码授权。等待授权中...`;
        const linkEl = document.getElementById('dc-open-link');
        linkEl.href = uri;
        linkEl.style.display = 'inline-flex';

        // 启动后台轮询
        if (dcPollTimer) clearInterval(dcPollTimer);
        const intervalSec = Math.max(5, d.data.interval || 5);
        dcPollTimer = setInterval(async () => {
          const pollRes = await fetch('/api/auth/device-code/poll', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name: activeAccountName, device_code: d.data.device_code})
          });
          const pollData = await pollRes.json();
          if (pollData.complete) {
            clearInterval(dcPollTimer);
            document.getElementById('dc-status-text').innerHTML = `<strong style="color:var(--success)">🎉 授权成功！Token 已持久化保存。</strong>`;
            toast('设备码授权成功');
            fetchStatus();
          }
        }, intervalSec * 1000);

      } catch(err) {
        document.getElementById('dc-status-text').innerText = '获取失败: ' + err;
      }
    }

    async function submitCookies() {
      const content = document.getElementById('ck-text-input').value.trim();
      if (!content) return toast('请输入 Cookie 内容');
      const res = await fetch('/api/auth/cookies', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: activeAccountName, cookies_raw: content})
      });
      const d = await res.json();
      if (d.status === 'ok') {
        document.getElementById('ck-result-msg').innerHTML = `<span style="color:var(--success)">✅ 导入成功 (${d.count}条, 含_U=${d.has_u})</span>`;
        toast('Cookie 导入成功');
        fetchStatus();
      } else {
        document.getElementById('ck-result-msg').innerHTML = `<span style="color:var(--danger)">❌ 校验失败: ${d.message}</span>`;
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
            append_live_log('Web 控制台更新了全局风控配置')
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

        if path == '/api/auth/device-code':
            self._handle_device_code_start(payload)
            return

        if path == '/api/auth/device-code/poll':
            self._handle_device_code_poll(payload)
            return

        if path == '/api/auth/cookies':
            self._handle_cookies_import(payload)
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
        from msr import config
        accs = config.accounts()
        detailed = []
        for a in accs:
            name = a['name']
            st = config.load_json(config.pfile(name, 'state.json'), {}) or {}
            rg = config.load_json(config.pfile(name, 'region.json'), {}) or {}
            detailed.append({
                'name': name,
                'enabled': a.get('enabled', True),
                'proxy': a.get('proxy'),
                'offset_minutes': a.get('offset_minutes', 0),
                'country': rg.get('country'),
                'language': rg.get('language'),
                'balance': st.get('balance', rg.get('balance', 0)),
                'last_run': st.get('last_run'),
                'last_results': st.get('last_results', [])
            })
        self._json_resp({'accounts': detailed, 'time': time.strftime('%F %T')})

    def _handle_device_code_start(self, payload):
        from msr import api, config
        name = payload.get('name')
        acc_cfg = config.get_account(name) or {}
        proxy = acc_cfg.get('proxy')
        try:
            dc = api.device_code_login(name, proxy=proxy)
            append_live_log(f'[{name}] 生成设备码成功: {dc.get("user_code")}')
            self._json_resp({'status': 'ok', 'data': dc})
        except Exception as ex:
            self._json_resp({'status': 'error', 'message': str(ex)}, code=500)

    def _handle_device_code_poll(self, payload):
        from msr import api, config, probe
        name = payload.get('name')
        device_code = payload.get('device_code')
        acc_cfg = config.get_account(name) or {}
        proxy = acc_cfg.get('proxy')
        p = config.profile(name)
        try:
            tok = api.poll_device_code_once(name, device_code, proxy=proxy)
            if tok:
                api.save_token(name, tok, p)
                pr = probe.probe_account(name, tok['access_token'], proxy=proxy)
                config.save_json(config.pfile(name, 'region.json'), pr)
                append_live_log(f'[{name}] 设备码授权成功，已保存 Token 及探测结果 (国家: {pr.get("country")})')
                self._json_resp({'status': 'ok', 'complete': True})
            else:
                self._json_resp({'status': 'ok', 'complete': False})
        except Exception as ex:
            self._json_resp({'status': 'error', 'message': str(ex)}, code=500)

    def _handle_cookies_import(self, payload):
        from msr import config, cookies
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
            self._json_resp({'status': 'ok', 'count': len(cks), 'has_u': diag['has_auth_u']})
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

    def log_message(self, format, *args):
        pass


def start_server(host='0.0.0.0', port=8080):
    from msr import config
    config.init()
    server = HTTPServer((host, port), WebHandler)
    append_live_log(f'Web 控制台服务已启动 (端口: {port})')
    print(f'MSR-ArmBot Web 控制台已启动: http://{host}:{port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nWeb 控制台已关闭')
        server.server_close()
