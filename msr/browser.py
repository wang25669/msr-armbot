"""浏览器层: chromium 生命周期 + CDP + Edge 伪装 + cookie 注入。

全部参数化(端口/UA/user-data-dir), 无 X11/xdotool 依赖 —— 纯 headless。
CDP 类从现网 bing_daily_v2.py 提取(含断线重连/断连恢复, 2026-09-19 教训)。
"""
import base64
import json
import os
import random
import shutil
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
    brands = [{'brand': 'Microsoft Edge WebView2', 'version': version},
              {'brand': 'Microsoft Edge', 'version': version},
              {'brand': 'Chromium', 'version': version},
              {'brand': 'Not(A:Brand', 'version': '24'}]
    fullver = [{'brand': 'Microsoft Edge WebView2', 'version': f'{version}.0.4283.60'},
               {'brand': 'Microsoft Edge', 'version': f'{version}.0.4283.60'},
               {'brand': 'Chromium', 'version': f'{version}.0.8059.80'},
               {'brand': 'Not(A:Brand', 'version': '24.0.0.0'}]
    ua = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
          f'Chrome/{version}.0.0.0 Safari/537.36 Edg/{version}.0.0.0 MSRewards/Desktop/1.9.0')
    return ua, brands, fullver


def disguise_edge(cdp, version='141', log=print):
    ua, brands, fullver = edge_fingerprint(version)
    he_patch = ('''(() => {
      try {
        const P = Object.getPrototypeOf(navigator.userAgentData);
        const orig = P.getHighEntropyValues;
        Object.defineProperty(P, "getHighEntropyValues", {
          value: function(h) {
            return orig.call(this, h).then(r => {
              r.fullVersionList = %s;
              r.platform = "Windows";
              r.platformVersion = "19.0.0";
              r.architecture = "x86";
              r.model = "";
              r.bitness = "64";
              return r;
            });
          },
          configurable: true,
          writable: true
        });
      } catch(e) {}

      // 覆盖 navigator.platform 为 Win32，消除 Linux/ARM 虚拟化特征
      try {
        Object.defineProperty(navigator, 'platform', {
          get: () => 'Win32',
          configurable: true
        });
      } catch(e) {}

      // 消除 webdriver 自动化特征
      try {
        Object.defineProperty(navigator, 'webdriver', {
          get: () => false,
          configurable: true
        });
      } catch(e) {}

      // 规范硬件核心与内存指纹
      try {
        Object.defineProperty(navigator, 'hardwareConcurrency', {
          get: () => 8,
          configurable: true
        });
        Object.defineProperty(navigator, 'deviceMemory', {
          get: () => 8,
          configurable: true
        });
      } catch(e) {}

      // 抹平 WebGL 渲染器指纹 (防止泄露 llvmpipe / SwiftShader 虚拟化特征)
      try {
        const patchWebGL = (proto) => {
          if (!proto) return;
          const orig = proto.getParameter;
          proto.getParameter = function(param) {
            if (param === 37445) return 'Google Inc. (NVIDIA)';
            if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)';
            return orig.apply(this, arguments);
          };
        };
        if (window.WebGLRenderingContext) patchWebGL(WebGLRenderingContext.prototype);
        if (window.WebGL2RenderingContext) patchWebGL(WebGL2RenderingContext.prototype);
      } catch(e) {}
    })()''' % json.dumps(fullver))
    try:
        cdp.call('Emulation.setUserAgentOverride', {
            'userAgent': ua,
            'userAgentMetadata': {'brands': brands, 'fullVersion': f'{version}.0.4283.60',
                                  'platform': 'Windows', 'platformVersion': '19.0.0',
                                  'architecture': 'x86', 'model': '', 'mobile': False}})
    except Exception as ex:
        log(f'Edge伪装UA容忍: {ex}')
    try:
        cdp.call('Page.addScriptToEvaluateOnNewDocument', {'source': he_patch})
    except Exception as ex:
        log(f'Edge伪装高熵与反指纹容忍: {ex}')
    try:
        cdp.call('Network.enable')
        cdp.call('Network.setExtraHTTPHeaders', {'headers': {
            'x-rewards-source': 'msrewards-desktop',
            'x-msrewards-app': '1'
        }})
    except Exception as ex:
        log(f'Rewards专属头注入容忍: {ex}')


def verify_disguise(cdp):
    """自检: 高熵 fullVersionList 必须是 Edge 且 platform 为 Win32。"""
    v = ev(cdp, '(async()=>{try{'
                'if(!navigator.userAgentData) return "NO-UAD";'
                'const h = await navigator.userAgentData'
                '.getHighEntropyValues(["fullVersionList"]);'
                'return JSON.stringify({ver: h.fullVersionList, plat: navigator.platform, wd: navigator.webdriver})'
                '}catch(e){return "ERR:"+e}})()')
    return 'Microsoft Edge' in (v or '') and 'Win32' in (v or '')


# ---------- chromium 生命周期 ----------

def _base_url(cdp):
    u = urllib.parse.urlparse(cdp.ws_url)
    return 'http://%s:%s' % (u.hostname, u.port)


def tab_ids(cdp):
    try:
        return [t['id'] for t in _http_json(_base_url(cdp) + '/json/list', timeout=5)]
    except Exception:
        return []


def close_tab(cdp, tid):
    try:
        _http_json(_base_url(cdp) + '/json/close/' + tid, timeout=5)
    except Exception:
        pass


def mouse_move(cdp, x, y):
    cdp.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})


def human_click(cdp, find_js, label='', close_new=False):
    """真人式点击: 滚动到可见 -> 随机轨迹接近 -> 按压释放。返回新标签 id 列表。

    2026-10-15 从现网 bing_daily_v2.human_click 移植。Rewards 卡片的归因
    记在"点击卡片"这个动作上, 直接 navigate 到 destinationUrl 不落库,
    所以卡片任务必须走真实鼠标点击。
    """
    before = set(tab_ids(cdp))
    time.sleep(random.uniform(0.8, 2.0))
    pt = ev(cdp, ("(() => { const el = (%s); if (!el) return null;"
                  " el.scrollIntoView({block:'center'});"
                  " const r = el.getBoundingClientRect();"
                  " return {x: r.x + r.width/2, y: r.y + r.height/2}; })()" % find_js))
    if not pt:
        raise StepError('点击目标不存在: %s' % label)
    x, y = float(pt['x']), float(pt['y'])
    time.sleep(random.uniform(0.5, 1.2))
    sx, sy = x + random.uniform(-400, 400), y + random.uniform(-300, 300)
    for _ in range(random.randint(4, 7)):
        sx += (x - sx) * random.uniform(0.3, 0.6) + random.uniform(-25, 25)
        sy += (y - sy) * random.uniform(0.3, 0.6) + random.uniform(-25, 25)
        mouse_move(cdp, round(sx, 1), round(sy, 1))
        time.sleep(random.uniform(0.08, 0.2))
    mouse_move(cdp, round(x, 1), round(y, 1))
    time.sleep(random.uniform(0.15, 0.4))
    cdp.call('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': x, 'y': y,
                                          'button': 'left', 'clickCount': 1})
    time.sleep(random.uniform(0.05, 0.15))
    cdp.call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': x, 'y': y,
                                          'button': 'left', 'clickCount': 1})
    time.sleep(random.uniform(1.0, 2.0))
    new = [t for t in tab_ids(cdp) if t not in before]
    if close_new:
        for tid in new:
            close_tab(cdp, tid)
        return []
    return new


class Chromium:
    """headless chromium 实例管理(每账号一个, 用完即停; 支持代理与外部已运行端口连接)。"""

    def __init__(self, binary='chromium', port=None, user_data_dir=None,
                 extra_args=None, version='141', proxy=None, cdp_port=None, log=print,
                 fingerprint=None):
        if not shutil.which(binary):
            for cand in ('google-chrome', 'chromium', 'chromium-browser', 'edge', 'msedge'):
                if shutil.which(cand):
                    binary = cand
                    break
        self.binary = binary
        self.cdp_port = cdp_port
        self.port = port or cdp_port or self._free_port()
        self.is_external = bool(cdp_port)
        self.user_data_dir = user_data_dir
        self.version = version
        self.proxy = proxy
        self.fingerprint = fingerprint or {}
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
        if self.is_external:
            try:
                tabs = _http_json(f'http://127.0.0.1:{self.port}/json/list', timeout=3)
                if tabs:
                    self.log(f'成功连接外部 Chrome CDP 端口: {self.port}')
                    return tabs
            except Exception:
                pass
            self.log(f'外部端口 {self.port} 未响应, 自动启动内置 headless chromium')
            self.is_external = False
            self.port = self._free_port()

        # 启动前清理当前账号 Profile 目录下残留的 Singleton 锁文件，防止因上次异常退出导致启动挂死
        if self.user_data_dir and os.path.isdir(self.user_data_dir):
            for lock_name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
                f = os.path.join(self.user_data_dir, lock_name)
                if os.path.exists(f) or os.path.islink(f):
                    try:
                        os.unlink(f)
                    except Exception:
                        pass

        w = self.fingerprint.get('screen_width', 1280)
        h = self.fingerprint.get('screen_height', 900)

        # 针对 ARM 低功耗/低内存环境全盘调优的 Chromium 启动参数
        args = [
            self.binary,
            '--headless=new',
            f'--remote-debugging-port={self.port}',
            f'--user-data-dir={self.user_data_dir}',
            '--no-sandbox',
            '--disable-gpu',
            '--disable-dev-shm-usage',             # 避免 Docker/ARM /dev/shm 64MB 崩溃
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-background-networking',
            '--disable-sync',
            '--disable-blink-features=AutomationControlled',
            '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
            '--disable-extensions',
            '--disable-component-update',
            '--disk-cache-size=33554432',          # 限制磁盘缓存为 32MB，保护 ARM 存储介质
            '--media-cache-size=16777216',         # 限制媒体缓存为 16MB
            '--js-flags=--max-old-space-size=256', # 限制 V8 堆内存为 256MB，防止 ARM OOM 崩溃
            f'--window-size={w},{h}'
        ]
        if self.proxy:
            args.append(f'--proxy-server={self.proxy}')
            self.log(f'Chromium 挂载代理出口: {self.proxy}')
        args.extend(self.extra_args)

        # Linux 上开启独立的 Process Group，确保子进程(renderer, GPU)在账号完成时能一并彻底杀除
        preexec = os.setsid if hasattr(os, 'setsid') else None
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, preexec_fn=preexec)
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

    def cleanup_extra_tabs(self, keep_tab_id=None):
        """关闭所有未使用的标签页，确保 ARM 设备仅保留当前活跃的单一工作标签以节约内存。"""
        try:
            tabs = _http_json(f'http://127.0.0.1:{self.port}/json/list', timeout=3)
            for t in tabs:
                tid = t.get('id')
                if tid and (keep_tab_id is None or tid != keep_tab_id):
                    self.close_tab(tid)
        except Exception:
            pass

    def attach(self, tab):
        cdp = CDP(tab['webSocketDebuggerUrl'])
        cdp.connect()
        cdp.call('Page.enable')
        cdp.call('Runtime.enable')
        if self.fingerprint:
            from msr import fingerprint
            fingerprint.apply_to_cdp(cdp, self.fingerprint, self.log)
        else:
            disguise_edge(cdp, self.version, self.log)
        return cdp

    def stop(self):
        """彻底关闭浏览器会话，终止整个子进程树并清理锁文件。"""
        if self.is_external:
            return

        # 1. 先尝试关闭所有打开的标签页
        try:
            tabs = _http_json(f'http://127.0.0.1:{self.port}/json/list', timeout=2)
            for t in tabs:
                self.close_tab(t.get('id'))
        except Exception:
            pass

        # 2. 杀除进程组 (包含所有 renderer 和 GPU 子进程)
        if self.proc:
            import signal
            pid = self.proc.pid
            try:
                if hasattr(os, 'getpgid') and hasattr(os, 'killpg'):
                    pgid = os.getpgid(pid)
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    self.proc.terminate()
            except Exception:
                try:
                    self.proc.terminate()
                except Exception:
                    pass

            t0 = time.time()
            while time.time() - t0 < 3:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)

            if self.proc.poll() is None:
                try:
                    if hasattr(os, 'getpgid') and hasattr(os, 'killpg'):
                        pgid = os.getpgid(pid)
                        os.killpg(pgid, signal.SIGKILL)
                    else:
                        self.proc.kill()
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
                try:
                    self.proc.wait(timeout=2)
                except Exception:
                    pass
            self.proc = None

        # 3. 清理当前账号 user-data-dir 下可能遗留的 Singleton 锁文件
        if self.user_data_dir and os.path.isdir(self.user_data_dir):
            for lock_name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
                f = os.path.join(self.user_data_dir, lock_name)
                if os.path.exists(f) or os.path.islink(f):
                    try:
                        os.unlink(f)
                    except Exception:
                        pass


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
