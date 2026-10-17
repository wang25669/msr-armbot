"""通知推送模块。支持 Server酱(Turbo/老版)、Telegram、自定义 Webhook 及标准输出。

配置位于 $MSR_HOME/settings.json 中的 notify 节点。
"""
import json
import urllib.parse
import urllib.request


def notify(title, text, level='summary'):
    """统一通知分发入口。level: error | summary | info"""
    from msr import settings
    cfg = settings.load_settings()
    n_cfg = cfg.get('notify', {})
    channels = n_cfg.get('channels', ['stdout'])
    on = n_cfg.get('on', ['error', 'summary'])

    if level not in on and 'both' not in on and level != 'info':
        return

    for ch in channels:
        try:
            if ch == 'stdout':
                print('[notify] %s: %s' % (title, text), flush=True)
            elif ch == 'serverchan':
                _serverchan(n_cfg.get('serverchan', {}), title, text)
            elif ch == 'telegram':
                _tg(n_cfg.get('telegram', {}), title, text)
            elif ch == 'webhook':
                _wh(n_cfg.get('webhook', {}), {'title': title, 'text': text, 'level': level})
        except Exception as ex:
            print('[notify] %s 发送失败: %s' % (ch, str(ex)[:80]), flush=True)


def _serverchan(sc_cfg, title, text):
    """Server酱推送 (支持 Turbo 版 SCT 及老版 SCU)。"""
    sendkey = (sc_cfg.get('sendkey') or '').strip()
    if not sendkey:
        print('[notify] Server酱未配置 sendkey，跳过推送')
        return

    url_template = sc_cfg.get('url') or 'https://sctapi.ftqq.com/{sendkey}.send'
    if '{sendkey}' in url_template:
        url = url_template.replace('{sendkey}', sendkey)
    else:
        url = url_template

    # 兼容旧版 SCU 域名
    if sendkey.startswith('SCU') and 'sctapi.ftqq.com' in url:
        url = f'https://sc.ftqq.com/{sendkey}.send'

    data = {
        'title': title[:32],
        'desp': text
    }
    ch = sc_cfg.get('channel')
    if ch:
        data['channel'] = str(ch)

    encoded = urllib.parse.urlencode(data).encode('utf-8')
    req = urllib.request.Request(url, data=encoded,
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode('utf-8', errors='ignore')
        try:
            r = json.loads(body)
            # Turbo: code=0; 旧版: errno=0
            if r.get('code') == 0 or r.get('errno') == 0 or r.get('error') == 0:
                print('[notify] Server酱推送成功')
            else:
                msg = r.get('message') or r.get('errmsg') or body
                print(f'[notify] Server酱返回提示: {msg}')
        except Exception:
            print('[notify] Server酱已上报')


def _tg(tg, title, text):
    """Telegram Bot 推送。"""
    token = tg.get('bot_token')
    chat_id = tg.get('chat_id')
    if not token or not chat_id:
        return
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    req = urllib.request.Request(url, data=json.dumps({
        'chat_id': chat_id,
        'text': f'*{title}*\n{text}',
        'parse_mode': 'Markdown'
    }).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        pass


def _wh(wh, payload):
    """自定义 HTTP Webhook 推送。"""
    url = wh.get('url')
    if not url:
        return
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'},
                                 method=wh.get('method', 'POST'))
    with urllib.request.urlopen(req, timeout=15) as resp:
        pass
