"""通知模块(预留)。默认 stdout+日志; 可插 Telegram/bark/自定义 webhook。

配置: $MSR_HOME/notify.json
{
  "channels": ["stdout"],                  # stdout | telegram | webhook
  "telegram": {"bot_token": "...", "chat_id": "..."},
  "webhook":  {"url": "...", "method": "POST"},
  "on":       ["error"]                    # error | summary | both
}
"""
import json
import urllib.request


def notify(title, text, level='summary'):
    from msr import config
    cfg = config.load_json(config.HOME + '/notify.json', {}) or {}
    channels = cfg.get('channels', ['stdout'])
    on = cfg.get('on', ['error', 'summary'])
    if level not in on and 'both' not in on:
        return
    for ch in channels:
        try:
            if ch == 'stdout':
                print('[notify] %s: %s' % (title, text), flush=True)
            elif ch == 'telegram':
                _tg(cfg['telegram'], title, text)
            elif ch == 'webhook':
                _wh(cfg['webhook'], {'title': title, 'text': text, 'level': level})
        except Exception as ex:
            print('[notify] %s 发送失败: %s' % (ch, str(ex)[:80]), flush=True)


def _tg(tg, title, text):
    url = 'https://api.telegram.org/bot%s/sendMessage' % tg['bot_token']
    req = urllib.request.Request(url, data=json.dumps({
        'chat_id': tg['chat_id'], 'text': '%s\n%s' % (title, text)}).encode(),
        headers={'Content-Type': 'application/json'})
    urllib.request.urlopen(req, timeout=15)


def _wh(wh, payload):
    req = urllib.request.Request(wh['url'], data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'},
                                 method=wh.get('method', 'POST'))
    urllib.request.urlopen(req, timeout=15)
