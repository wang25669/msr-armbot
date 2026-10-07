"""浏览器层: chromium 生命周期 + CDP + Edge 伪装 + cookie 注入。

全部参数化(端口/UA/user-data-dir), 无 X11/xdotool 依赖 —— 纯 headless。
CDP 类从现网 bing_daily_v2.py 提取(含断线重连/断连恢复, 2026-09-19 教训)。
"""
import base64
import json
import os
import random
import socket
import struct
import subprocess
import time
import urllib.parse
import urllib.request

WS_TIMEOUT = 60


class StepError(Exception):
    pass


def _http_json(url, method='GET', timeout=15, headers=None):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {'_raw': raw}


class CDP:
    """stdlib-only CDP over WebSocket(含断线重连+会话恢复)。"""

    def __init__(self, ws_url):
        self.ws_url = ws_url
        self._id = 0
        u = urllib.parse.urlparse(ws_url)
        self.host = u.hostname
        self.port = u.port or 80
        self.path = u.path + (('?' + u.query) if u.query else '')
        self.sock = None
        self._rest = b''
        self.event_handler = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *a):
        self.close()

    def connect(self):
        s = socket.create_connection((self.host, self.port), timeout=WS_TIMEOUT)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f'GET {self.path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n'
               'Upgrade: websocket\r\nConnection: Upgrade\r\n'
               f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n')
        s.sendall(req.encode())
        buf = b''
        while b'\r\n\r\n' not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise StepError('CDP WebSocket 握手中断')
            buf += chunk
        if b'101' not in buf.split(b'\r\n')[0]:
            raise StepError('CDP 握手失败')
        self.sock = s
        self._rest = buf.split(b'\r\n\r\n', 1)[1]

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _reconnect(self):
        self.close()
        for attempt in range(3):
            try:
                self.connect()
                for m in ('Page.enable', 'Runtime.enable'):
                    try:
                        self._call_once(m, timeout=15)
                    except Exception:
                        pass
                return True
            except Exception:
                time.sleep(2)
        return False

    def _send_frame(self, payload):
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        n = len(payload)
        hdr = b'\x81'
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < (1 << 16):
            hdr += bytes([0x80 | 126]) + struct.pack('>H', n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack('>Q', n)
        self.sock.sendall(hdr + mask + masked)

    def _recv_exact(self, n):
        out = self._rest[:n]
        self._rest = self._rest[n:]
        while len(out) < n:
            chunk = self.sock.recv(min(65536, n - len(out)))
            if not chunk:
                raise StepError('CDP 连接被对端关闭')
            out += chunk
        return out

    def _recv_frame(self):
        b0, b1 = self._recv_exact(2)
        opcode = b0 & 0x0F
        ln = b1 & 0x7F
        if ln == 126:
            ln = struct.unpack('>H', self._recv_exact(2))[0]
        elif ln == 127:
            ln = struct.unpack('>Q', self._recv_exact(8))[0]
        data = self._recv_exact(ln)
        if opcode == 0x8:
            raise StepError('CDP 连接被关闭')
        if opcode == 0x9:
            self.sock.sendall(b'\x8a\x80' + os.urandom(4))
            return None
        return data

    def _call_once(self, method, params=None, timeout=WS_TIMEOUT):
        self._id += 1
        mid = self._id
        self._send_frame(json.dumps({'id': mid, 'method': method,
                                     'params': params or {}}).encode())
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.sock.settimeout(max(1, deadline - time.time()))
            frame = self._recv_frame()
            if frame is None:
                continue
            try:
                obj = json.loads(frame.decode())
            except Exception:
                continue
            if obj.get('method') and self.event_handler:
                self.event_handler(obj)
            if obj.get('id') == mid:
                if 'error' in obj:
                    raise StepError(f'CDP {method} 出错: {obj["error"]}')
                return obj.get('result', {})
        raise StepError(f'CDP {method} 超时')

    def call(self, method, params=None, timeout=WS_TIMEOUT):
        try:
            return self._call_once(method, params, timeout)
        except (BrokenPipeError, ConnectionResetError, OSError, socket.error) as e:
            if not self._reconnect():
                raise StepError(f'CDP 调用失败且重连无效: {method}: {e}')
            try:
                return self._call_once(method, params, timeout)
            except Exception as e2:
                raise StepError(f'CDP {method} 重连后仍失败: {e2}')
        except StepError as e:
            if '连接被关闭' in str(e) and self._reconnect():
                return self._call_once(method, params, timeout)
            raise


def ev(cdp, expr, timeout=WS_TIMEOUT):
    r = cdp.call('Runtime.evaluate', {'expression': expr, 'awaitPromise': True,
                                      'returnByValue': True, 'userGesture': True},
                 timeout=timeout)
    if r.get('exceptionDetails'):
        d = r['exceptionDetails']
        msg = (d.get('exception') or {}).get('description') or d.get('text')
        raise StepError(f'JS 执行异常: {str(msg)[:200]}')
    return (r.get('result') or {}).get('value')


def navigate(cdp, url):
    cdp.call('Page.navigate', {'url': url})
    time.sleep(3)
    for _ in range(40):
        try:
            if ev(cdp, 'document.readyState') == 'complete':
                break
        except StepError:
            break
        time.sleep(0.5)
    time.sleep(random.uniform(1.5, 3.5))


# ---------- Edge 伪装(可配置版本号, 默认随 chromium 版本走) ----------

def edge_fingerprint(version='141'):
    brands = [{'brand': 'Chromium', 'version': version},
              {'brand': 'Microsoft Edge', 'version': version},
              {'brand': 'Not/A)Brand', 'version': '99'}]
    fullver = [{'brand': 'Chromium', 'version': f'{version}.0.0.0'},
               {'brand': 'Microsoft Edge', 'version': f'{version}.0.0.0'},
               {'brand': 'Not/A)Brand', 'version': '99.0.0.0'}]
    ua = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
          f'Chrome/{version}.0.0.0 Safari/537.36 Edg/{version}.0.0.0')
    return ua, brands, fullver


def disguise_edge(cdp, version='141', log=print):
    ua, brands, fullver = edge_fingerprint(version)
    he_patch = ('(() => { try { const P = Object.getPrototypeOf(navigator.userAgentData);'
                ' const orig = P.getHighEntropyValues;'
                ' Object.defineProperty(P, "getHighEntropyValues", {value: function(h) {'
                ' return orig.call(this, h).then(r => { r.fullVersionList = %s; return r; });'
                '}, configurable: true, writable: true}); } catch(e) {} })()'
                % json.dumps(fullver))
    try:
        cdp.call('Emulation.setUserAgentOverride', {
            'userAgent': ua,
            'userAgentMetadata': {'brands': brands, 'fullVersion': f'{version}.0.0.0',
                                  'platform': 'Windows', 'platformVersion': '15.0.0',
                                  'architecture': 'x86', 'model': '', 'mobile': False}})
    except Exception as ex:
        log(f'Edge伪装UA容忍: {ex}')
    try:
        cdp.call('Page.addScriptToEvaluateOnNewDocument', {'source': he_patch})
    except Exception as ex:
        log(f'Edge伪装高熵容忍: {ex}')


def verify_disguise(cdp):
    """自检: 高熵 fullVersionList 必须是 Edge。"""
    v = ev(cdp, '(async()=>{try{'
                'if(!navigator.userAgentData) return "NO-UAD";'
                'const h = await navigator.userAgentData'
                '.getHighEntropyValues(["fullVersionList"]);'
                'return JSON.stringify(h.fullVersionList)'
                '}catch(e){return "ERR:"+e}})()')
    return 'Microsoft Edge' in (v or '')


# ---------- chromium 生命周期 ----------

class Chromium:
    """headless chromium 实例管理(每账号一个, 用完即停)。"""

    def __init__(self, binary='chromium', port=None, user_data_dir=None,
                 extra_args=None, version='141', log=print):
        self.binary = binary
        self.port = port or self._free_port()
        self.user_data_dir = user_data_dir
        self.version = version
        self.extra_args = extra_args or []
        self.log = log
        self.proc = None

    @staticmethod
    def _free_port():
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        p = s.getsockname()[1]
        s.close()
        return p

    def start(self):
        args = [self.binary,
                '--headless=new',
                f'--remote-debugging-port={self.port}',
                f'--user-data-dir={self.user_data_dir}',
                '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
                '--no-first-run', '--no-default-browser-check',
                '--disable-background-networking', '--disable-sync',
                '--window-size=1280,900'] + self.extra_args
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                tabs = _http_json(f'http://127.0.0.1:{self.port}/json/list', timeout=3)
                if tabs:
                    return tabs
            except Exception:
                pass
            time.sleep(0.5)
        raise StepError('chromium 启动超时(CDP 无响应)')

    def new_tab(self, url='about:blank'):
        t = _http_json(f'http://127.0.0.1:{self.port}/json/new?{url}', method='PUT')
        return t

    def close_tab(self, tid):
        try:
            _http_json(f'http://127.0.0.1:{self.port}/json/close/{tid}', method='GET')
        except Exception:
            pass

    def attach(self, tab):
        cdp = CDP(tab['webSocketDebuggerUrl'])
        cdp.connect()
        cdp.call('Page.enable')
        cdp.call('Runtime.enable')
        disguise_edge(cdp, self.version, self.log)
        return cdp

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None


# ---------- cookie 注入(B1 登录方案) ----------

def inject_cookies(cdp, cookies, log=print):
    """cookies: [{name,value,domain,path,...}] (Cookie-Editor 导出格式兼容)。

    在导航前调用。注入后首个导航生效; httpOnly cookie 必须由本接口写入
    (JS document.cookie 写不了 httpOnly)。
    """
    ok = 0
    for c in cookies:
        params = {'name': c['name'], 'value': c.get('value', ''),
                  'domain': c.get('domain') or '.bing.com',
                  'path': c.get('path') or '/',
                  'secure': bool(c.get('secure', True)),
                  'httpOnly': bool(c.get('httpOnly', False))}
        if c.get('expirationDate'):
            params['expires'] = c['expirationDate']
        try:
            r = cdp.call('Network.setCookie', params)
            if r.get('success'):
                ok += 1
        except Exception as ex:
            log(f'cookie {c["name"]} 注入容忍: {str(ex)[:60]}')
    cdp.call('Network.enable')
    log(f'cookie 注入 {ok}/{len(cookies)}')
    return ok
