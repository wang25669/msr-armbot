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
   - 方式 A: MSR 官方设备码免密授权 (Device Code 流，手机/电脑扫码授权，后台自动轮询落盘)
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
import random
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer, ThreadingHTTPServer as HTTPServer

import re

LIVE_LOGS = []
LIVE_LOGS_SET = set()
LIVE_LOGS_LOCK = threading.Lock()
MAX_LIVE_LOGS = 2000
LAST_DISK_SYNC = 0
DISK_FILE_STATE = {}


def format_log_line(raw_line, default_acc='系统', known_accounts=None, fallback_ts=None):
    """标准化单行日志为: [HH:MM:SS] [账号/系统] 正文。保证每行仅有唯一时间标签与账号标签。"""
    s = str(raw_line or '').strip()
    if not s:
        return ''

    # 1. 提取或剥离行首可能存在的重复时间戳
    ts = None
    while True:
        m_ts = re.match(r'^(?:\[?(\d{4}-\d{2}-\d{2}\s+)?(\d{2}:\d{2}:\d{2})\]?\s*)+', s)
        if m_ts:
            ts = m_ts.group(2)
            s = s[m_ts.end():].strip()
        else:
            break

    if not ts:
        ts = fallback_ts or time.strftime('%H:%M:%S')

    # 2. 提取或匹配账号标签 [acc] 或裸账号名 acc
    m_acc = re.match(r'^\[([a-zA-Z0-9_\-\u4e00-\u9fa5]+)\]\s*', s)
    if m_acc and (not known_accounts or m_acc.group(1) in known_accounts or m_acc.group(1) in ('系统', '定时调度')):
        acc = m_acc.group(1)
        content = s[m_acc.end():].strip()
    else:
        # 尝试匹配无括号的账号名 (例如 "gkerstnt refresh 第1次失败")
        m_bare = re.match(r'^([a-zA-Z0-9_\-]+)\s+', s)
        if m_bare and known_accounts and m_bare.group(1) in known_accounts:
            acc = m_bare.group(1)
            content = s[m_bare.end():].strip()
        else:
            acc = default_acc or '系统'
            content = s

    # 消除正文中残留的重复自身标签或账号名，如 [tntker] 或 tntker
    while content.startswith(f'[{acc}]'):
        content = content[len(f'[{acc}]'):].strip()
    if content.startswith(f'{acc} '):
        content = content[len(f'{acc} '):].strip()

    return f'[{ts}] [{acc}] {content}'


def append_live_log(msg, name=None):
    from msr import config
    known = None
    try:
        known = {a['name'] for a in config.accounts()}
    except Exception:
        pass
    line = format_log_line(msg, default_acc=name or '系统', known_accounts=known)
    if not line:
        return
    with LIVE_LOGS_LOCK:
        if line not in LIVE_LOGS_SET:
            LIVE_LOGS_SET.add(line)
            LIVE_LOGS.append(line)
            if len(LIVE_LOGS) > MAX_LIVE_LOGS:
                removed = LIVE_LOGS.pop(0)
                LIVE_LOGS_SET.discard(removed)
    print(line, flush=True)


def sync_logs_from_disk(force=False):
    """从落盘日志增量同步，彻底杜绝重复输出，并保证时序一致。"""
    global LAST_DISK_SYNC
    now = time.time()
    if not force and (now - LAST_DISK_SYNC < 2.0):
        return
    LAST_DISK_SYNC = now

    from msr import config
    today = time.strftime('%Y-%m-%d')
    known = set()
    try:
        known = {a['name'] for a in config.accounts()}
    except Exception:
        pass

    new_lines = []

    def _ingest_file(f_path, default_tag):
        if not os.path.isfile(f_path):
            return
        try:
            mtime = os.path.getmtime(f_path)
            state = DISK_FILE_STATE.get(f_path)
            if not force and state and state['mtime'] == mtime:
                return

            offset = 0 if force or not state else state['offset']
            file_size = os.path.getsize(f_path)
            if offset > file_size:
                offset = 0

            f_ts = time.strftime('%H:%M:%S', time.localtime(mtime))
            with open(f_path, 'r', encoding='utf-8', errors='ignore') as f:
                if offset > 0:
                    f.seek(offset)
                for line in f:
                    line_s = line.strip()
                    if not line_s:
                        continue
                    fl = format_log_line(line_s, default_acc=default_tag, known_accounts=known, fallback_ts=f_ts)
                    if fl and fl not in LIVE_LOGS_SET:
                        new_lines.append(fl)
                DISK_FILE_STATE[f_path] = {'mtime': mtime, 'offset': f.tell()}
        except Exception:
            pass

    # 1. 扫描各账号今日落盘日志
    try:
        for acc in config.accounts():
            name = acc['name']
            log_f = os.path.join(config.pfile(name, 'logs'), f'{today}.log')
            _ingest_file(log_f, name)
    except Exception:
        pass

    # 2. 扫描 cron.log (若存在)
    try:
        cron_p = os.path.join(getattr(config, 'HOME', '/data'), 'cron.log')
        _ingest_file(cron_p, '系统')
    except Exception:
        pass

    if new_lines:
        def _extract_ts(line):
            m = re.match(r'^\[(\d{2}:\d{2}:\d{2})\]', line)
            return m.group(1) if m else '00:00:00'

        with LIVE_LOGS_LOCK:
            for l in new_lines:
                if l not in LIVE_LOGS_SET:
                    LIVE_LOGS_SET.add(l)
                    LIVE_LOGS.append(l)
            # 保持全局时间升序
            LIVE_LOGS.sort(key=_extract_ts)
            if len(LIVE_LOGS) > MAX_LIVE_LOGS:
                excess = len(LIVE_LOGS) - MAX_LIVE_LOGS
                for _ in range(excess):
                    removed = LIVE_LOGS.pop(0)
                    LIVE_LOGS_SET.discard(removed)


ACTIVE_RUNNERS = {}
RUNNER_STATE = {
    'running': False,
    'target': '',
    'started_at': 0,
    'last_line': '',
    'exit_code': None,
}
RUNNER_STATE_LOCK = threading.Lock()


def _sync_runner_state():
    running_names = list(ACTIVE_RUNNERS.keys())
    RUNNER_STATE['running'] = bool(running_names)
    RUNNER_STATE['target'] = ', '.join(running_names) if running_names else ''
    if running_names:
        RUNNER_STATE['started_at'] = min(v['started_at'] for v in ACTIVE_RUNNERS.values())
        RUNNER_STATE['last_line'] = ACTIVE_RUNNERS[running_names[-1]].get('last_line', '')
    else:
        RUNNER_STATE['last_line'] = ''


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
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Heiti SC", sans-serif; }
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

    /* 现代化实时日志控制台增强样式 */
    .log-container-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 24px;
      box-shadow: 0 4px 6px -1px rgba(0,0,0,0.2);
      display: flex;
      flex-direction: column;
      height: calc(100vh - 220px);
      min-height: 580px;
      transition: all 0.2s ease;
      position: relative;
    }
    .log-container-card.fullscreen {
      position: fixed;
      inset: 0;
      z-index: 9999;
      width: 100vw;
      height: 100vh;
      border-radius: 0;
      margin: 0;
      padding: 16px 20px;
      background: #070c18;
      border: none;
    }
    .btn-primary-sub {
      background: rgba(56, 189, 248, 0.2);
      color: #38bdf8;
      border: 1px solid rgba(56, 189, 248, 0.4);
    }
    .btn-primary-sub:hover {
      background: rgba(56, 189, 248, 0.35);
      color: #fff;
    }
    .btn-warning-sub {
      background: rgba(245, 158, 11, 0.2);
      color: #fbbf24;
      border: 1px solid rgba(245, 158, 11, 0.4);
    }
    .btn-warning-sub:hover {
      background: rgba(245, 158, 11, 0.35);
      color: #fff;
    }

    /* 指标卡片网格 */
    .log-metrics-grid {
      display: grid;
      grid-template-columns: 1.4fr 1.1fr 1fr;
      gap: 12px;
      margin-bottom: 12px;
      flex-shrink: 0;
    }
    @media (max-width: 900px) {
      .log-metrics-grid { grid-template-columns: 1fr 1fr; }
    }
    @media (max-width: 580px) {
      .log-metrics-grid { grid-template-columns: 1fr; }
    }
    .log-metric-card {
      background: rgba(15, 23, 42, 0.85);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 14px;
      display: flex;
      flex-direction: column;
      justify-content: center;
      transition: border-color 0.15s, transform 0.15s;
    }
    .log-metric-card:hover {
      border-color: #475569;
    }
    .log-metric-label {
      font-size: 11px;
      color: var(--text-sub);
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 4px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .log-metric-val {
      font-size: 13.5px;
      font-weight: 700;
      color: #f1f5f9;
      display: flex;
      align-items: center;
      gap: 6px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    /* 控制与过滤工具栏 */
    .log-toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      justify-content: space-between;
      background: rgba(15, 23, 42, 0.65);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 12px;
      margin-bottom: 12px;
      flex-shrink: 0;
    }
    .log-tool-group {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .log-search-wrapper {
      position: relative;
      display: inline-flex;
      align-items: center;
      min-width: 170px;
    }
    .log-search-wrapper input {
      padding: 6px 28px 6px 10px;
      font-size: 12px;
      background: #090d18;
      border: 1px solid var(--border);
      border-radius: 6px;
      color: #fff;
      height: 30px;
    }
    .log-search-clear {
      position: absolute;
      right: 8px;
      color: #64748b;
      cursor: pointer;
      font-size: 13px;
      display: none;
    }
    .log-search-clear:hover { color: #f1f5f9; }

    /* 终端视窗 */
    .terminal-window {
      flex: 1;
      display: flex;
      flex-direction: column;
      background: #050811;
      border: 1px solid #1e293b;
      border-radius: 8px;
      overflow: hidden;
      position: relative;
      min-height: 380px;
    }
    .terminal-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 6px 14px;
      background: #0d1527;
      border-bottom: 1px solid #1e293b;
      font-size: 12px;
      color: var(--text-sub);
      user-select: none;
      flex-shrink: 0;
    }
    .terminal-dots {
      display: flex;
      gap: 6px;
      align-items: center;
    }
    .dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      display: inline-block;
    }
    .dot-red { background: #ef4444; }
    .dot-yellow { background: #f59e0b; }
    .dot-green { background: #10b981; }

    #log-box {
      flex: 1;
      overflow-y: auto;
      overflow-x: auto;
      padding: 10px 14px;
      font-family: "JetBrains Mono", Menlo, Consolas, Monaco, monospace;
      font-size: 12.5px;
      line-height: 1.65;
      color: #cbd5e1;
      background: transparent;
      border: none;
      border-radius: 0;
      max-height: none;
      margin: 0;
      white-space: pre;
    }
    #log-box.wrap-mode {
      white-space: pre-wrap;
      word-break: break-all;
    }

    /* 单条日志行样式 */
    .l-row {
      display: flex;
      align-items: flex-start;
      padding: 2px 6px;
      border-radius: 4px;
      margin-bottom: 1px;
      transition: background 0.1s;
    }
    .l-row:hover {
      background: rgba(255, 255, 255, 0.04);
    }
    .l-num {
      user-select: none;
      width: 44px;
      flex-shrink: 0;
      color: #475569;
      font-size: 11px;
      text-align: right;
      padding-right: 12px;
    }
    .l-content {
      flex: 1;
      word-break: break-all;
    }
    .l-ts {
      color: #64748b;
      margin-right: 6px;
    }
    .l-acc-badge {
      background: rgba(56, 189, 248, 0.15);
      color: #38bdf8;
      padding: 1px 5px;
      border-radius: 3px;
      font-size: 11px;
      font-weight: 600;
      margin-right: 6px;
      display: inline-block;
    }

    /* 日志行色彩高亮 */
    .l-err {
      background: rgba(239, 68, 68, 0.13) !important;
      border-left: 3px solid #ef4444;
      color: #fca5a5 !important;
    }
    .l-warn {
      background: rgba(245, 158, 11, 0.1) !important;
      border-left: 3px solid #f59e0b;
      color: #fde68a !important;
    }
    .l-succ {
      background: rgba(34, 197, 94, 0.08);
      border-left: 3px solid #22c55e;
      color: #86efac;
    }
    .l-banner {
      background: rgba(99, 102, 241, 0.12);
      border: 1px solid rgba(99, 102, 241, 0.3);
      color: #c7d2fe !important;
      font-weight: 600;
      border-radius: 4px;
      margin: 4px 0;
    }
    .l-mark {
      background: #eab308;
      color: #0f172a;
      padding: 0 2px;
      border-radius: 2px;
      font-weight: 700;
    }

    /* 悬浮提示新日志药丸 */
    .new-logs-pill {
      position: absolute;
      bottom: 18px;
      right: 24px;
      background: linear-gradient(135deg, #0284c7, #0ea5e9);
      color: #fff;
      padding: 8px 18px;
      border-radius: 30px;
      font-size: 12.5px;
      font-weight: 700;
      cursor: pointer;
      box-shadow: 0 6px 20px rgba(0,0,0,0.6), 0 0 10px rgba(14,165,233,0.5);
      display: none;
      align-items: center;
      gap: 6px;
      z-index: 20;
      transition: transform 0.15s, background 0.15s;
    }
    .new-logs-pill:hover {
      transform: translateY(-2px);
      background: linear-gradient(135deg, #0369a1, #0284c7);
    }
    .pulse-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #22c55e;
      display: inline-block;
      box-shadow: 0 0 8px #22c55e;
      animation: pulseAnim 1.8s infinite;
    }
    @keyframes pulseAnim {
      0% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.4; transform: scale(0.85); }
      100% { opacity: 1; transform: scale(1); }
    }
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
        <div class="card-title">添加 / 编辑 MSR 账号</div>
        <div class="grid" style="grid-template-columns: 1.2fr 1fr 1.6fr auto; align-items: flex-end;">
          <div class="form-group">
            <label>账号名称 (唯一标识)</label>
            <input type="text" id="acc-name-input" placeholder="例如: sg_user, cn_main">
          </div>
          <div class="form-group">
            <label>每日定时时间 (24小时制 HH:MM，必填)</label>
            <input type="text" id="acc-time-input" placeholder="例如: 02:30" maxlength="5">
          </div>
          <div class="form-group">
            <label>独立出口代理 (支持 HTTP / SOCKS5，留空直连)</label>
            <div style="display:flex; gap:6px;">
              <input type="text" id="acc-proxy-input" placeholder="http://user:pass@127.0.0.1:7890">
              <button class="btn btn-secondary btn-sm" style="white-space:nowrap;" onclick="testProxyInForm()">⚡ 测试</button>
            </div>
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
              <label>每日自动定时调度总开关 (总闸门：关闭后所有账号定时均不自动触发)</label>
              <select id="sch-daily-enabled">
                <option value="true">开启 (各账号按设定的每日时间自动执行)</option>
                <option value="false">关闭 (仅手动触发)</option>
              </select>
            </div>
            <div class="form-group">
              <label>最大任务并发上限 (max_concurrency，限制同时启动的 Chromium 实例数，默认 2)</label>
              <input type="number" id="sch-max-concurrency" min="1" max="10" placeholder="2">
            </div>
            <div class="form-group">
              <label>极速测试模式 (fast_mode, 开启后等待压缩至 3-6 秒)</label>
              <select id="cfg-fast-mode">
                <option value="false">关闭 (真实真人节奏，约2小时/号)</option>
                <option value="true">开启 (极速调试，仅测试流程连通性)</option>
              </select>
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
              <label>定时启动随机延迟上限 (分钟，默认 25 分钟防整点特征)</label>
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
      <div class="log-container-card" id="log-card">
        <!-- 顶部核心价值指标卡片 -->
        <div class="log-metrics-grid">
          <!-- 1. 当前任务状态 -->
          <div class="log-metric-card">
            <div class="log-metric-label">
              <span>⚡ 当前运行状态</span>
              <span id="metric-state-badge" class="badge badge-info" style="font-size:10px; padding:1px 6px;">空闲</span>
            </div>
            <div class="log-metric-val" id="metric-state-text" title="当前执行状态">
              <span class="pulse-dot" style="background:#64748b; box-shadow:none;" id="metric-pulse"></span>
              <span style="font-size:13px; font-weight:normal; color:var(--text-sub);">暂无运行中的任务</span>
            </div>
          </div>

          <!-- 2. 异常与风控诊断 -->
          <div class="log-metric-card" onclick="quickFilterError()" style="cursor:pointer;" title="点击一键筛选全部异常与告警日志">
            <div class="log-metric-label">
              <span>🛡️ 异常与风控诊断</span>
              <span style="font-size:10px; color:var(--primary);">点击过滤 ↗</span>
            </div>
            <div class="log-metric-val" id="metric-error-text">
              <span style="color:var(--success); font-size:13px;">🟢 全部正常 (0 异常)</span>
            </div>
          </div>

          <!-- 3. 轮询与日志流健康度 -->
          <div class="log-metric-card">
            <div class="log-metric-label">
              <span>📊 轮询流状态</span>
              <span id="metric-poll-status" style="font-size:10px; color:var(--success);">● 自动同步</span>
            </div>
            <div class="log-metric-val" style="font-size:13px; font-weight:normal; color:var(--text-sub);">
              已载入 <strong id="metric-lines-count" style="color:#fff;">0</strong> 行 · 同步于 <span id="log-last-sync" style="color:#38bdf8;">--:--:--</span>
            </div>
          </div>
        </div>

        <!-- 操作与过滤工具栏 -->
        <div class="log-toolbar">
          <div class="log-tool-group">
            <!-- 轮询控制 -->
            <button id="btn-poll-toggle" class="btn btn-secondary btn-sm" onclick="toggleLogPoll()" title="暂停或恢复每3秒自动拉取">
              ⏸️ 暂停自动拉取
            </button>
            <div style="display:flex; align-items:center; gap:4px; font-size:12px; color:var(--text-sub);">
              <span>频率:</span>
              <select id="log-interval-select" onchange="changePollInterval(this.value)" style="width:auto; padding:4px 8px; font-size:12px; height:30px;">
                <option value="3000" selected>3 秒</option>
                <option value="5000">5 秒</option>
                <option value="10000">10 秒</option>
                <option value="manual">手动刷新</option>
              </select>
            </div>
            <button class="btn btn-secondary btn-sm" onclick="loadLogsNow(true)" title="强制拉取最新日志">
              🔄 立即刷新
            </button>

            <span style="color:var(--border);">|</span>

            <!-- 账号过滤 -->
            <div style="display:flex; align-items:center; gap:4px; font-size:12px; color:var(--text-sub);">
              <span>账号:</span>
              <select id="log-acc-filter" onchange="onAccountFilterChange(this.value)" style="width:auto; max-width:130px; padding:4px 8px; font-size:12px; height:30px;">
                <option value="all">全部账号</option>
                <option value="__system__">仅系统/调度</option>
              </select>
            </div>

            <!-- 日志级别过滤 -->
            <div style="display:flex; align-items:center; gap:4px; font-size:12px; color:var(--text-sub);">
              <span>级别:</span>
              <select id="log-level-filter" onchange="onLevelFilterChange(this.value)" style="width:auto; padding:4px 8px; font-size:12px; height:30px;">
                <option value="all">全部日志</option>
                <option value="error">🔴 仅错误/告警</option>
                <option value="milestone">🟢 仅关键阶段/结果</option>
                <option value="search">🔍 仅搜索/答题明细</option>
              </select>
            </div>

            <!-- 搜索框 -->
            <div class="log-search-wrapper">
              <input type="text" id="log-search-input" placeholder="🔍 检索关键词..." oninput="onSearchInput(this.value)">
              <span id="log-search-clear" class="log-search-clear" onclick="clearSearch()">✕</span>
            </div>
          </div>

          <div class="log-tool-group">
            <!-- 自动滚屏开关 -->
            <button id="btn-autoscroll" class="btn btn-primary-sub btn-sm" onclick="toggleAutoScroll()" title="开关自动跟踪滚动到底部">
              📌 自动贴底: 开
            </button>
            <!-- 换行切换 -->
            <button id="btn-wrap-toggle" class="btn btn-primary-sub btn-sm" onclick="toggleWordWrap()" title="切换长日志自动折行">
              ↩ 折行: 开
            </button>
            <!-- 复制 -->
            <button class="btn btn-secondary btn-sm" onclick="copyDisplayedLogs()" title="复制当前视图显示的全部日志">
              📋 复制
            </button>
            <!-- 导出文件 -->
            <button class="btn btn-secondary btn-sm" onclick="downloadLogsFile()" title="下载日志为文本文件">
              💾 导出
            </button>
            <!-- 清屏 -->
            <button class="btn btn-secondary btn-sm" onclick="clearLogView()" title="清空当前页面显示(不影响后端落盘)">
              🧹 清屏
            </button>
            <!-- 全屏 -->
            <button id="btn-fullscreen" class="btn btn-secondary btn-sm" onclick="toggleFullscreen()" title="全屏沉浸式展开控制台">
              ⛶ 全屏
            </button>
          </div>
        </div>

        <!-- 终端主体视窗 -->
        <div class="terminal-window">
          <div class="terminal-bar">
            <div class="terminal-dots">
              <span class="dot dot-red"></span>
              <span class="dot dot-yellow"></span>
              <span class="dot dot-green"></span>
              <span style="font-family:monospace; margin-left:6px; color:#94a3b8; font-weight:600;">terminal@msr-armbot:~</span>
            </div>
            <div id="terminal-stats" style="font-family:monospace; font-size:11px; color:#64748b;">
              正在连接控制台日志流...
            </div>
          </div>

          <!-- 日志滚动容器 (默认开启 wrap-mode) -->
          <div id="log-box" class="wrap-mode">正在加载日志...</div>

          <!-- 悬浮提示：新日志未读浮标 (用户向上翻阅时出现) -->
          <div id="new-logs-pill" class="new-logs-pill" onclick="scrollToBottom(true)">
            <span>⬇ 有新日志</span>
            <span id="pill-count-badge" style="background:rgba(255,255,255,0.25); padding:1px 6px; border-radius:10px; font-size:11px;">0</span>
            <span style="font-weight:normal; font-size:11px;">(点击直达最新)</span>
          </div>
        </div>
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
        所有账号均统一遵循标准化流水线执行，各账号按区域自适应过滤，多账号之间在独立代理与并发控制（默认并发上限 2）下安全隔离执行，杜绝关联风控：
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
        <span class="pipeline-badge" style="background:#10b981;">阶段 9 · 积分结算</span>
        <div>
          <strong style="color:#fff;">检查并领取待领取积分 (claim)</strong>
          <div style="font-size:12px; color:var(--text-sub); margin-top:3px;">检查 Dashboard 顶部是否有已达成但待领取的额外积分面板，自动展开并点击完成结算入账。</div>
        </div>
      </div>

      <div class="pipeline-step">
        <span class="pipeline-badge" style="background:#06b6d4;">阶段 10 · 容错核查</span>
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
        <button id="modal-tab-pwd" class="tab-btn active" onclick="switchModalTab('pwd')">手动登录（推荐）</button>
        <button id="modal-tab-ck" class="tab-btn" onclick="switchModalTab('ck')">Cookie 快速导入 (免密备选)</button>
      </div>

      <!-- 手动登录: 服务端浏览器 + 实时画面 + 输入转发 -->
      <div id="modal-panel-pwd">
        <p style="font-size:13px; color:var(--text-sub); margin-bottom:12px; line-height:1.6;">
          点击下方按钮后，服务端会用<b>该账号专属的浏览器 profile</b> 与<b>该账号配置的代理出口</b>打开微软官方登录页，
          画面直接显示在方框里。密码、验证码、Authenticator 推送、拼图全部由你自己输入/点击；
          登录完成后系统自动抓取 Cookie 并静默换取 Token（不做任何页面状态判定，不代填密码）。
        </p>

        <div id="live-egress-line" style="font-size:12.5px; color:#94a3b8; margin-bottom:10px;">出口: 尚未启动</div>

        <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px;">
          <button id="live-start-btn" class="btn" onclick="startLiveLogin()">🖥️ 打开微软登录页（手动登录）</button>
          <button id="live-finish-btn" class="btn btn-secondary" style="display:none;" onclick="finishLiveLogin()">✅ 我已登录完成，抓取凭据</button>
          <button id="live-close-btn" class="btn btn-secondary" style="display:none;" onclick="closeLiveLogin()">✖ 关闭会话</button>
        </div>

        <div id="login-pwd-status" style="font-size:13px; padding:10px; border-radius:6px; background:#070d19; color:var(--text-sub); margin-top:12px; min-height:38px; display:flex; align-items:center;">
          尚未启动登录会话。
        </div>

        <details id="cred-box" style="margin-top:14px; border:1px solid var(--border); border-radius:8px; padding:10px 12px; background:#0a1220;">
          <summary style="cursor:pointer; font-size:13px; color:#cbd5e1; outline:none;">
            🔐 自动重登凭据（可选：保存账号密码）
            <span id="cred-state" style="font-size:12px; color:#94a3b8; margin-left:6px;"></span>
          </summary>
          <div style="font-size:12px; color:#94a3b8; margin:8px 0 10px; line-height:1.6;">
            会话过期时用这份凭据自动登录（只走"账号 → 密码 → 保持登录"，遇到验证码/推送一律放弃并通知你手动登录）。
            凭据仅存在该账号 profile 目录的 <code>credential.json</code>（0600），不会上传到任何地方。
          </div>
          <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:10px;">
            <input id="cred-username" type="text" placeholder="账号邮箱 (如 xxx@outlook.com)">
            <input id="cred-password" type="password" placeholder="密码">
          </div>
          <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
            <button id="cred-save-btn" class="btn btn-sm" onclick="saveCredential()">保存凭据</button>
            <button id="cred-del-btn" class="btn btn-secondary btn-sm" onclick="deleteCredential()">删除已保存凭据</button>
            <span style="font-size:11.5px; color:#64748b;">保存后账号卡片会出现「清除凭据」按钮</span>
          </div>
        </details>
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

  <!-- 独立弹窗 5: 账号修改代理与调度配置 -->
  <div id="edit-proxy-modal" class="modal-overlay">
    <div class="modal">
      <div class="card-title">
        <span id="edit-proxy-title">✏️ 修改账号代理与定时配置</span>
        <button class="btn btn-secondary btn-sm" onclick="closeEditProxyModal()">关闭</button>
      </div>

      <p style="font-size:13px; color:var(--text-sub); margin-bottom:14px; line-height:1.5;">
        为该账号单独指定每日定时执行时间与出口代理（支持 HTTP / SOCKS5，留空为直连）。修改保存后立即生效。
      </p>

      <div class="form-group">
        <label>账号名称 (唯一标识)</label>
        <input type="text" id="edit-proxy-name" readonly style="background:#0f172a; color:#94a3b8; cursor:not-allowed;">
      </div>

      <div class="form-group">
        <label>每日定时执行时间 (24小时制 HH:MM，必填，例如: 02:30)</label>
        <input type="text" id="edit-proxy-time" placeholder="例如: 02:30" maxlength="5">
      </div>

      <div class="form-group">
        <label>独立出口代理配置 (支持 HTTP / SOCKS5，留空为直连)</label>
        <div style="display:flex; gap:8px;">
          <input type="text" id="edit-proxy-val" placeholder="例如: http://user:pass@127.0.0.1:7890 或留空直连">
          <button class="btn btn-secondary btn-sm" style="white-space:nowrap;" onclick="testProxyInModal()">⚡ 测试代理</button>
          <button class="btn btn-secondary btn-sm" style="white-space:nowrap;" onclick="clearProxyInModal()">设为直连</button>
        </div>
      </div>

      <div id="modal-proxy-test-box" style="display:none; margin-bottom:14px; padding:10px 14px; border-radius:6px; font-size:12.5px; line-height:1.6;"></div>

      <div class="form-group" style="margin-top:10px;">
        <label style="display:inline-flex; align-items:center; gap:8px; cursor:pointer; color:#f1f5f9; font-size:13px;">
          <input type="checkbox" id="edit-proxy-align-fp" checked style="width:auto;">
          <span>同时将该账号的<strong>硬件指纹时区与语言自动对齐至新代理出口</strong>（推荐，防时区风控）</span>
        </label>
      </div>

      <div style="margin-top:20px; display:flex; justify-content:flex-end; gap:10px;">
        <button class="btn btn-secondary" onclick="closeEditProxyModal()">取消</button>
        <button class="btn" id="btn-submit-proxy" onclick="submitEditProxy()">保存代理配置</button>
      </div>
    </div>
  </div>

  <div id="toast" class="toast"></div>

  <script>
    let activeAccountName = '';
    let currentAccounts = [];
    let dcPollTimer = null;
    let logPollTimer = null;
    let currentFpData = null;

    // 实时日志控制台交互状态
    let isAutoScroll = true;
    let isWordWrap = true;
    let isPollPaused = false;
    let pollIntervalMs = '3000';
    let rawLogLines = [];
    let lastLogRawText = '';
    let unreadNewLogsCount = 0;
    let activeLevelFilter = 'all';
    let activeAccountFilter = 'all';
    let activeSearchKeyword = '';
    let isFullscreen = false;
    let knownAccounts = [];

    function updateAccountFilterOptions(accounts) {
      if (!Array.isArray(accounts)) return;
      knownAccounts = accounts.map(a => (typeof a === 'string' ? a : a.name)).filter(Boolean);
      const sel = document.getElementById('log-acc-filter');
      if (!sel) return;

      const currentVal = sel.value;
      sel.innerHTML = '';

      const optAll = document.createElement('option');
      optAll.value = 'all';
      optAll.textContent = '全部日志 (所有账号)';
      sel.appendChild(optAll);

      const optSys = document.createElement('option');
      optSys.value = '__system__';
      optSys.textContent = '🌐 仅系统与调度日志';
      sel.appendChild(optSys);

      knownAccounts.forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = `👤 账号: ${name}`;
        sel.appendChild(opt);
      });

      if (currentVal && (currentVal === 'all' || currentVal === '__system__' || knownAccounts.includes(currentVal))) {
        sel.value = currentVal;
      } else {
        sel.value = 'all';
      }
    }

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
        loadLogsNow(true);
        startLogPolling();
      } else if (tabName === 'settings') {
        loadSettingsForm();
      }
    }

    async function fetchStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        currentAccounts = data.accounts || [];
        renderDashboard(currentAccounts);
        renderManageList(currentAccounts);
        updateAccountFilterOptions(data.accounts || []);
        document.getElementById('stat-summary').innerText = `总账号数: ${data.accounts?.length || 0} | 服务器时间: ${data.time || ''}`;
        if (data.scheduler) {
          const sch = data.scheduler;
          const runner = data.runner || {};
          const badge = document.getElementById('scheduler-badge');
          if (badge) {
            const runningCount = runner.running_count || (runner.running ? 1 : 0);
            const maxConc = sch.max_concurrency || runner.max_concurrency || 2;
            const schText = sch.daily_enabled
              ? `<span style="color:#10b981;">自动调度生效中</span>`
              : `<span style="color:var(--text-sub);">已暂停</span>`;
            badge.innerHTML = `⏰ 定时引擎: ${schText} | ⚡ 运行并发: <b style="color:var(--primary); font-family:monospace;">${runningCount}/${maxConc}</b>`;
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

        const cdp = a.cdp_status || { status: 'idle', desc: '动态闲置' };
        let cdpBadge = `<span class="badge badge-disabled" style="font-size:11px;" title="未启动时自动动态分配空闲端口">🔌 CDP: ${cdp.desc}</span>`;
        if (cdp.status === 'running') {
          cdpBadge = `<span class="badge badge-success" style="font-size:11px;" title="正在运行中的 Chromium CDP 端口">🔌 CDP: ${cdp.desc}</span>`;
        } else if (cdp.status === 'external') {
          cdpBadge = `<span class="badge badge-info" style="font-size:11px;" title="配置的外部固定 Chrome 调试端口">🔌 CDP: ${cdp.desc}</span>`;
        }

        const runBtn = a.enabled
          ? (cdp.status === 'running'
             ? `<button class="btn btn-sm" style="background:#f59e0b; cursor:wait;" disabled>⏳ 运行中</button>`
             : `<button class="btn btn-sm" onclick="runSingle('${a.name}')">立即运行</button>`)
          : `<button class="btn btn-secondary btn-sm" style="opacity:0.65;" onclick="toast('账号 [${a.name}] 当前处于停用状态，请先在“账号管理”中启用')">⏸️ 已停用</button>`;

        let actionButtons = `
          ${runBtn}
          <button class="btn btn-secondary btn-sm" onclick="openEditProxyModal('${a.name}', '${a.proxy || ''}', '${a.schedule_time || ''}')">✏️ 改配置</button>
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
                ${cdpBadge}
                <span class="badge ${a.enabled ? 'badge-success' : 'badge-disabled'}">${a.enabled ? '启用' : '停用'}</span>
              </div>
            </div>
            <div class="acc-meta">
              <div>⏰ 每日定时: <strong style="color:#38bdf8; font-family:monospace;">${a.schedule_time || '未设置'}</strong> | 🔌 CDP: <strong style="color:#a7f3d0;">${cdp.desc}</strong></div>
              <div>💰 当前积分: <strong style="color:var(--primary); font-size:16px;">${a.balance || 0}</strong> <span style="font-size:12px; color:var(--success); margin-left:6px;">(今日: +${todayGained}分)</span></div>

              <!-- MSR 三区架构与代理一致性诊断 -->
              <div style="background:rgba(255,255,255,0.02); border:1px solid rgba(255,255,255,0.07); border-radius:6px; padding:8px 10px; margin:7px 0;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:5px;">
                  <span style="font-weight:700; color:#94a3b8; font-size:11px;">🌍 MSR 业务区域架构与漫游诊断</span>
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

        const cdp = a.cdp_status || { status: 'idle', desc: '动态闲置' };
        let cdpBadge = `<span class="badge badge-disabled" style="font-size:11px;">🔌 CDP: ${cdp.desc}</span>`;
        if (cdp.status === 'running') {
          cdpBadge = `<span class="badge badge-success" style="font-size:11px;">🔌 CDP: ${cdp.desc}</span>`;
        } else if (cdp.status === 'external') {
          cdpBadge = `<span class="badge badge-info" style="font-size:11px;">🔌 CDP: ${cdp.desc}</span>`;
        }

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

        const credBadge = a.has_saved_password
          ? `<span class="badge badge-info" style="font-size:10.5px;" title="已安全持久化账号密码凭据，支持自动自愈重登">🔑 凭据已存</span>`
          : '';

        const credClearBtn = a.has_saved_password
          ? `<button class="btn btn-secondary btn-sm" style="color:#ef4444; font-size:11px;" onclick="clearSavedCred('${a.name}')" title="清除已保存的密码凭据">清除密码</button>`
          : '';

        const row = document.createElement('div');
        row.style = 'display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--border); padding:12px 0; flex-wrap:wrap; gap:10px;';
        row.innerHTML = `
          <div>
            <div style="display:flex; align-items:center; gap:8px;">
              <strong style="font-size:16px; color:#fff;">${a.name}</strong>
              ${a.saved_username ? `<span style="font-size:12px; color:var(--text-sub); font-family:monospace;">(${a.saved_username})</span>` : ''}
              ${authBadge}
              ${cdpBadge}
              ${credBadge}
              <span class="badge ${diagBadgeClass}" style="font-size:11px;">${diag.text}</span>
              <span class="badge badge-info" style="font-size:11px;">近7天 +${a.history_7d?.total_gained || 0}分</span>
            </div>
            <div style="color:var(--text-sub); font-size:13px; margin-top:4px;">
              ⏰ 每日执行: <b style="color:#38bdf8; font-family:monospace;">${a.schedule_time || '未设置'}</b> | 代理: <code>${a.proxy || '直连'}</code> | 👤 资料: <b>${a.account_country || '-'}</b> | 🎁 市场: <b>${a.rewards_market || a.country || '-'}</b> | 🌐 出口: <b>${pGeoShort}</b>
            </div>
            <div style="color:#64748b; font-size:12px; margin-top:2px;">
              指纹: <code style="color:#a7f3d0; font-size:11px;">${a.fingerprint_summary || '默认'}</code>
            </div>
          </div>
          <div class="flex-row">
            <button class="btn btn-primary-sub btn-sm" onclick="openEditProxyModal('${a.name}', '${a.proxy || ''}', '${a.schedule_time || ''}')">✏️ 修改配置</button>
            <button class="btn btn-secondary btn-sm" onclick="openFingerprintModal('${a.name}')">专属指纹</button>
            ${authBtns}
            ${credClearBtn}
            <button class="btn btn-secondary btn-sm" onclick="toggleAcc('${a.name}', ${!a.enabled})">${a.enabled ? '停用' : '启用'}</button>
            <button class="btn btn-danger btn-sm" onclick="deleteAcc('${a.name}')">删除</button>
          </div>
        `;
        el.appendChild(row);
      });
    }

    async function saveNewAccount() {
      const name = document.getElementById('acc-name-input').value.trim();
      const schedule_time = document.getElementById('acc-time-input').value.trim();
      const proxy = document.getElementById('acc-proxy-input').value.trim() || null;
      if (!name) return toast('请输入账号标识名称');
      if (!schedule_time) return toast('请输入每日定时执行时间 (24小时制 HH:MM，如 02:30)');
      if (!/^([01]\d|2[0-3]):([0-5]\d)$/.test(schedule_time)) return toast('定时时间格式错误，须为 24小时制 HH:MM (如 02:30)');

      const res = await fetch('/api/accounts', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, schedule_time, proxy, enabled: true})
      });
      const d = await res.json().catch(() => ({}));
      if (res.status !== 200) {
        return toast('保存失败: ' + (d.message || '未知错误'));
      }
      toast(`账号 ${name} 已保存 (每日定时: ${schedule_time})`);
      document.getElementById('acc-name-input').value = '';
      document.getElementById('acc-time-input').value = '';
      document.getElementById('acc-proxy-input').value = '';
      fetchStatus();
    }

    let editingAccountName = '';

    function openEditProxyModal(name, currentProxy, currentTime) {
      editingAccountName = name;
      document.getElementById('edit-proxy-title').innerText = `✏️ 修改账号代理与定时: [${name}]`;
      document.getElementById('edit-proxy-name').value = name;
      const cleanProxy = (currentProxy && currentProxy !== 'null' && currentProxy !== 'undefined') ? currentProxy : '';
      document.getElementById('edit-proxy-val').value = cleanProxy;
      document.getElementById('edit-proxy-time').value = (currentTime && currentTime !== 'null' && currentTime !== 'undefined') ? currentTime : '02:00';
      document.getElementById('edit-proxy-align-fp').checked = true;

      const box = document.getElementById('modal-proxy-test-box');
      box.style.display = 'none';
      box.innerHTML = '';

      document.getElementById('edit-proxy-modal').style.display = 'flex';
    }

    function closeEditProxyModal() {
      document.getElementById('edit-proxy-modal').style.display = 'none';
    }

    async function testProxyInModal() {
      const proxy = document.getElementById('edit-proxy-val').value.trim();
      const box = document.getElementById('modal-proxy-test-box');
      if (!proxy) {
        box.style.display = 'block';
        box.style.background = 'rgba(56,189,248,0.1)';
        box.style.border = '1px solid rgba(56,189,248,0.3)';
        box.style.color = '#38bdf8';
        box.innerHTML = 'ℹ️ 当前代理配置为空，账号将采用直连方式运行。';
        return;
      }
      box.style.display = 'block';
      box.style.background = 'rgba(56,189,248,0.1)';
      box.style.border = '1px solid rgba(56,189,248,0.3)';
      box.style.color = '#38bdf8';
      box.innerHTML = '⚡ 正在连接代理服务器测试出口与延迟...';

      try {
        const res = await fetch('/api/accounts/proxy-test', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({proxy, name: editingAccountName})
        });
        const d = await res.json();
        if (d.status === 'ok') {
          const g = d.data;
          box.style.background = 'rgba(16,185,129,0.1)';
          box.style.border = '1px solid rgba(16,185,129,0.3)';
          box.style.color = '#10b981';
          box.innerHTML = `🟢 <b>代理连通正常!</b> 出口 IP: <code>${g.ip}</code> | 地区: <b>${g.country_name || g.country} · ${g.city || ''}</b> | 时区: <b>${g.timezone}</b> | 延迟: <b>${g.latency_ms}ms</b>`;
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
        box.innerText = '测试请求异常: ' + e;
      }
    }

    function clearProxyInModal() {
      document.getElementById('edit-proxy-val').value = '';
      const box = document.getElementById('modal-proxy-test-box');
      box.style.display = 'block';
      box.style.background = 'rgba(56,189,248,0.1)';
      box.style.border = '1px solid rgba(56,189,248,0.3)';
      box.style.color = '#38bdf8';
      box.innerHTML = 'ℹ️ 已清空输入。点击【保存代理配置】后该账号将恢复为直连模式。';
    }

    async function submitEditProxy() {
      const name = editingAccountName;
      const proxy = document.getElementById('edit-proxy-val').value.trim() || null;
      const schedule_time = document.getElementById('edit-proxy-time').value.trim();
      const autoAlign = document.getElementById('edit-proxy-align-fp').checked;
      const btn = document.getElementById('btn-submit-proxy');

      if (!schedule_time) return toast('请输入每日定时执行时间 (如 02:30)');
      if (!/^([01]\d|2[0-3]):([0-5]\d)$/.test(schedule_time)) return toast('定时时间格式错误，须为 24小时制 HH:MM (如 02:30)');

      btn.disabled = true;
      btn.innerText = '正在保存...';

      try {
        const res = await fetch('/api/accounts', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            name,
            proxy,
            schedule_time,
            auto_align_proxy: autoAlign
          })
        });
        const d = await res.json();
        if (d.status === 'ok') {
          if (autoAlign && proxy) {
            try {
              await fetch('/api/accounts/fingerprint/align-proxy', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name})
              });
            } catch(e) {}
          }
          toast(`账号 [${name}] 的代理配置已成功更新！`);
          closeEditProxyModal();
          fetchStatus();
        } else {
          toast('保存失败: ' + (d.message || '未知错误'));
        }
      } catch(e) {
        toast('请求失败: ' + e);
      } finally {
        btn.disabled = false;
        btn.innerText = '保存代理配置';
      }
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

    async function clearSavedCred(name) {
      if (!confirm(`确定清除账号 [${name}] 保存的 MSR 账号密码凭据吗？（清除后凭据过期将无法自动静默重登）`)) return;
      await fetch('/api/auth/credential/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name})
      });
      toast(`账号 ${name} 的保存凭据已清除`);
      fetchStatus();
    }

    async function runSingle(name) {
      toast(`正在启动账号 ${name} 任务...`);
      const res = await fetch('/api/run', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name})
      });
      if (res.status === 409) {
        const d = await res.json().catch(() => ({}));
        toast(`⚠️ ${d.message || '已有任务正在执行中，请等待完成'}`);
      }
      setTab('logs');
    }

    async function triggerRunAll() {
      toast('已提交全部账号执行队列');
      const res = await fetch('/api/run', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: '--all'})
      });
      if (res.status === 409) {
        const d = await res.json().catch(() => ({}));
        toast(`⚠️ ${d.message || '已有任务正在执行中，请等待完成'}`);
      }
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
        document.getElementById('sch-max-concurrency').value = s.schedule?.max_concurrency ?? 2;
        document.getElementById('cfg-fast-mode').value = String(!!s.schedule?.fast_mode);
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
          max_concurrency: parseInt(document.getElementById('sch-max-concurrency').value) || 2,
          fast_mode: document.getElementById('cfg-fast-mode').value === 'true',
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

    /* 现代化轻量级实时日志控制台引擎 */

    function escapeHtml(str) {
      if (!str) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }

    function startLogPolling() {
      if (logPollTimer) clearInterval(logPollTimer);
      if (isPollPaused || pollIntervalMs === 'manual') return;
      const ms = parseInt(pollIntervalMs, 10) || 3000;
      logPollTimer = setInterval(() => loadLogsNow(false), ms);
    }

    function toggleLogPoll() {
      isPollPaused = !isPollPaused;
      const btn = document.getElementById('btn-poll-toggle');
      const statusEl = document.getElementById('metric-poll-status');
      if (isPollPaused) {
        if (logPollTimer) clearInterval(logPollTimer);
        btn.innerHTML = '▶️ 恢复自动拉取';
        btn.classList.add('btn-warning-sub');
        btn.classList.remove('btn-secondary');
        if (statusEl) statusEl.innerHTML = '<span style="color:var(--warning);">⏸ 已暂停拉取</span>';
        toast('已暂停实时日志自动拉取，可安心向上翻看');
      } else {
        btn.innerHTML = '⏸️ 暂停自动拉取';
        btn.classList.remove('btn-warning-sub');
        btn.classList.add('btn-secondary');
        if (statusEl) statusEl.innerHTML = `<span style="color:var(--success);">● 每 ${parseInt(pollIntervalMs)/1000}s 同步</span>`;
        startLogPolling();
        loadLogsNow(true);
        toast('已恢复实时日志自动拉取');
      }
    }

    function changePollInterval(val) {
      pollIntervalMs = val;
      const statusEl = document.getElementById('metric-poll-status');
      if (val === 'manual') {
        if (logPollTimer) clearInterval(logPollTimer);
        if (statusEl) statusEl.innerHTML = '<span style="color:var(--text-sub);">✋ 仅手动刷新</span>';
        toast('已切换为手动刷新模式');
      } else {
        const sec = parseInt(val, 10) / 1000;
        if (statusEl) statusEl.innerHTML = `<span style="color:var(--success);">● 每 ${sec}s 同步</span>`;
        if (!isPollPaused) startLogPolling();
        toast(`已调整刷新频率为每 ${sec} 秒`);
      }
    }

    function setupLogScrollListener() {
      const box = document.getElementById('log-box');
      const pill = document.getElementById('new-logs-pill');
      if (!box) return;
      box.addEventListener('scroll', () => {
        const distanceToBottom = box.scrollHeight - box.scrollTop - box.clientHeight;
        if (distanceToBottom <= 45) {
          if (!isAutoScroll) {
            isAutoScroll = true;
            updateAutoScrollUI();
          }
          unreadNewLogsCount = 0;
          if (pill) pill.style.display = 'none';
        } else {
          if (isAutoScroll) {
            isAutoScroll = false;
            updateAutoScrollUI();
          }
        }
      });
    }

    function scrollToBottom(smooth = true) {
      const box = document.getElementById('log-box');
      const pill = document.getElementById('new-logs-pill');
      if (!box) return;
      isAutoScroll = true;
      updateAutoScrollUI();
      unreadNewLogsCount = 0;
      if (pill) pill.style.display = 'none';
      box.scrollTo({
        top: box.scrollHeight,
        behavior: smooth ? 'smooth' : 'auto'
      });
    }

    function toggleAutoScroll() {
      if (isAutoScroll) {
        isAutoScroll = false;
        updateAutoScrollUI();
        toast('已关闭自动贴底，自由翻阅历史中');
      } else {
        scrollToBottom(true);
        toast('已锁定最底部');
      }
    }

    function updateAutoScrollUI() {
      const btn = document.getElementById('btn-autoscroll');
      if (!btn) return;
      if (isAutoScroll) {
        btn.innerHTML = '📌 自动贴底: 开';
        btn.className = 'btn btn-primary-sub btn-sm';
      } else {
        btn.innerHTML = '📌 自由翻看 (未贴底)';
        btn.className = 'btn btn-secondary btn-sm';
      }
    }

    function extractLogInsights(lines, runnerState) {
      let errorCount = 0;
      let warnCount = 0;
      let activeState = '空闲中 (待机就绪)';
      let isRunning = false;

      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (!line.trim()) continue;
        const l = line.toLowerCase();

        if (l.includes('error') || l.includes('exception') || l.includes('失败') || l.includes('失效') || l.includes('熔断') || l.includes('timed out') || l.includes('failed')) {
          errorCount++;
        } else if (l.includes('warn') || l.includes('警告') || l.includes('未推进') || l.includes('重试') || l.includes('未达标')) {
          warnCount++;
        }
      }

      // 1. 若后端 runnerState 明确处于运行状态
      if (runnerState && runnerState.running === true) {
        isRunning = true;
        let hint = (runnerState.last_line || '').trim();
        if (hint) {
          hint = hint.replace(/^\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\]\s*/, '').replace(/^.*?>>>\s*/, '').replace(/^.*?⏰\s*/, '');
          activeState = hint;
        } else {
          activeState = '任务流水线执行中...';
        }
      } else {
        // 后端 runner 为空闲或未提供 runnerState：逆序扫描日志末尾关键事件
        let foundFinalStatus = false;
        for (let i = lines.length - 1; i >= 0; i--) {
          const line = lines[i].trim();
          if (!line) continue;

          // 优先匹配任务终态/完成/退出事件（一旦检测到最新的标志为结束，立即判定为空闲）
          if (line.includes('=== 运行结束') ||
              line.includes('<<< 任务执行完毕') ||
              line.includes('已完全关闭浏览器') ||
              line.includes('全账号任务已完成') ||
              line.includes('单账号任务已完成') ||
              line.includes('所有账号自动化任务执行完毕') ||
              line.includes('今日任务已全部完成') ||
              line.includes('今日暂无执行日志') ||
              line.includes('等待下一次定时任务')) {
            isRunning = false;
            activeState = '空闲中 (任务已执行完毕)';
            foundFinalStatus = true;
            break;
          }

          // 仅当后端 runner 未明确声明 false 时，才允许通过日志启动标识别为运行中（防止旧的阶段性日志造成状态卡死）
          if ((!runnerState || runnerState.running !== false) &&
              (line.includes('>>> 开始执行任务') ||
               line.includes('正在自动唤醒') ||
               line.includes('开始 MSR 账号全自动登录') ||
               line.includes('=== 运行开始') ||
               line.includes('>>> 开始浏览器阶段') ||
               (line.includes('搜索') && (line.includes('停留') || line.includes('计数'))))) {
            isRunning = true;
            let clean = line.replace(/^\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\]\s*/, '').replace(/^.*?>>>\s*/, '').replace(/^.*?⏰\s*/, '');
            activeState = clean;
            foundFinalStatus = true;
            break;
          }
        }

        if (!foundFinalStatus) {
          isRunning = false;
          activeState = '空闲中 (待机就绪)';
        }
      }

      // 状态卡片
      const pulseEl = document.getElementById('metric-pulse');
      const stateTextEl = document.getElementById('metric-state-text');
      const stateBadgeEl = document.getElementById('metric-state-badge');
      if (pulseEl && stateTextEl && stateBadgeEl) {
        if (isRunning) {
          pulseEl.style.background = '#22c55e';
          pulseEl.style.boxShadow = '0 0 8px #22c55e';
          stateBadgeEl.className = 'badge badge-success';
          stateBadgeEl.innerText = '运行中';
          stateTextEl.innerHTML = `<span class="pulse-dot"></span><span style="font-size:13px; font-weight:600; color:#fff;" title="${escapeHtml(activeState)}">${escapeHtml(activeState)}</span>`;
        } else {
          pulseEl.style.background = '#64748b';
          pulseEl.style.boxShadow = 'none';
          stateBadgeEl.className = 'badge badge-info';
          stateBadgeEl.innerText = '空闲';
          stateTextEl.innerHTML = `<span class="pulse-dot" style="background:#64748b; box-shadow:none;"></span><span style="font-size:13px; font-weight:normal; color:var(--text-sub);">${escapeHtml(activeState)}</span>`;
        }
      }

      // 异常卡片
      const errTextEl = document.getElementById('metric-error-text');
      if (errTextEl) {
        if (errorCount > 0) {
          errTextEl.innerHTML = `<span style="color:var(--danger); font-size:13px; font-weight:700;">🔴 检出 ${errorCount} 项异常 ${warnCount ? `(${warnCount}项警告)` : ''}</span>`;
        } else if (warnCount > 0) {
          errTextEl.innerHTML = `<span style="color:var(--warning); font-size:13px; font-weight:600;">🟡 0 异常 · ${warnCount} 项警告</span>`;
        } else {
          errTextEl.innerHTML = `<span style="color:var(--success); font-size:13px;">🟢 全部正常 (0 异常)</span>`;
        }
      }

      // 总行数
      const countEl = document.getElementById('metric-lines-count');
      if (countEl) countEl.innerText = lines.length;
    }

    function renderLogRows() {
      const box = document.getElementById('log-box');
      if (!box) return;
      if (!rawLogLines || rawLogLines.length === 0) {
        box.innerHTML = '<div style="color:var(--text-sub); padding:16px;">暂无日志记录。</div>';
        const st = document.getElementById('terminal-stats');
        if (st) st.innerText = '0 行日志';
        return;
      }

      let html = '';
      let matchedCount = 0;
      const kw = activeSearchKeyword.trim().toLowerCase();

      for (let i = 0; i < rawLogLines.length; i++) {
        const line = rawLogLines[i];
        if (!line.trim()) continue;

        // 1. 账号过滤 (每行日志必定严格包含 [账号] 或 [系统])
        if (activeAccountFilter !== 'all') {
          if (activeAccountFilter === '__system__') {
            if (!line.includes('[系统]') && !line.includes('[定时调度]')) continue;
          } else {
            if (!line.includes(`[${activeAccountFilter}]`)) continue;
          }
        }

        const l = line.toLowerCase();
        const isErr = l.includes('error') || l.includes('exception') || l.includes('失败') || l.includes('失效') || l.includes('熔断') || l.includes('timed out') || l.includes('failed');
        const isWarn = l.includes('warn') || l.includes('警告') || l.includes('未推进') || l.includes('重试') || l.includes('未达标');
        const isSucc = l.includes('成功') || l.includes('完成') || l.includes('[ok]') || l.includes('all_ok') || l.includes('入账') || l.includes('有效') || l.includes('自适应对齐至');
        const isBanner = line.includes('===') || line.includes('>>>') || line.includes('<<<');
        const isSearch = l.includes('搜索') || l.includes('热搜') || l.includes('打卡') || l.includes('答题') || l.includes('资讯') || l.includes('阅读');

        // 2. 级别过滤
        if (activeLevelFilter === 'error' && !isErr && !isWarn) continue;
        if (activeLevelFilter === 'milestone' && !isBanner && !isSucc) continue;
        if (activeLevelFilter === 'search' && !isSearch) continue;

        // 3. 搜索关键词过滤
        if (kw && !l.includes(kw)) continue;

        matchedCount++;

        let rowClass = 'l-row';
        if (isErr) rowClass += ' l-err';
        else if (isWarn) rowClass += ' l-warn';
        else if (isBanner) rowClass += ' l-banner';
        else if (isSucc) rowClass += ' l-succ';

        let escaped = escapeHtml(line);

        // 高亮行首时间戳 [HH:MM:SS]
        escaped = escaped.replace(/^\[(\d{2}:\d{2}:\d{2})\]/g, '<span class="l-ts">[$1]</span>');

        // 高亮行首紧随的账号徽标 [TAG]
        escaped = escaped.replace(/(<span class="l-ts">\[\d{2}:\d{2}:\d{2}\]<\/span>\s*)\[([a-zA-Z0-9_\-\u4e00-\u9fa5]+)\]/g, (match, tsPart, p1) => {
          if (p1 === '系统' || p1 === '定时调度') {
            return `${tsPart}<span class="l-acc-badge" style="background:rgba(148,163,184,0.18); color:#94a3b8;">${p1}</span>`;
          }
          return `${tsPart}<span class="l-acc-badge">${p1}</span>`;
        });

        // 高亮关键词 (安全无正则异常匹配)
        if (kw) {
          const lowerEsc = escaped.toLowerCase();
          const kLen = kw.length;
          let sIdx = 0;
          let resHtml = '';
          while (true) {
            const fIdx = lowerEsc.indexOf(kw, sIdx);
            if (fIdx === -1) {
              resHtml += escaped.slice(sIdx);
              break;
            }
            resHtml += escaped.slice(sIdx, fIdx);
            resHtml += '<mark class="l-mark">' + escaped.slice(fIdx, fIdx + kLen) + '</mark>';
            sIdx = fIdx + kLen;
          }
          escaped = resHtml;
        }

        html += `<div class="${rowClass}"><span class="l-num">${matchedCount}</span><span class="l-content">${escaped}</span></div>`;
      }

      box.innerHTML = html || '<div style="color:var(--text-sub); padding:16px;">无符合当前筛选条件的日志行。</div>';
      const st = document.getElementById('terminal-stats');
      if (st) st.innerText = `共 ${rawLogLines.length} 行 · 当前显示 ${matchedCount} 行`;
    }

    function onSearchInput(val) {
      activeSearchKeyword = val;
      const clearBtn = document.getElementById('log-search-clear');
      if (clearBtn) clearBtn.style.display = val ? 'inline' : 'none';
      renderLogRows();
      if (isAutoScroll) scrollToBottom(false);
    }

    function clearSearch() {
      const input = document.getElementById('log-search-input');
      if (input) input.value = '';
      onSearchInput('');
    }

    function quickFilterError() {
      activeLevelFilter = activeLevelFilter === 'error' ? 'all' : 'error';
      const sel = document.getElementById('log-level-filter');
      if (sel) sel.value = activeLevelFilter;
      renderLogRows();
      toast(activeLevelFilter === 'error' ? '已筛选全部异常与告警日志' : '已重置日志级别筛选');
    }

    async function onAccountFilterChange(val) {
      activeAccountFilter = val;
      lastLogRawText = '';
      toast(val === 'all' ? '已切换为全部日志' : (val === '__system__' ? '已切换为仅系统/调度日志' : `正在切换至账号 [${val}] 的完整日志...`));
      await loadLogsNow(true);
      if (isAutoScroll) scrollToBottom(false);
    }

    function onLevelFilterChange(val) {
      activeLevelFilter = val;
      renderLogRows();
      if (isAutoScroll) scrollToBottom(false);
    }

    function toggleWordWrap() {
      isWordWrap = !isWordWrap;
      const box = document.getElementById('log-box');
      const btn = document.getElementById('btn-wrap-toggle');
      if (box && btn) {
        if (isWordWrap) {
          box.classList.add('wrap-mode');
          btn.innerHTML = '↩ 折行: 开';
          btn.className = 'btn btn-primary-sub btn-sm';
        } else {
          box.classList.remove('wrap-mode');
          btn.innerHTML = '↩ 折行: 关';
          btn.className = 'btn btn-secondary btn-sm';
        }
      }
    }

    function toggleFullscreen() {
      isFullscreen = !isFullscreen;
      const card = document.getElementById('log-card');
      const btn = document.getElementById('btn-fullscreen');
      if (card && btn) {
        if (isFullscreen) {
          card.classList.add('fullscreen');
          btn.innerHTML = '⤓ 退出全屏';
          btn.className = 'btn btn-warning-sub btn-sm';
        } else {
          card.classList.remove('fullscreen');
          btn.innerHTML = '⛶ 全屏';
          btn.className = 'btn btn-secondary btn-sm';
        }
      }
    }

    function copyDisplayedLogs() {
      if (!rawLogLines || rawLogLines.length === 0) {
        toast('暂无日志可复制');
        return;
      }
      const box = document.getElementById('log-box');
      if (!box) return;
      const text = box.innerText;
      navigator.clipboard.writeText(text).then(() => {
        toast('已复制当前视图显示的全部日志');
      }).catch(() => {
        toast('复制失败，请手动选取');
      });
    }

    function downloadLogsFile() {
      if (!rawLogLines || rawLogLines.length === 0) {
        toast('暂无日志可导出');
        return;
      }
      const text = rawLogLines.join(String.fromCharCode(10));
      const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      const dateStr = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
      a.download = `msr_bot_logs_${dateStr}.log`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      toast('日志文件导出成功');
    }

    function clearLogView() {
      rawLogLines = [];
      lastLogRawText = '';
      renderLogRows();
      toast('已清空当前屏幕显示');
    }

    async function loadLogsNow(force = false) {
      if (isPollPaused && !force) return;
      try {
        let url = '/api/logs?format=json';
        if (activeAccountFilter && activeAccountFilter !== 'all') {
          url += `&acc=${encodeURIComponent(activeAccountFilter)}`;
        }
        const res = await fetch(url);
        const data = await res.json();
        const text = data.text || '';
        const runner = data.runner || null;

        const syncEl = document.getElementById('log-last-sync');
        const timeStr = new Date().toLocaleTimeString();
        if (syncEl) syncEl.innerText = timeStr;

        if (!force && text === lastLogRawText) {
          extractLogInsights(rawLogLines, runner);
          return;
        }

        const oldLinesCount = rawLogLines.length;
        lastLogRawText = text;
        rawLogLines = (text || '').split(String.fromCharCode(10));
        const newLinesCount = rawLogLines.length - oldLinesCount;

        const pill = document.getElementById('new-logs-pill');
        const countBadge = document.getElementById('pill-count-badge');
        if (!isAutoScroll && newLinesCount > 0 && oldLinesCount > 0) {
          unreadNewLogsCount += newLinesCount;
          if (pill && countBadge) {
            countBadge.innerText = unreadNewLogsCount;
            pill.style.display = 'flex';
          }
        }

        extractLogInsights(rawLogLines, runner);
        renderLogRows();

        if (isAutoScroll) {
          const box = document.getElementById('log-box');
          if (box) box.scrollTop = box.scrollHeight;
        }
      } catch(e) {
        const syncEl = document.getElementById('log-last-sync');
        if (syncEl) syncEl.innerText = '连接异常';
      }
    }


    /* 登录弹窗逻辑 (手动登录: 服务端浏览器 + 实时画面 + 输入转发) */
    function openLoginModal(name, isReLogin) {
      activeAccountName = name;
      document.getElementById('modal-title').innerText = isReLogin
        ? `账号重新登录 / 凭据更新: [${name}]`
        : `账号授权登录: [${name}]`;

      const cur = (currentAccounts || []).find(x => x.name === name) || {};
      const hintEl = document.getElementById('login-auth-hint');
      const authTxt = (cur.auth && cur.auth.valid) ? (' ' + cur.auth.summary) : '';
      const proxyTxt = cur.proxy ? cur.proxy : '直连（未配置代理）';
      hintEl.innerHTML = (isReLogin
          ? `<span style="color:var(--success)">🟢 该账号已有凭据。</span>`
          : `<span style="color:var(--danger)">🔴 该账号尚未登录或凭据失效。</span>`)
        + `${authTxt}<br><span style="color:#94a3b8; font-size:12px;">浏览器 profile: [${name}]；出口: ${proxyTxt}；时区/语言/指纹按该出口自动匹配。</span>`;

      closeLiveOverlay();
      document.getElementById('live-finish-btn').style.display = 'none';
      document.getElementById('live-close-btn').style.display = 'none';
      const sb = document.getElementById('live-start-btn');
      if (sb) sb.disabled = false;
      document.getElementById('live-egress-line').innerHTML = '出口: 尚未启动';
      document.getElementById('login-pwd-status').innerHTML = '尚未启动登录会话。';
      initCredBox(cur);
      document.getElementById('ck-text-input').value = '';
      document.getElementById('ck-result-msg').innerText = '';
      document.getElementById('login-modal').style.display = 'flex';
      switchModalTab('pwd');
    }

    let livePollTimer = null;
    let liveFrameSeq = 0;
    let liveViewport = { w: 1280, h: 900 };      // 页面真实视口(innerWidth/Height), 坐标换算基准
    let liveZoom = 'fit';                        // fit | 100%

    function closeLoginModal() {
      stopLivePolling();
      document.getElementById('login-modal').style.display = 'none';
      fetchStatus();
    }

    function switchModalTab(tab) {
      document.getElementById('modal-tab-pwd').classList.toggle('active', tab === 'pwd');
      document.getElementById('modal-tab-ck').classList.toggle('active', tab === 'ck');
      document.getElementById('modal-panel-pwd').style.display = tab === 'pwd' ? 'block' : 'none';
      document.getElementById('modal-panel-ck').style.display = tab === 'ck' ? 'block' : 'none';
    }

    /* ===== 手动登录: 实时画面拉流 + 鼠标/键盘/触屏输入转发 ===== */
    function liveImg() { return document.getElementById('live-screen-img'); }

    async function startLiveLogin() {
      if (!activeAccountName) return toast('请先选择账号');
      const statusEl = document.getElementById('login-pwd-status');
      const btn = document.getElementById('live-start-btn');
      if (btn) btn.disabled = true;
      statusEl.innerHTML = '<span style="color:var(--primary)">⏳ 正在用该账号的 profile 与代理启动浏览器...</span>';
      try {
        const res = await fetch('/api/auth/live/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: activeAccountName})
        });
        const d = await res.json();
        if (d.status !== 'ok') throw new Error(d.message || '启动失败');

        openLiveOverlay();
        document.getElementById('live-finish-btn').style.display = 'inline-block';
        document.getElementById('live-close-btn').style.display = 'inline-block';
        liveFrameSeq = 0;
        applyLiveSession(d.session);
        bindLiveInput();
        bindLiveKeys();
        focusLive();
        if (livePollTimer) clearInterval(livePollTimer);
        livePollTimer = setInterval(pollLiveLogin, 1200);
        pumpLiveFrames();
      } catch(err) {
        statusEl.innerHTML = `<span style="color:var(--danger)">❌ 启动失败: ${err.message || err}</span>`;
        if (btn) btn.disabled = false;
      }
    }

    function applyLiveSession(s) {
      if (!s) return;
      const statusEl = document.getElementById('login-pwd-status');
      const egressEl = document.getElementById('live-egress-line');
      if (s.page_viewport && s.page_viewport.w) {
        liveViewport = { w: s.page_viewport.w, h: s.page_viewport.h };
      } else if (s.viewport && s.viewport.w) {
        liveViewport = s.viewport;
      }
      const accEl = document.getElementById('live-overlay-acc');
      if (accEl) accEl.innerText = '[' + s.name + ']';
      const egEl = document.getElementById('live-overlay-egress');
      if (egEl && s.egress && s.egress.status === 'ok') {
        egEl.innerText = '出口 ' + (s.egress.ip || '-') + ' (' + (s.egress.country || '-') + ') 经 ' + (s.proxy || '直连');
      }
      if (egressEl && s.egress) {
        const e = s.egress;
        let t;
        if (e.status === 'ok') {
          t = `出口: <b>${e.ip || '-'}</b> (${e.country || '-'}, ${e.timezone || '-'}) 经 ${s.proxy || '直连'}`;
        } else {
          t = `出口: 未能确认 (${s.proxy || '直连'})，画面若打不开请检查代理`;
        }
        if (e.warning) t += ` <span style="color:#f59e0b">⚠️ ${e.warning}</span>`;
        egressEl.innerHTML = t;
      }
      if (statusEl && s.message) {
        const bad = (s.status === 'error' || s.status === 'needs_action');
        const good = (s.status === 'success');
        const color = good ? 'var(--success)' : (bad ? 'var(--danger)' : 'var(--primary)');
        const icon = s.status === 'waiting' ? '🖥️ ' : (s.status === 'finishing' ? '⏳ ' : '');
        statusEl.innerHTML = `<span style="color:${color}">${icon}${s.message}</span>`;
      }
    }

    async function pollLiveLogin() {
      if (!activeAccountName) return;
      try {
        const res = await fetch(`/api/auth/live/status?name=${encodeURIComponent(activeAccountName)}`);
        const s = await res.json();
        if (s.status === 'idle') { stopLivePolling(); return; }
        applyLiveSession(s);
        if (s.status === 'success') {
          stopLivePolling();
          document.getElementById('live-finish-btn').style.display = 'none';
          document.getElementById('live-close-btn').style.display = 'none';
          closeLiveOverlay();
          document.getElementById('live-start-btn').disabled = false;
          toast('登录成功：Cookie 与 Token 已就绪');
          fetchStatus();
        } else if (s.status === 'error' || s.status === 'cancelled') {
          stopLivePolling();
          document.getElementById('live-start-btn').disabled = false;
        }
      } catch(e) {}
    }

    function stopLivePolling() {
      if (livePollTimer) { clearInterval(livePollTimer); livePollTimer = null; }
    }

    /* 画面: 带 seq 的增量拉流, 无新帧时服务端返回 304, 不产生重绘与带宽浪费 */
    let livePumping = false;
    async function pumpLiveFrames() {
      if (livePumping) return;
      livePumping = true;
      try {
        while (livePollTimer && activeAccountName && liveOverlayOpen) {
          const url = `/api/auth/live/frame?name=${encodeURIComponent(activeAccountName)}&seq=${liveFrameSeq}`;
          let res;
          try {
            res = await fetch(url, {cache: 'no-store'});
          } catch(e) { break; }
          if (res.status === 404) break;
          if (res.status === 200) {
            const seq = parseInt(res.headers.get('X-Frame-Seq') || '0', 10);
            const blob = await res.blob();
            if (blob && blob.size > 0) {
              const img = liveImg();
              const old = img.dataset.objurl;
              const u = URL.createObjectURL(blob);
              img.dataset.objurl = u;
              img.src = u;
              if (old) setTimeout(() => { try { URL.revokeObjectURL(old); } catch(e) {} }, 2000);
            }
            liveFrameSeq = seq || (liveFrameSeq + 1);
          }
          await new Promise(r => setTimeout(r, 150));
        }
      } finally {
        livePumping = false;
      }
    }

    /* 坐标换算: 显示坐标 -> 页面真实视口坐标
       注意: 画面帧会被 screencast 缩放到 maxWidth/maxHeight, 所以绝不能用图片尺寸当基准,
       必须用页面 innerWidth/innerHeight, 否则点击整体偏移(曾导致输入框点不中、打不进字) */
    function livePoint(e) {
      const img = liveImg();
      const vw = liveViewport.w || 1280;
      const vh = liveViewport.h || 800;
      if (!img) return {x: 0, y: 0, w: vw, h: vh};
      const rect = img.getBoundingClientRect();
      const x = Math.round((e.clientX - rect.left) * vw / Math.max(1, rect.width));
      const y = Math.round((e.clientY - rect.top) * vh / Math.max(1, rect.height));
      return {
        x: Math.max(0, Math.min(vw - 1, x)),
        y: Math.max(0, Math.min(vh - 1, y)),
        w: vw, h: vh
      };
    }

    /* ===== 全屏登录台 ===== */
    let liveOverlayOpen = false;

    function openLiveOverlay() {
      const ov = document.getElementById('live-overlay');
      if (!ov) return;
      ov.style.display = 'flex';
      liveOverlayOpen = true;
      bindLiveInput();
      bindLiveKeys();
      focusLive();
      pumpLiveFrames();
    }

    function closeLiveOverlay() {
      const ov = document.getElementById('live-overlay');
      if (ov) ov.style.display = 'none';
      liveOverlayOpen = false;
      const sb = document.getElementById('live-start-btn');
      if (sb) sb.disabled = false;
    }

    function refreshLiveNow() {
      liveFrameSeq = 0;
      pumpLiveFrames();
    }

    function toggleLiveZoom() {
      const img = liveImg();
      const btn = document.getElementById('live-zoom-btn');
      if (!img) return;
      if (liveZoom === 'fit') {
        liveZoom = '100%';
        img.style.maxWidth = 'none';
        img.style.maxHeight = 'none';
        if (btn) btn.innerText = '适应屏幕';
      } else {
        liveZoom = 'fit';
        img.style.maxWidth = '100vw';
        img.style.maxHeight = 'calc(100vh - 132px)';
        if (btn) btn.innerText = '100%';
      }
    }

    function showRipple(e) {
      const img = liveImg();
      const rp = document.getElementById('click-ripple');
      if (!img || !rp) return;
      const rect = img.getBoundingClientRect();
      rp.style.left = (e.clientX - rect.left) + 'px';
      rp.style.top = (e.clientY - rect.top) + 'px';
      rp.style.display = 'block';
      setTimeout(() => { rp.style.display = 'none'; }, 400);
    }

    async function sendLiveInput(ev) {
      if (!activeAccountName) return;
      try {
        const res = await fetch('/api/auth/live/input', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(Object.assign({name: activeAccountName}, ev))
        });
        return await res.json();
      } catch(e) { return {status: 'error', message: String(e)}; }
    }

    let liveLastMoveAt = 0;
    let liveMouseDown = false;

    function bindLiveInput() {
      const img = liveImg();
      if (!img || img.dataset.bound) return;
      img.dataset.bound = '1';

      img.addEventListener('mousemove', (e) => {
        const now = Date.now();
        if (now - liveLastMoveAt < 60) return;
        liveLastMoveAt = now;
        const p = livePoint(e);
        sendLiveInput({kind: 'mouse', type: 'mouseMoved', x: p.x, y: p.y});
      });

      img.addEventListener('mousedown', (e) => {
        e.preventDefault();
        focusLive();
        showRipple(e);
        const p = livePoint(e);
        liveMouseDown = true;
        sendLiveInput({kind: 'mouse', type: 'mousePressed', x: p.x, y: p.y,
                       button: e.button === 2 ? 'right' : 'left', clickCount: 1});
      });

      img.addEventListener('mouseup', (e) => {
        if (!liveMouseDown) return;
        liveMouseDown = false;
        const p = livePoint(e);
        sendLiveInput({kind: 'mouse', type: 'mouseReleased', x: p.x, y: p.y,
                       button: e.button === 2 ? 'right' : 'left', clickCount: 1});
      });

      img.addEventListener('contextmenu', (e) => e.preventDefault());

      img.addEventListener('wheel', (e) => {
        e.preventDefault();
        const p = livePoint(e);
        sendLiveInput({kind: 'mouse', type: 'mouseWheel', x: p.x, y: p.y,
                       deltaX: e.deltaX, deltaY: e.deltaY});
      }, {passive: false});

      /* 触屏 (手机/平板): 转换为鼠标事件 */
      img.addEventListener('touchstart', (e) => {
        const t = e.touches[0];
        if (!t) return;
        e.preventDefault();
        focusLive();
        const p = livePoint(t);
        liveMouseDown = true;
        sendLiveInput({kind: 'mouse', type: 'mousePressed', x: p.x, y: p.y, button: 'left', clickCount: 1});
      }, {passive: false});

      img.addEventListener('touchend', (e) => {
        const t = e.changedTouches[0];
        if (!t) return;
        e.preventDefault();
        const p = livePoint(t);
        liveMouseDown = false;
        sendLiveInput({kind: 'mouse', type: 'mouseReleased', x: p.x, y: p.y, button: 'left', clickCount: 1});
      }, {passive: false});
    }

    const LIVE_KEYS = {
      Enter:     {key: 'Enter',     code: 'Enter',     vk: 13, text: '\\r'},
      Backspace: {key: 'Backspace', code: 'Backspace', vk: 8},
      Tab:       {key: 'Tab',       code: 'Tab',       vk: 9},
      Escape:    {key: 'Escape',    code: 'Escape',    vk: 27},
      Delete:    {key: 'Delete',    code: 'Delete',    vk: 46},
      ArrowLeft: {key: 'ArrowLeft', code: 'ArrowLeft', vk: 37},
      ArrowUp:   {key: 'ArrowUp',   code: 'ArrowUp',   vk: 38},
      ArrowRight:{key: 'ArrowRight',code: 'ArrowRight',vk: 39},
      ArrowDown: {key: 'ArrowDown', code: 'ArrowDown', vk: 40},
      Home:      {key: 'Home',      code: 'Home',      vk: 36},
      End:       {key: 'End',       code: 'End',       vk: 35}
    };

    function focusLive() {
      const box = document.getElementById('screen-container');
      if (box) { try { box.focus({preventScroll: true}); } catch(e) { box.focus(); } }
    }

    function bindLiveKeys() {
      if (document.body.dataset.livekeys) return;
      document.body.dataset.livekeys = '1';

      const inLocalField = (el) => {
        const tag = (el && el.tagName) || '';
        return tag === 'INPUT' || tag === 'TEXTAREA' || (el && el.isContentEditable);
      };

      document.addEventListener('keydown', (e) => {
        if (!liveOverlayOpen) return;
        if (e.key === 'Escape') { e.preventDefault(); closeLiveOverlay(); return; }
        if (inLocalField(e.target)) return;          // 本地输入框里的按键交给它自己
        if (e.ctrlKey || e.metaKey || e.altKey) return;
        const k = LIVE_KEYS[e.key];
        if (k) {
          e.preventDefault();
          sendLiveInput({kind: 'key', type: 'keyDown', key: k.key, code: k.code,
                         vk: k.vk, text: k.text || '', press: true});
          return;
        }
        if (e.key && e.key.length === 1) {
          e.preventDefault();
          sendLiveInput({kind: 'text', text: e.key});
        }
      });

      document.addEventListener('paste', (e) => {
        if (!liveOverlayOpen || inLocalField(e.target)) return;
        const txt = (e.clipboardData || window.clipboardData).getData('text');
        if (txt) { e.preventDefault(); sendLiveInput({kind: 'text', text: txt}); }
      });
    }

    async function sendLiveText() {
      const inp = document.getElementById('live-text-input');
      if (!inp) return;
      const t = inp.value;
      if (!t) return;
      inp.value = '';
      const r = await sendLiveInput({kind: 'text', text: t});
      focusLive();
    }

    async function finishLiveLogin() {
      if (!activeAccountName) return;
      const statusEl = document.getElementById('login-pwd-status');
      statusEl.innerHTML = '<span style="color:var(--primary)">⏳ 正在抓取 Cookie 并换取 Token...</span>';
      try {
        await fetch('/api/auth/live/finish', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: activeAccountName})
        });
        if (!livePollTimer) livePollTimer = setInterval(pollLiveLogin, 1200);
      } catch(e) {
        statusEl.innerHTML = `<span style="color:var(--danger)">❌ 抓取请求失败: ${e}</span>`;
      }
    }

    /* ===== 自动重登凭据 (账号密码) ===== */
    function initCredBox(acc) {
      const cur = acc || (currentAccounts || []).find(x => x.name === activeAccountName) || {};
      const box = document.getElementById('cred-box');
      if (box) box.open = !cur.has_saved_password;   // 没存过就默认展开, 免得用户找不到
      const uEl = document.getElementById('cred-username');
      const pEl = document.getElementById('cred-password');
      const st = document.getElementById('cred-state');
      if (uEl) uEl.value = cur.saved_username || '';
      if (pEl) pEl.value = '';
      if (st) {
        st.innerText = cur.has_saved_password
          ? '已保存（会话过期可自动重登）'
          : '未保存（会话过期需要手动登录）';
        st.style.color = cur.has_saved_password ? '#4ade80' : '#94a3b8';
      }
    }

    async function saveCredential() {
      if (!activeAccountName) return toast('请先选择账号');
      const u = (document.getElementById('cred-username').value || '').trim();
      const p = document.getElementById('cred-password').value || '';
      if (!u || !p) return toast('请填写账号邮箱与密码');
      const btn = document.getElementById('cred-save-btn');
      if (btn) btn.disabled = true;
      try {
        const res = await fetch('/api/auth/credential/save', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: activeAccountName, username: u, password: p})
        });
        const d = await res.json();
        if (d.status !== 'ok') throw new Error(d.message || '保存失败');
        document.getElementById('cred-password').value = '';
        const st = document.getElementById('cred-state');
        if (st) { st.innerText = '已保存（会话过期可自动重登）'; st.style.color = '#4ade80'; }
        toast('凭据已保存，后续会话过期会自动用账号密码重登');
        fetchStatus();
      } catch(e) {
        toast('保存失败: ' + (e.message || e));
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function deleteCredential() {
      if (!activeAccountName) return;
      if (!confirm('确定删除该账号已保存的登录凭据吗？删除后会话过期只能手动登录。')) return;
      try {
        await fetch('/api/auth/credential/delete', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: activeAccountName})
        });
        const st = document.getElementById('cred-state');
        if (st) { st.innerText = '未保存（会话过期需要手动登录）'; st.style.color = '#94a3b8'; }
        toast('已删除保存的凭据');
        fetchStatus();
      } catch(e) { toast('删除失败: ' + e); }
    }

    async function closeLiveLogin() {
      if (!activeAccountName) return;
      if (!confirm('确定关闭当前登录会话吗？未完成的登录不会保存。')) return;
      stopLivePolling();
      try {
        await fetch('/api/auth/live/cancel', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: activeAccountName})
        });
      } catch(e) {}
      closeLiveOverlay();
      document.getElementById('live-finish-btn').style.display = 'none';
      document.getElementById('live-close-btn').style.display = 'none';
      document.getElementById('live-start-btn').disabled = false;
      document.getElementById('login-pwd-status').innerHTML = '会话已关闭。';
    }



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
      setupLogScrollListener();
    });
  </script>
  <!-- 全屏手动登录台: 画面按屏幕尺寸铺开, 支持鼠标/键盘/触屏输入 -->
  <div id="live-overlay" style="display:none; position:fixed; inset:0; z-index:9000; background:#060a12;
       flex-direction:column; font-family:inherit;">
    <div style="display:flex; align-items:center; gap:10px; padding:10px 14px; border-bottom:1px solid #1e293b; flex:0 0 auto;">
      <span style="font-weight:700; color:#e2e8f0;">🖥️ 微软登录</span>
      <span id="live-overlay-acc" style="color:#38bdf8; font-size:13px;"></span>
      <span id="live-overlay-egress" style="color:#94a3b8; font-size:12px;"></span>
      <span style="flex:1"></span>
      <button class="btn btn-secondary btn-sm" onclick="toggleLiveZoom()" id="live-zoom-btn">100%</button>
      <button class="btn btn-secondary btn-sm" onclick="refreshLiveNow()">刷新画面</button>
      <button class="btn btn-sm" style="background:#16a34a" onclick="finishLiveLogin()">✅ 我已登录完成</button>
      <button class="btn btn-secondary btn-sm" onclick="closeLiveOverlay()">收起（不关会话）</button>
      <button class="btn btn-sm" style="background:#b91c1c" onclick="closeLiveLogin()">✖ 关闭会话</button>
    </div>
    <div style="flex:1 1 auto; overflow:auto; background:#000; display:flex; align-items:flex-start; justify-content:center;">
      <div id="screen-container" tabindex="0" style="position:relative; outline:none; line-height:0;">
        <img id="live-screen-img" src="" alt="微软登录画面加载中..." draggable="false"
             style="display:block; max-width:100vw; max-height:calc(100vh - 132px); cursor:crosshair; user-select:none;">
        <div id="click-ripple" style="display:none; position:absolute; width:18px; height:18px; border-radius:50%;
             border:2px solid #38bdf8; background:rgba(56,189,248,0.35); pointer-events:none;
             transform:translate(-50%,-50%);"></div>
      </div>
    </div>
    <div style="flex:0 0 auto; padding:8px 14px 12px; border-top:1px solid #1e293b;">
      <div style="display:flex; gap:8px; align-items:center;">
        <input id="live-text-input" type="text" style="flex:1; padding:9px 12px; background:#0b1220; border:1px solid #1e293b;
               border-radius:8px; color:#e2e8f0;"
               placeholder="在这里打字（中文/长文本/粘贴用这里，回车发送）—— 会自动落到页面上当前输入框"
               onkeydown="if(event.key==='Enter'){event.preventDefault();sendLiveText();}">
        <button class="btn btn-sm" onclick="sendLiveText()">发送</button>
      </div>
      <div style="font-size:12px; color:#64748b; margin-top:6px;">
        点画面任意位置即可操作（鼠标已支持移动/点击/滚轮/触屏）；直接敲键盘也可以（Enter/Tab/退格/方向键）；Esc 收起画面。
      </div>
    </div>
  </div>

</body>
</html>
"""


class WebHandler(BaseHTTPRequestHandler):
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

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
            self._handle_logs(u)
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

        if path == '/api/auth/live/status':
            params = urllib.parse.parse_qs(u.query)
            name = params.get('name', [None])[0]
            from msr import live_login
            sess = live_login.get(name) if name else None
            if not sess:
                self._json_resp({'status': 'idle', 'name': name})
            else:
                self._json_resp(sess.status_dict())
            return

        if path == '/api/auth/live/frame':
            params = urllib.parse.parse_qs(u.query)
            name = params.get('name', [None])[0]
            try:
                since = int(params.get('seq', ['0'])[0])
            except (TypeError, ValueError):
                since = 0
            from msr import live_login
            sess = live_login.get(name) if name else None
            if not sess:
                self._json_resp({'status': 'error', 'message': '会话不存在或已结束'}, code=404)
                return
            seq, data = sess.frame(since)
            try:
                if not data:
                    self.send_response(304)
                    self.send_header('X-Frame-Seq', str(seq))
                    self.send_cors()
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
                    self.send_header('X-Frame-Seq', str(seq))
                    self.send_cors()
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
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
            old_proxy = target.get('proxy') if target else None
            new_proxy = payload.get('proxy')

            import re
            schedule_time = (payload.get('schedule_time') or '').strip()
            if target and not schedule_time:
                schedule_time = target.get('schedule_time')
            if not schedule_time or not re.match(r'^([01]\d|2[0-3]):([0-5]\d)$', schedule_time):
                self._json_resp({'status': 'error', 'message': '每日定时执行时间不能为空，格式须为 24小时制 HH:MM (如 02:30)'}, code=400)
                return
            payload['schedule_time'] = schedule_time

            if target:
                target.update(payload)
            else:
                accs.append(payload)
            config.save_accounts(accs)

            # 代理变更时自动清理旧的地理信息缓存，促使系统重新探测新代理出口
            if old_proxy != new_proxy:
                try:
                    geo_f = config.pfile(name, 'proxy_geo.json')
                    if os.path.isfile(geo_f):
                        os.remove(geo_f)
                except Exception:
                    pass

            append_live_log(f'保存账号配置: {name} (每日定时: {schedule_time}, 代理: {new_proxy or "直连"})')
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
            from msr import notify, settings
            append_live_log('触发通知测试推送...')
            cfg = settings.load_settings()
            chs = cfg.get('notify', {}).get('channels', ['stdout'])
            ch_names = {
                'stdout': '控制台',
                'serverchan': 'Server酱',
                'telegram': 'Telegram',
                'webhook': 'Webhook'
            }
            active_names = [ch_names.get(c, c) for c in chs]
            ch_desc = '、'.join(active_names) if active_names else '标准输出'
            notify.notify('MSR-ArmBot Web 控制台测试', f'恭喜！通知通道配置正常，当前启用渠道 [{ch_desc}] 推送成功！', level='info')
            self._json_resp({'status': 'ok', 'message': f'推送测试请求已发送 (渠道: {ch_desc})'})
            return

        if path == '/api/auth/live/start':
            from msr import live_login, config
            name = (payload.get('name') or '').strip()
            if not name:
                self._json_resp({'status': 'error', 'message': '缺少账号参数 name'}, code=400)
                return
            acc = config.get_account(name) or {}
            proxy = acc.get('proxy')
            append_live_log(f'[{name}] 打开微软登录页（手动登录，出口: {proxy or "直连"}）', name='系统')
            try:
                sess = live_login.start_session(name, proxy=proxy)
            except Exception as ex:
                self._json_resp({'status': 'error', 'message': str(ex)[:200]}, code=500)
                return
            self._json_resp({'status': 'ok', 'session': sess.status_dict()})
            return

        if path == '/api/auth/live/input':
            from msr import live_login
            name = payload.get('name')
            sess = live_login.get(name) if name else None
            if not sess:
                self._json_resp({'status': 'error', 'message': '会话不存在'}, code=404)
                return
            self._json_resp(sess.input(payload))
            return

        if path == '/api/auth/live/finish':
            from msr import live_login
            name = payload.get('name')
            sess = live_login.get(name) if name else None
            if not sess:
                self._json_resp({'status': 'error', 'message': '会话不存在'}, code=404)
                return
            append_live_log(f'[{name}] 开始抓取登录凭据 (Cookie + Token)...', name='系统')
            self._json_resp({'status': 'ok', 'session': sess.finish_async()})
            return

        if path == '/api/auth/live/cancel':
            from msr import live_login
            name = payload.get('name')
            closed = live_login.stop_session(name, 'cancelled') if name else False
            if closed:
                append_live_log(f'[{name}] 已关闭手动登录会话', name='系统')
            self._json_resp({'status': 'ok', 'closed': bool(closed)})
            return

        if path == '/api/auth/credential':
            self._handle_credential_login(payload)
            return

        if path == '/api/auth/credential/save':
            from msr import config
            name = (payload.get('name') or '').strip()
            username = (payload.get('username') or '').strip()
            password = payload.get('password') or ''
            if not name or not username or not password:
                self._json_resp({'status': 'error', 'message': '账号标识、邮箱、密码都不能为空'}, code=400)
                return
            if not config.get_account(name):
                self._json_resp({'status': 'error', 'message': '账号不存在: %s' % name}, code=400)
                return
            try:
                config.save_credential(name, username, password)
            except Exception as ex:
                self._json_resp({'status': 'error', 'message': str(ex)[:150]}, code=500)
                return
            append_live_log(f'[{name}] 已保存自动重登凭据 (账号: {username})')
            self._json_resp({'status': 'ok', 'username': username, 'has_saved_password': True})
            return

        if path == '/api/auth/credential/delete':
            from msr import config
            name = payload.get('name')
            if name:
                config.delete_credential(name)
                append_live_log(f'[{name}] 已安全清除保存的 MSR 账号密码凭据')
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
                config.delete_profile(name)
                append_live_log(f'删除账号及独立 Profile 目录: {name}')
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

            # 凭据存储检测
            cred = config.get_credential(name)
            saved_username = (cred.get('username') if cred else None) or a.get('username')
            has_saved_password = bool(cred and cred.get('password'))

            # 探测当前活跃 CDP 端口状态
            p = config.profile(name)
            cdp_port_configured = a.get('cdp_port')
            active_port_file = os.path.join(p, 'chrome', 'DevToolsActivePort')
            with RUNNER_STATE_LOCK:
                is_task_running = name in ACTIVE_RUNNERS

            p_lock = os.path.join(p, 'runner.lock')
            if not is_task_running and os.path.isfile(p_lock):
                try:
                    with open(p_lock, 'r', encoding='utf-8') as f:
                        c = f.read().strip()
                    from msr.runner import is_runner_process_alive
                    if c.isdigit() and is_runner_process_alive(int(c)):
                        is_task_running = True
                except Exception:
                    pass

            if is_task_running:
                real_port = None
                if os.path.isfile(active_port_file):
                    try:
                        with open(active_port_file, 'r', encoding='utf-8') as pf:
                            lines = [l.strip() for l in pf if l.strip()]
                        if lines and lines[0].isdigit():
                            real_port = int(lines[0])
                    except Exception:
                        pass
                cdp_status = {
                    'status': 'running',
                    'port': real_port or cdp_port_configured,
                    'desc': f':{real_port} (运行中)' if real_port else '启动中...'
                }
            elif cdp_port_configured:
                cdp_status = {
                    'status': 'external',
                    'port': cdp_port_configured,
                    'desc': f':{cdp_port_configured} (外部)'
                }
            else:
                cdp_status = {
                    'status': 'idle',
                    'port': None,
                    'desc': '动态闲置'
                }

            detailed.append({
                'name': name,
                'enabled': a.get('enabled', True),
                'schedule_time': a.get('schedule_time'),
                'cdp_status': cdp_status,
                'saved_username': saved_username,
                'has_saved_password': has_saved_password,
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
        max_concurrency = int(sch.get('max_concurrency', 2))
        scheduler_info = {
            'daily_enabled': sch.get('daily_enabled', True),
            'max_concurrency': max_concurrency,
            'status': '运行中' if sch.get('daily_enabled', True) else '已暂停'
        }
        with RUNNER_STATE_LOCK:
            runner_copy = dict(RUNNER_STATE)
            runner_copy['running_count'] = len(ACTIVE_RUNNERS)
            runner_copy['max_concurrency'] = max_concurrency
            runner_copy['active_tasks'] = {k: {'started_at': v['started_at'], 'last_line': v.get('last_line', '')} for k, v in ACTIVE_RUNNERS.items()}
        self._json_resp({
            'accounts': detailed,
            'scheduler': scheduler_info,
            'runner': runner_copy,
            'time': time.strftime('%F %T')
        })

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
            append_live_log(f'[{name}] 开始 MSR 账号全自动登录 (账号: {username})...')
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

        from msr import config, settings
        home_args = ['--home', config.HOME] if getattr(config, 'HOME', None) else []

        if dry:
            cmd = [sys.executable, '-m', 'msr'] + home_args + ['run', name or '--all', '--dry-run']
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            self._json_resp({'status': 'ok', 'output': p.stdout})
            return

        st_cfg = settings.load_settings()
        max_conc = int(st_cfg.get('schedule', {}).get('max_concurrency', 2))

        with RUNNER_STATE_LOCK:
            if name and name != '--all':
                if name in ACTIVE_RUNNERS:
                    append_live_log(f'⚠️ 账号 [{name}] 当前已有任务正在执行中，请勿重复启动')
                    self._json_resp({'status': 'busy', 'message': f'账号 [{name}] 任务正在执行中，请勿重复提交'}, code=409)
                    return
                if len(ACTIVE_RUNNERS) >= max_conc:
                    append_live_log(f'⚠️ 当前并发任务已达上限 ({len(ACTIVE_RUNNERS)}/{max_conc})，拒绝启动账号 [{name}]')
                    self._json_resp({'status': 'busy', 'message': f'当前并发运行任务数已达上限 ({len(ACTIVE_RUNNERS)}/{max_conc})，请等待空闲槽位'}, code=409)
                    return
            else:
                if ACTIVE_RUNNERS:
                    append_live_log(f'⚠️ 当前已有 {len(ACTIVE_RUNNERS)} 个账号正在并发运行中，拒绝执行批量运行')
                    self._json_resp({'status': 'busy', 'message': f'当前已有 {len(ACTIVE_RUNNERS)} 个账号正在并发运行中，请等待完成后再触发批量执行'}, code=409)
                    return

        # 检查底层 runner.lock 文件锁 (细粒度单账号锁与残留核验)
        if name and name != '--all':
            lock_p = os.path.join(config.profile(name), 'runner.lock')
            if os.path.isfile(lock_p):
                try:
                    with open(lock_p, 'r', encoding='utf-8') as f:
                        c = f.read().strip()
                    if c.isdigit():
                        pid = int(c)
                        from msr.runner import is_runner_process_alive
                        if is_runner_process_alive(pid):
                            append_live_log(f'⚠️ 账号 [{name}] 任务进程 (PID: {pid}) 正在执行中')
                            self._json_resp({'status': 'busy', 'message': f'账号 [{name}] 已有进程正在执行中 (PID: {pid})'}, code=409)
                            return
                        else:
                            try:
                                os.remove(lock_p)
                            except Exception:
                                pass
                except Exception:
                    pass

        def bg_runner(target_name):
            with RUNNER_STATE_LOCK:
                ACTIVE_RUNNERS[target_name] = {
                    'started_at': time.time(),
                    'last_line': '正在启动任务...',
                    'proc': None
                }
                _sync_runner_state()

            tag = target_name if target_name != '--all' else '系统'
            append_live_log(f'>>> 开始执行任务: {target_name}', name=tag)
            cmd = [sys.executable, '-m', 'msr'] + home_args + ['run', target_name]
            if st_cfg.get('schedule', {}).get('fast_mode'):
                cmd.append('--fast')
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            with RUNNER_STATE_LOCK:
                if target_name in ACTIVE_RUNNERS:
                    ACTIVE_RUNNERS[target_name]['proc'] = proc
            for line in iter(proc.stdout.readline, ''):
                line = line.strip()
                if line:
                    with RUNNER_STATE_LOCK:
                        if target_name in ACTIVE_RUNNERS:
                            ACTIVE_RUNNERS[target_name]['last_line'] = line
                        _sync_runner_state()
                    append_live_log(line)
            proc.stdout.close()
            proc.wait()
            with RUNNER_STATE_LOCK:
                ACTIVE_RUNNERS.pop(target_name, None)
                _sync_runner_state()
            append_live_log(f'<<< 任务执行完毕 (exit={proc.returncode})', name=tag)

        t = threading.Thread(target=bg_runner, args=(name or '--all',), daemon=True)
        t.start()
        self._json_resp({'status': 'ok', 'message': '后台执行进程已启动'})

    def _handle_logs(self, u=None):
        acc_filter = None
        format_param = 'text'
        if u and getattr(u, 'query', None):
            params = urllib.parse.parse_qs(u.query)
            acc_filter = params.get('acc', [None])[0]
            format_param = params.get('format', ['text'])[0]

        # 增量从磁盘日志同步最新内容到 LIVE_LOGS (去重并保持单一有序数据源)
        sync_logs_from_disk()

        with LIVE_LOGS_LOCK:
            all_lines = list(LIVE_LOGS)

        if acc_filter == '__system__':
            filtered_lines = [l for l in all_lines if '[系统]' in l or '[定时调度]' in l]
        elif acc_filter and acc_filter != 'all':
            acc_tag = f'[{acc_filter}]'
            filtered_lines = [l for l in all_lines if acc_tag in l]
        else:
            filtered_lines = all_lines

        text = '\n'.join(filtered_lines) if filtered_lines else '今日暂无执行日志。'

        if format_param == 'json':
            with RUNNER_STATE_LOCK:
                st_copy = dict(RUNNER_STATE)
            self._json_resp({
                'status': 'ok',
                'runner': st_copy,
                'text': text,
                'lines_count': len(filtered_lines),
                'timestamp': time.time(),
            })
            return

        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_cors()
            self.end_headers()
            self.wfile.write(text.encode('utf-8'))
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _json_resp(self, data, code=200):
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_cors()
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True


def _start_scheduler_daemon():
    """轻量级内置每日定时调度引擎。

    各账号在 Web 控制台独立设置每日定时时间 (HH:MM)，到点后系统自动加上随机抖动延迟，
    并在并发上限 (max_concurrency) 保护下安全拉起执行。
    """
    triggered_today = {}  # map name -> today_str

    def scheduler_loop():
        nonlocal triggered_today
        import subprocess
        from msr import settings, config
        while True:
            try:
                time.sleep(20)
                st = settings.load_settings()
                sch = st.get('schedule', {})
                if not sch.get('daily_enabled', sch.get('enabled', True)):
                    continue

                now_hm = time.strftime('%H:%M')
                today_str = time.strftime('%Y-%m-%d')

                # 清理过期日期的触发记录
                triggered_today = {k: v for k, v in triggered_today.items() if v == today_str}

                accs = [a for a in config.accounts() if a.get('enabled', True)]
                max_conc = int(sch.get('max_concurrency', 2))
                jitter_max = int(sch.get('random_start_delay_max', 25))
                is_fast = bool(sch.get('fast_mode', False))

                for a in accs:
                    name = a.get('name')
                    sch_time = a.get('schedule_time')
                    if not name or not sch_time:
                        continue
                    if sch_time == now_hm and triggered_today.get(name) != today_str:
                        triggered_today[name] = today_str

                        def _spawn_task(acc_name, target_time):
                            if jitter_max > 0 and not is_fast:
                                jitter_sec = random.uniform(0, jitter_max * 60)
                                append_live_log(f'[{acc_name}] 到达预定时间 {target_time}，开启随机抖动等待 {jitter_sec / 60:.1f} 分钟后开始执行...', name='定时调度')
                                time.sleep(jitter_sec)

                            # 槽位等待与防爆保护
                            while True:
                                with RUNNER_STATE_LOCK:
                                    if len(ACTIVE_RUNNERS) < max_conc and acc_name not in ACTIVE_RUNNERS:
                                        ACTIVE_RUNNERS[acc_name] = {
                                            'started_at': time.time(),
                                            'last_line': '定时任务唤醒启动中...',
                                            'proc': None
                                        }
                                        _sync_runner_state()
                                        break
                                time.sleep(5)

                            append_live_log(f'[{acc_name}] 开始执行每日定时自动化任务流程...', name='定时调度')
                            home_args = ['--home', config.HOME] if getattr(config, 'HOME', None) else []
                            cmd = [sys.executable, '-m', 'msr'] + home_args + ['run', acc_name]
                            if is_fast:
                                cmd.append('--fast')
                            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                            with RUNNER_STATE_LOCK:
                                if acc_name in ACTIVE_RUNNERS:
                                    ACTIVE_RUNNERS[acc_name]['proc'] = proc
                            for line in iter(proc.stdout.readline, ''):
                                line = line.strip()
                                if line:
                                    with RUNNER_STATE_LOCK:
                                        if acc_name in ACTIVE_RUNNERS:
                                            ACTIVE_RUNNERS[acc_name]['last_line'] = line
                                        _sync_runner_state()
                                    append_live_log(line)
                            proc.stdout.close()
                            proc.wait()
                            with RUNNER_STATE_LOCK:
                                ACTIVE_RUNNERS.pop(acc_name, None)
                                _sync_runner_state()
                            append_live_log(f'[{acc_name}] 每日定时任务执行完毕 (exit={proc.returncode})', name='定时调度')

                        t = threading.Thread(target=_spawn_task, args=(name, sch_time), daemon=True, name=f'Scheduler-{name}')
                        t.start()

            except Exception as ex:
                append_live_log(f'定时调度异常: {ex}', name='定时调度')

    t = threading.Thread(target=scheduler_loop, daemon=True, name='DailyScheduler')
    t.start()


def _init_reaper():
    """在 Linux / Docker (PID 1) 环境自动回收孤儿子进程，防止 Chromium/Crashpad 僵尸进程。"""
    try:
        import signal
        def _sigchld_handler(signum, frame):
            while True:
                try:
                    pid, _ = os.waitpid(-1, os.WNOHANG)
                    if pid <= 0:
                        break
                except (ChildProcessError, OSError):
                    break
        signal.signal(signal.SIGCHLD, _sigchld_handler)
    except Exception:
        pass


def run_web(host='0.0.0.0', port=8080):
    """启动轻量级 Web 控制台服务器。"""
    _init_reaper()
    try:
        from msr import live_login
        cleaned = live_login.reap_orphans()
        if cleaned:
            append_live_log('启动自愈: 清理孤儿登录会话 %s' % cleaned, name='系统')
    except Exception as _ex:
        append_live_log('启动自愈失败(忽略): %s' % _ex, name='系统')
    sync_logs_from_disk(force=True)
    addr = (host, int(port))
    server = ThreadingHTTPServer(addr, WebHandler)
    _start_scheduler_daemon()
    print(f'MSR-ArmBot Web 控制台已启动: http://{host}:{port}', flush=True)
    append_live_log(f'Web 控制台服务已启动于 http://{host}:{port}', name='系统')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nWeb 控制台正在关闭...', flush=True)
        server.shutdown()


start_server = run_web
