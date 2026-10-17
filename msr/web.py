"""轻量级内置 Web 管理控制台与 RESTful API。

纯 Python 标准库实现 (基于 http.server)，零第三方 pip 依赖。
提供响应式现代化 Web 界面:
1. 账号状态与积分仪表盘 (实时余额、今日增量、区域与代理)
2. 账号管理 (添加/修改、独立代理出口配置、开关切换)
3. 集中参数可视化配置 (搜索停留时间、卡片延迟、Server酱 SendKey、极速模式切换)
4. 任务手动触发与测试 (单账号/全量运行、dry-run、推送测试)
5. 实时日志查看
"""
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MSR-ArmBot 控制中心</title>
  <style>
    :root {
      --bg: #0f172a; --card-bg: #1e293b; --text: #f8fafc; --text-sub: #94a3b8;
      --primary: #38bdf8; --primary-hover: #0ea5e9; --success: #22c55e;
      --warn: #eab308; --danger: #ef4444; --border: #334155;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    body { background: var(--bg); color: var(--text); padding: 24px; min-height: 100vh; }
    .container { max-width: 1200px; margin: 0 auto; }
    header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 16px; }
    h1 { font-size: 24px; font-weight: 700; color: var(--primary); display: flex; align-items: center; gap: 8px; }
    .badge { font-size: 12px; background: rgba(56, 189, 248, 0.2); color: var(--primary); padding: 4px 8px; border-radius: 4px; }
    .nav-tabs { display: flex; gap: 12px; margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }
    .tab-btn { background: none; border: none; color: var(--text-sub); font-size: 15px; font-weight: 600; padding: 8px 16px; cursor: pointer; border-radius: 6px; }
    .tab-btn.active { background: var(--card-bg); color: var(--primary); border: 1px solid var(--border); }
    .tab-content { display: none; }
    .tab-content.active { display: block; }
    .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 20px; }
    .card-title { font-size: 17px; font-weight: 600; margin-bottom: 16px; display: flex; justify-content: space-between; align-items: center; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
    .acc-box { background: rgba(15, 23, 42, 0.6); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
    .acc-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
    .acc-name { font-size: 18px; font-weight: 700; color: #fff; }
    .acc-meta { font-size: 13px; color: var(--text-sub); line-height: 1.8; }
    .btn { background: var(--primary); color: #0f172a; border: none; padding: 8px 16px; border-radius: 6px; font-weight: 600; cursor: pointer; transition: 0.2s; font-size: 14px; }
    .btn:hover { background: var(--primary-hover); }
    .btn-secondary { background: #334155; color: #fff; }
    .btn-secondary:hover { background: #475569; }
    .btn-danger { background: var(--danger); color: #fff; }
    .btn-sm { padding: 4px 10px; font-size: 12px; }
    .form-group { margin-bottom: 16px; }
    label { display: block; font-size: 13px; font-weight: 600; color: var(--text-sub); margin-bottom: 6px; }
    input, select, textarea { width: 100%; background: #0f172a; border: 1px solid var(--border); color: #fff; padding: 10px 12px; border-radius: 6px; font-size: 14px; }
    input:focus, textarea:focus { border-color: var(--primary); outline: none; }
    .flex-row { display: flex; gap: 12px; align-items: center; }
    pre { background: #0b1120; border: 1px solid var(--border); border-radius: 6px; padding: 12px; font-family: monospace; font-size: 12px; color: #cbd5e1; max-height: 400px; overflow-y: auto; white-space: pre-wrap; }
    .toast { position: fixed; bottom: 20px; right: 20px; background: var(--card-bg); border: 1px solid var(--primary); padding: 12px 20px; border-radius: 8px; font-size: 14px; display: none; z-index: 1000; box-shadow: 0 4px 12px rgba(0,0,0,0.5); }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>MSR-ArmBot <span class="badge">控制中心</span></h1>
      <div class="flex-row">
        <button class="btn btn-secondary" onclick="fetchStatus()">刷新状态</button>
        <button class="btn" onclick="triggerRunAll()">执行全部账号</button>
      </div>
    </header>

    <div class="nav-tabs">
      <button class="tab-btn active" onclick="switchTab('dashboard')">仪表盘</button>
      <button class="tab-btn" onclick="switchTab('accounts')">账号管理</button>
      <button class="tab-btn" onclick="switchTab('settings')">参数配置</button>
      <button class="tab-btn" onclick="switchTab('logs')">运行日志</button>
    </div>

    <!-- 仪表盘 -->
    <div id="tab-dashboard" class="tab-content active">
      <div class="card">
        <div class="card-title">
          <span>账号概览</span>
          <span id="summary-meta" style="font-size:13px; color:var(--text-sub)"></span>
        </div>
        <div id="acc-grid" class="grid">加载中...</div>
      </div>
    </div>

    <!-- 账号管理 -->
    <div id="tab-accounts" class="tab-content">
      <div class="card">
        <div class="card-title">添加 / 编辑账号</div>
        <div class="grid" style="grid-template-columns: 1fr 1fr 1fr auto; align-items: flex-end;">
          <div class="form-group">
            <label>账号名称 (标识)</label>
            <input type="text" id="new-acc-name" placeholder="例如: sg, cn, us_main">
          </div>
          <div class="form-group">
            <label>独立出口代理 (支持 HTTP/SOCKS5，空为直连)</label>
            <input type="text" id="new-acc-proxy" placeholder="http://127.0.0.1:7890">
          </div>
          <div class="form-group">
            <label>调度偏移 (分钟)</label>
            <input type="number" id="new-acc-offset" value="0">
          </div>
          <div class="form-group">
            <button class="btn" onclick="saveAccount()">保存账号</button>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-title">已有账号配置列表</div>
        <div id="acc-manage-list"></div>
      </div>
    </div>

    <!-- 参数配置 -->
    <div id="tab-settings" class="tab-content">
      <div class="card">
        <div class="card-title">
          <span>集中节奏与风控参数</span>
          <button class="btn" onclick="saveSettings()">保存全部配置</button>
        </div>
        <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 20px;">
          <div>
            <h4 style="color:var(--primary); margin-bottom:12px;">搜索任务节奏</h4>
            <div class="form-group">
              <label>单次搜索随机停留最小秒数 (默认 180s = 3分钟防冷却)</label>
              <input type="number" id="cfg-search-min">
            </div>
            <div class="form-group">
              <label>单次搜索随机停留最大秒数 (默认 300s = 5分钟)</label>
              <input type="number" id="cfg-search-max">
            </div>
            <div class="form-group">
              <label>搜索不涨分连续重试上限 (stall_max_retries)</label>
              <input type="number" id="cfg-search-stall">
            </div>
          </div>

          <div>
            <h4 style="color:var(--primary); margin-bottom:12px;">卡片与通知</h4>
            <div class="form-group">
              <label>卡片归因停留最小秒数 (attribution_wait_min)</label>
              <input type="number" id="cfg-card-min">
            </div>
            <div class="form-group">
              <label>Server酱 SendKey (Turbo 版 SCT / 旧版 SCU)</label>
              <input type="text" id="cfg-sc-key" placeholder="SCTxxxxxxxx">
            </div>
            <div class="form-group">
              <label>Server酱 自定义通道 (选填)</label>
              <input type="text" id="cfg-sc-channel" placeholder="例如: 9">
            </div>
            <div class="form-group">
              <button class="btn btn-secondary btn-sm" onclick="testNotify()">测试发送通知</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 运行日志 -->
    <div id="tab-logs" class="tab-content">
      <div class="card">
        <div class="card-title">
          <span>实时日志输出</span>
          <button class="btn btn-secondary btn-sm" onclick="fetchLogs()">刷新日志</button>
        </div>
        <pre id="log-output">加载中...</pre>
      </div>
    </div>
  </div>

  <div id="toast" class="toast"></div>

  <script>
    function showToast(msg) {
      const t = document.getElementById('toast');
      t.innerText = msg;
      t.style.display = 'block';
      setTimeout(() => { t.style.display = 'none'; }, 3000);
    }

    function switchTab(tab) {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      event.target.classList.add('active');
      document.getElementById('tab-' + tab).classList.add('active');
      if (tab === 'logs') fetchLogs();
      if (tab === 'settings') loadSettings();
    }

    async function fetchStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        renderDashboard(data);
        renderManageList(data.accounts);
      } catch(e) {
        showToast('获取状态失败: ' + e);
      }
    }

    function renderDashboard(data) {
      const grid = document.getElementById('acc-grid');
      grid.innerHTML = '';
      if (!data.accounts || data.accounts.length === 0) {
        grid.innerHTML = '<div style="color:var(--text-sub)">暂无账号，请在“账号管理”中添加。</div>';
        return;
      }
      data.accounts.forEach(a => {
        const box = document.createElement('div');
        box.className = 'acc-box';
        box.innerHTML = `
          <div class="acc-header">
            <span class="acc-name">${a.name}</span>
            <span class="badge" style="${a.enabled ? '' : 'background:rgba(239,68,68,0.2);color:var(--danger)'}">
              ${a.enabled ? '启用中' : '已停用'}
            </span>
          </div>
          <div class="acc-meta">
            <div>🌍 区域: <strong>${a.country || '待探测'}</strong> (${a.language || '-'})</div>
            <div>💰 积分余额: <strong style="color:var(--primary); font-size:16px;">${a.balance || 0}</strong></div>
            <div>🌐 代理: <code>${a.proxy || '直连'}</code></div>
            <div>⏱️ 上次运行: ${a.last_run || '从未'}</div>
            <div style="font-size:12px; color:var(--text-sub); margin-top:4px;">
              结果: ${a.last_results && a.last_results.length ? a.last_results.join(' | ') : '无'}
            </div>
          </div>
          <div style="margin-top:12px; display:flex; gap:8px;">
            <button class="btn btn-sm" onclick="runAccount('${a.name}')">立即运行</button>
            <button class="btn btn-secondary btn-sm" onclick="runAccountDry('${a.name}')">排期预览</button>
          </div>
        `;
        grid.appendChild(box);
      });
    }

    function renderManageList(accs) {
      const el = document.getElementById('acc-manage-list');
      el.innerHTML = '';
      accs.forEach(a => {
        const row = document.createElement('div');
        row.style = 'display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--border); padding:10px 0;';
        row.innerHTML = `
          <div>
            <strong>${a.name}</strong> 
            <span style="color:var(--text-sub); font-size:13px; margin-left:10px;">代理: ${a.proxy || '直连'} | 偏移: ${a.offset_minutes || 0}分</span>
          </div>
          <div class="flex-row">
            <button class="btn btn-secondary btn-sm" onclick="toggleAccount('${a.name}', ${!a.enabled})">${a.enabled ? '停用' : '启用'}</button>
            <button class="btn btn-danger btn-sm" onclick="deleteAccount('${a.name}')">删除</button>
          </div>
        `;
        el.appendChild(row);
      });
    }

    async function saveAccount() {
      const name = document.getElementById('new-acc-name').value.trim();
      const proxy = document.getElementById('new-acc-proxy').value.trim() || null;
      const offset = parseInt(document.getElementById('new-acc-offset').value || 0);
      if (!name) return showToast('请输入账号名称');
      await fetch('/api/accounts', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, proxy, offset_minutes: offset, enabled: true})
      });
      showToast('账号已保存');
      document.getElementById('new-acc-name').value = '';
      document.getElementById('new-acc-proxy').value = '';
      fetchStatus();
    }

    async function toggleAccount(name, enabled) {
      await fetch('/api/accounts/toggle', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, enabled})
      });
      fetchStatus();
    }

    async function deleteAccount(name) {
      if (!confirm('确定删除账号 ' + name + ' 吗?')) return;
      await fetch('/api/accounts?name=' + encodeURIComponent(name), { method: 'DELETE' });
      fetchStatus();
    }

    async function loadSettings() {
      const res = await fetch('/api/settings');
      const s = await res.json();
      document.getElementById('cfg-search-min').value = s.search?.dwell_min_seconds || 180;
      document.getElementById('cfg-search-max').value = s.search?.dwell_max_seconds || 300;
      document.getElementById('cfg-search-stall').value = s.search?.stall_max_retries || 12;
      document.getElementById('cfg-card-min').value = s.cards?.attribution_wait_min || 60;
      document.getElementById('cfg-sc-key').value = s.notify?.serverchan?.sendkey || '';
      document.getElementById('cfg-sc-channel').value = s.notify?.serverchan?.channel || '';
    }

    async function saveSettings() {
      const body = {
        search: {
          dwell_min_seconds: parseInt(document.getElementById('cfg-search-min').value),
          dwell_max_seconds: parseInt(document.getElementById('cfg-search-max').value),
          stall_max_retries: parseInt(document.getElementById('cfg-search-stall').value)
        },
        cards: {
          attribution_wait_min: parseInt(document.getElementById('cfg-card-min').value)
        },
        notify: {
          channels: document.getElementById('cfg-sc-key').value ? ['stdout', 'serverchan'] : ['stdout'],
          serverchan: {
            sendkey: document.getElementById('cfg-sc-key').value.trim(),
            channel: document.getElementById('cfg-sc-channel').value.trim()
          }
        }
      };
      await fetch('/api/settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
      });
      showToast('配置已持久化保存');
    }

    async function testNotify() {
      showToast('正在发送测试通知...');
      const res = await fetch('/api/notify-test', { method: 'POST' });
      const data = await res.json();
      showToast(data.message || '测试完毕');
    }

    async function runAccount(name) {
      showToast('已向后台提交运行任务: ' + name);
      fetch('/api/run', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name})
      });
      switchTab('logs');
    }

    async function runAccountDry(name) {
      const res = await fetch('/api/run?dry_run=1&name=' + encodeURIComponent(name), { method: 'POST' });
      const d = await res.json();
      alert('执行排期计划:\n' + d.output);
    }

    async function triggerRunAll() {
      showToast('已触发全部账号执行');
      fetch('/api/run', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name: '--all'}) });
      switchTab('logs');
    }

    async function fetchLogs() {
      const res = await fetch('/api/logs');
      const text = await res.text();
      document.getElementById('log-output').innerText = text || '暂无日志';
    }

    fetchStatus();
  </script>
</body>
</html>
"""


class WebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path

        if path in ('/', '/index.html'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode('utf-8'))
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
            self._json_resp({'status': 'ok'})
            return

        if path == '/api/accounts':
            from msr import config
            accs = config.accounts()
            name = payload.get('name')
            target = next((a for a in accs if a['name'] == name), None)
            if target:
                target.update(payload)
            else:
                accs.append(payload)
            config.save_accounts(accs)
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
            self._json_resp({'status': 'ok'})
            return

        if path == '/api/notify-test':
            from msr import notify
            notify.notify('MSR-ArmBot Web 控制台测试', '恭喜！通知通道配置正确，Server酱 / Webhook 推送成功！', level='info')
            self._json_resp({'status': 'ok', 'message': '推送请求已发出'})
            return

        if path == '/api/run':
            from msr import runner
            import subprocess
            name = payload.get('name') or urllib.parse.parse_qs(u.query).get('name', [None])[0]
            dry = 'dry_run' in u.query or payload.get('dry_run')

            if dry:
                cmd = [sys.executable, '-m', 'msr', 'run', name or '--all', '--dry-run']
                p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                self._json_resp({'status': 'ok', 'output': p.stdout})
                return

            def bg_runner():
                cmd = [sys.executable, '-m', 'msr', 'run', name or '--all']
                subprocess.run(cmd)

            t = threading.Thread(target=bg_runner)
            t.daemon = True
            t.start()
            self._json_resp({'status': 'ok', 'message': '后台任务已启动'})
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

    def _handle_logs(self):
        from msr import config
        lines = []
        # 读取主日志或最近日志
        today = time.strftime('%Y-%m-%d')
        for acc in config.accounts():
            name = acc['name']
            log_f = os.path.join(config.pfile(name, 'logs'), f'{today}.log')
            if os.path.isfile(log_f):
                lines.append(f'=== 账号 {name} 今日日志 ({today}) ===')
                try:
                    with open(log_f, 'r', encoding='utf-8', errors='ignore') as f:
                        lines.extend(f.readlines()[-80:])
                except Exception:
                    pass
                lines.append('')
        text = ''.join(lines) if lines else '今日暂无执行日志。'
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(text.encode('utf-8'))

    def _json_resp(self, data, code=200):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def log_message(self, format, *args):
        # 简化控制台输出，不干扰主流程
        pass


def start_server(host='0.0.0.0', port=8080):
    server = HTTPServer((host, port), WebHandler)
    print(f'MSR-ArmBot Web 控制台已启动: http://{host}:{port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nWeb 控制台已关闭')
        server.server_close()
