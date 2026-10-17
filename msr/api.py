"""RewardsPlatform API 层(从 edge_app_streak.py 提炼, 加入重试与降级)。"""
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

API = 'https://prod.rewardsplatform.microsoft.com/dapi'
TOKEN_URL = 'https://login.microsoftonline.com/consumers/oauth2/v2.0/token'
CLIENT_ID = '0000000040170455'       # Bing App 官方 client id
SCOPE = 'service::prod.rewardsplatform.microsoft.com::MBI_SSL'
REDIRECT = 'https://login.live.com/oauth20_desktop.srf'
DEVICE_CODE_URL = 'https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode'

EDGE_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
           'Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0')
APP_UA = ('Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/UQ1A.240205.002; ) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Version/4.0 Chrome/124.0.6367.82 Mobile Safari/537.36 '
          'BingSapphire/31.1.2110003554')
APP_VER = 'SAAndroid/31.1.2110003554'


class AuthError(Exception):
    pass


def _log(name, msg):
    from msr import config
    config.log(name, msg)


def _build_opener(proxy=None):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
    handlers.append(urllib.request.HTTPSHandler())
    return urllib.request.build_opener(*handlers)


def api(name, method, path, token, headers, body=None, retries=3, proxy=None):
    """单次 API 调用, 指数退避重试(5/10/20s)。支持可选代理 proxy。401/403 不重试。"""
    data = json.dumps(body).encode() if body is not None else None
    last = None
    opener = _build_opener(proxy)
    for i in range(retries):
        req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
        try:
            with opener.open(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as ex:
            if ex.code in (401, 403):
                raise
            last = ex
        except (urllib.error.URLError, OSError, socket.timeout) as ex:
            last = ex
        if i < retries - 1:
            wait = 5 * (2 ** i)
            _log(name, 'api %s %s 第%d次失败(%s), %ds后重试' % (
                method, path.split('?')[0], i + 1, str(last)[:60], wait))
            time.sleep(wait)
    raise last



def edge_headers(token, rg):
    c = rg['country']
    return {'authorization': 'Bearer ' + token, 'x-rewards-appid': 'EdgeDesktop',
            'x-rewards-country': c, 'x-rewards-language': rg['language'],
            'x-rewards-partnerid': 'EdgeHub', 'user-agent': EDGE_UA,
            'content-type': 'application/json'}


def app_headers(token, rg):
    c = rg['country'].lower()
    return {'authorization': 'Bearer ' + token, 'x-rewards-partnerid': 'startapp',
            'x-rewards-appid': APP_VER, 'x-rewards-ismobile': 'true',
            'x-rewards-country': c, 'x-rewards-language': rg['language'],
            'x-rewards-flights': 'rwgobig', 'user-agent': APP_UA,
            'content-type': 'application/json; charset=utf-8'}


def get_me(name, token, rg, proxy=None):
    return api(name, 'GET', '/me?channel=edge&options=Profile,Promotions',
               token, edge_headers(token, rg), proxy=proxy)['response']


def find_promo(promos, name):
    for p in promos.get('promotions', []):
        if p.get('name') == name:
            return p.get('attributes', {})
    return None


# ---------- token 生命周期 ----------

def _token_post(name, body, proxy=None):
    req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(body).encode(),
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    opener = _build_opener(proxy)
    with opener.open(req, timeout=30) as r:
        return json.load(r)


def device_code_login(name, proxy=None):
    """device-code 流程: 返回 {device_code, user_code, verification_uri}。
    用户在任意浏览器完成授权后, 用 poll_device_code() 换 token。"""
    d = _token_post(name, {'client_id': CLIENT_ID, 'scope': SCOPE}, proxy=proxy)
    return d


def poll_device_code(name, device_code, interval=5, timeout=300, proxy=None):
    """轮询 token 端点直到用户完成授权。返回 token dict。"""
    t0 = time.time()
    while time.time() < deadline if 'deadline' in locals() else (time.time() - t0 < timeout):
        time.sleep(interval)
        try:
            d = _token_post(name, {'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                                   'client_id': CLIENT_ID, 'device_code': device_code}, proxy=proxy)
            if d.get('refresh_token') or d.get('access_token'):
                return d
            if d.get('error') == 'authorization_pending':
                continue
            if d.get('error') == 'slow_down':
                interval += 5
                continue
            raise AuthError('device code 错误: %s' % d.get('error'))
        except urllib.error.HTTPError as ex:
            body = ex.read().decode()
            try:
                d = json.loads(body)
            except Exception:
                raise AuthError('token 端点异常: %s %s' % (ex.code, body[:120]))
            if d.get('error') == 'authorization_pending':
                continue
            if d.get('error') == 'slow_down':
                interval += 5
                continue
            raise AuthError('授权失败: %s (%s)' % (d.get('error'), d.get('error_description', '')[:100]))
    raise AuthError('等待授权超时(%ds)' % timeout)


def save_token(name, tdict, profile_path):
    """持久化 token(dict, 至少含 refresh_token/access_token)。"""
    from msr import config
    path = profile_path + '/token.json'
    cur = config.load_json(path, {}) or {}
    cur['access_token'] = tdict.get('access_token') or cur.get('access_token')
    if tdict.get('refresh_token'):          # 微软可能不轮换 refresh_token
        cur['refresh_token'] = tdict['refresh_token']
    config.save_json(path, cur)
    os_chmod_quiet(path, 0o600)
    return cur


def os_chmod_quiet(path, mode):
    try:
        import os as _os
        _os.chmod(path, mode)
    except Exception:
        pass


def refresh_access(name, profile_path, rg=None, retries=3, proxy=None):
    """refresh_token 换新 token。3 次重试 + 旧 access_token 降级(教训见 2026-10-07)。支持代理。"""
    from msr import config
    path = profile_path + '/token.json'
    t = config.load_json(path)
    if not t or not t.get('refresh_token'):
        raise AuthError('profile 无 token, 先 msr login %s' % name)
    body = {'grant_type': 'refresh_token', 'client_id': CLIENT_ID,
            'refresh_token': t['refresh_token'], 'scope': SCOPE, 'redirect_uri': REDIRECT}
    last = None
    for i in range(retries):
        try:
            d = _token_post(name, body, proxy=proxy)
            if not d.get('access_token'):
                raise AuthError('refresh 响应无 access_token')
            return save_token(name, d, profile_path)['access_token']
        except urllib.error.HTTPError as ex:
            detail = ex.read().decode()[:120]
            last = ex
            _log(name, 'refresh 第%d次失败: %s %s' % (i + 1, ex.code, detail))
        except Exception as ex:
            last = ex
            _log(name, 'refresh 第%d次失败: %s' % (i + 1, str(ex)[:100]))
        if i < retries - 1:
            time.sleep(20 * (i + 1))
    if rg and t.get('access_token'):
        try:
            get_me(name, t['access_token'], rg, proxy=proxy)
            _log(name, 'refresh 3次全败, 旧 access_token 仍有效, 降级续跑')
            return t['access_token']
        except Exception:
            pass
    raise AuthError('refresh 失败(已重试%d次): %s' % (retries, str(last)[:120]))

