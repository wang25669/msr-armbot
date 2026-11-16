# -*- coding: utf-8 -*-
"""连续打卡(streak)服务端状态读取 —— 一步判定"有没有下发 / 今天做完没有 / 要不要激活"。

数据源: `https://rewards.bing.com/earn` 页面(Next.js App Router)随 HTML 下发的 **RSC payload**。
2026-11-11 实测: /dapi/me 的 promotions 里**没有**视觉/每日活动/Edge 的打卡对象, 但这五个打卡
全部在 payload 里, 每个都是独立对象 `{"type":"streak","model":{"streak":{...}}}`, 按 `partner` 区分:

    partner       含义            完成口径(实测字段)
    bing          必应搜索打卡    activitiesCompleted/activitiesTotal = 1/1 (搜索 1 次)
    dailyset      每日活动        activitiesCompleted/activitiesTotal = 3/3
    edge          Edge 浏览打卡   activitiesCompleted/activitiesTotal = 30/30 (**分钟**)
    bingapp       必应 App 签到   activitiesCompleted/activitiesTotal = 1/1
    visualsearch  视觉搜索打卡    activitiesCompleted/activitiesTotal = 1/1

共同字段: isCurrentDayCompleted(今日是否完成)、completedDays/currentDay/totalDays(连续天数)、
isEnabled(是否下发可用)、destinationUrl(入口)、activationOfferId/activationActivityType(需要激活时用)。

所以 dailyset / edge / visual 这三类都能**一次 HTTP GET 判完**, 不需要开浏览器扫 DOM。
注意: 整份 cookie 塞 Cookie 头会到 42KB, Bing 直接回 400; 只发 bing.com 域(约 4~60 枚)即可。
"""
import re
import urllib.request

from msr import config as _cfg

EARN = 'https://rewards.bing.com/earn'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0')

PARTNERS = ('bing', 'dailyset', 'edge', 'bingapp', 'visualsearch')

_MODEL_RE = re.compile(r'"model"\s*:\s*\{\s*"streak"\s*:\s*\{')
_FIELDS = ('partner', 'activitiesCompleted', 'activitiesTotal', 'isCurrentDayCompleted',
           'completedDays', 'currentDay', 'totalDays', 'isEnabled', 'activityTitle',
           'activitiesLabel', 'destinationUrl', 'activationOfferId', 'activationActivityType')


def _walk(s, start):
    """从 depth=0 的 '{' 出发返回配对 '}' 之后的位置(字符串内的括号跳过)。"""
    depth, in_str, esc = 0, False, False
    for k in range(start, min(len(s), start + 6000)):
        ch = s[k]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return k + 1
    return -1


def _field(obj, key):
    m = re.search(r'"%s"\s*:\s*("(?:[^"\\]|\\.)*"|true|false|-?\d+)' % key, obj)
    if not m:
        return None
    v = m.group(1)
    if v.startswith('"'):
        return v[1:-1].replace('\\\\u0026', '&').replace('\\u0026', '&')
    if v in ('true', 'false'):
        return v == 'true'
    return int(v)


def parse_streaks(html):
    """payload -> {partner: 规范化 dict}。空/坏输入返回 None(视为读取失败)。"""
    if not html:
        return None
    h = html.replace('\\"', '"')
    out = {}
    for m in _MODEL_RE.finditer(h):
        start = m.end() - 1
        end = _walk(h, start)
        if end < 0:
            continue
        obj = h[start:end]
        partner = _field(obj, 'partner')
        if not partner:
            continue
        done = _field(obj, 'activitiesCompleted') or 0
        total = _field(obj, 'activitiesTotal') or 0
        out[partner] = {
            'partner': partner,
            'completed': done,
            'total': total,
            'complete': bool(total > 0 and done >= total),
            'today_done': _field(obj, 'isCurrentDayCompleted'),
            'enabled': _field(obj, 'isEnabled'),
            'completed_days': _field(obj, 'completedDays'),
            'current_day': _field(obj, 'currentDay'),
            'total_days': _field(obj, 'totalDays'),
            'entry': _field(obj, 'destinationUrl'),
            'activation_offer': _field(obj, 'activationOfferId'),
            'activation_type': _field(obj, 'activationActivityType'),
            'activity_title': _field(obj, 'activityTitle'),
        }
    return out


def fetch_streaks(name, proxy=None, timeout=25, stats=None):
    """一次 GET /earn 取全部 streak 状态。网络/解析失败返回 None(调用方回退旧路径)。

    `stats` 可选: 传入 dict 时填充 {'status', 'bytes', 'partners'} 供诊断日志用,
    让"读取失败"和"页面确实没有该对象"在日志里可区分(2026-11-13 事故的直接教训)。
    """
    if stats is not None:
        stats.setdefault('status', 'init')
        stats.setdefault('bytes', 0)
        stats.setdefault('partners', [])
    cookies = _cfg.load_json(_cfg.pfile(name, 'cookie.json'), []) or []
    bing = [c for c in cookies if 'bing.com' in str(c.get('domain') or '')]
    ck = '; '.join('%s=%s' % (c.get('name'), c.get('value'))
                   for c in (bing or cookies) if c.get('name') and c.get('value'))
    if not ck:
        if stats is not None:
            stats['status'] = 'no-cookie'
        return None
    req = urllib.request.Request(EARN, headers={
        'Cookie': ck, 'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9'})
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
    try:
        resp = urllib.request.build_opener(*handlers).open(req, timeout=timeout)
        raw = resp.read()
        if stats is not None:
            stats['status'] = getattr(resp, 'status', 'ok')
            stats['bytes'] = len(raw)
    except Exception as ex:
        if stats is not None:
            stats['status'] = '%s: %s' % (type(ex).__name__, str(ex)[:60])
        return None
    out = parse_streaks(raw.decode('utf-8', 'ignore'))
    if stats is not None and out:
        stats['partners'] = sorted(out.keys())
    return out


def is_done(streak):
    """打卡是否今日已完成(服务端口径)。streak 为 None/缺失时返回 False。"""
    if not streak:
        return False
    if streak.get('complete'):
        return True
    return streak.get('today_done') is True


def describe(streak):
    """日志用的一行描述。"""
    if not streak:
        return '未下发'
    return '%s/%s%s' % (streak.get('completed'), streak.get('total'),
                        '(今日已完成)' if is_done(streak) else '')

def reward_day(now=None):
    """当前奖励日(美西日期 YYYY-MM-DD)。微软 Rewards 的"每天"以美西 0 点切日。

    2026-11-11 实测: 美西 00:20 时 /earn 的 dailyset 已从 3/3 回到 0/3, 说明日界按美西。
    用于"同一天不要重复上报 Edge 30 分钟"的去重键。
    """
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo('America/Los_Angeles')).strftime('%Y-%m-%d')
    except Exception:
        # 无 tzdata 时退化为固定 UTC-8 (PST); DST 期间会有 1 小时误差, 只影响日界附近
        import time as _t
        return _t.strftime('%Y-%m-%d', _t.gmtime(_t.time() - 8 * 3600))
