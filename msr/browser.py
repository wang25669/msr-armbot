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
import threading
import time
import urllib.parse
import urllib.request

WS_TIMEOUT = 60


class StepError(Exception):
    pass


_no_proxy_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(url, method='GET', timeout=15, headers=None):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    with _no_proxy_opener.open(req, timeout=timeout) as r:
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
        self.fingerprint = None
        self.log = None
        self._lock = threading.RLock()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *a):
        self.close()

    def connect(self):
        with self._lock:
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
        with self._lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

    def _reconnect(self):
        with self._lock:
            self.close()
            # 1. 尝试直接重连原 path (最多 2 次)
            for attempt in range(2):
                try:
                    self.connect()
                    for m in ('Page.enable', 'Runtime.enable'):
                        try:
                            self._call_once(m, timeout=15)
                        except Exception:
                            pass
                    return True
                except Exception:
                    self.close()
                    time.sleep(1)

            # 2. 若原 tab 已失效/被关闭，查询 Chromium targets 找寻活跃 page 或创建新 page 兜底恢复
            try:
                targets = _http_json(f'http://{self.host}:{self.port}/json/list', timeout=3)
                # 仅寻找真实的普通网页标签 (type == 'page')
                page_tab = next((t for t in targets if t.get('type') == 'page' and t.get('webSocketDebuggerUrl')), None)
                if not page_tab:
                    # 没有任何普通 page，自动开一个新 page 兜底
                    page_tab = _http_json(f'http://{self.host}:{self.port}/json/new?about:blank', method='PUT', timeout=5)

                if page_tab and page_tab.get('webSocketDebuggerUrl'):
                    new_ws = page_tab['webSocketDebuggerUrl']
                    self.ws_url = new_ws
                    u = urllib.parse.urlparse(new_ws)
                    self.path = u.path + (('?' + u.query) if u.query else '')
                    self.connect()
                    for m in ('Page.enable', 'Runtime.enable'):
                        try:
                            self._call_once(m, timeout=15)
                        except Exception:
                            pass
                    if self.fingerprint and self.log:
                        try:
                            from msr import fingerprint
                            fingerprint.apply_to_cdp(self, self.fingerprint, self.log)
                        except Exception:
                            pass
                    return True
            except Exception:
                pass

            return False

    def _send_frame(self, payload):
        if not self.sock:
            raise StepError('CDP 套接字未连接')
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

    def _parse_frame_from_buf(self):
        """尝试从 self._rest 缓冲区解析一条完整的 WebSocket 帧。
        返回 (has_frame, frame_bytes)。若数据不足则保留缓冲区并返回 (False, None)。
        """
        if len(self._rest) < 2:
            return False, None
        b0 = self._rest[0]
        b1 = self._rest[1]
        opcode = b0 & 0x0F
        ln = b1 & 0x7F
        offset = 2
        if ln == 126:
            if len(self._rest) < 4:
                return False, None
            ln = struct.unpack('>H', self._rest[2:4])[0]
            offset = 4
        elif ln == 127:
            if len(self._rest) < 10:
                return False, None
            ln = struct.unpack('>Q', self._rest[2:10])[0]
            offset = 10

        has_mask = bool(b1 & 0x80)
        if has_mask:
            if len(self._rest) < offset + 4:
                return False, None
            mask = self._rest[offset:offset + 4]
            offset += 4

        if len(self._rest) < offset + ln:
            return False, None

        payload = self._rest[offset:offset + ln]
        self._rest = self._rest[offset + ln:]

        if has_mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        if opcode == 0x8:
            raise StepError('CDP 连接被关闭')
        if opcode == 0x9:
            try:
                self.sock.sendall(b'\x8a\x80' + os.urandom(4))
            except Exception:
                pass
            return True, None
        return True, payload

    def _recv_frame(self, timeout=None):
        if not self.sock:
            raise StepError('CDP 套接字未连接')
        deadline = (time.time() + timeout) if timeout is not None else None
        while True:
            has_frame, frame = self._parse_frame_from_buf()
            if has_frame:
                return frame
            rem = (deadline - time.time()) if deadline is not None else WS_TIMEOUT
            if timeout is not None and rem <= 0:
                return None
            self.sock.settimeout(max(0.02, min(rem, WS_TIMEOUT)))
            try:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise StepError('CDP 连接被对端关闭')
                self._rest += chunk
            except (socket.timeout, TimeoutError):
                if timeout is not None:
                    return None
                raise StepError('CDP 接收超时')

    def pump_events(self, timeout=0.05):
        """非阻塞/短超时主动排空并分发 WebSocket 中已到达的所有异步事件帧。"""
        with self._lock:
            if not self.sock:
                return 0
            count = 0
            deadline = time.time() + timeout
            while time.time() < deadline:
                rem = max(0.005, deadline - time.time())
                try:
                    frame = self._recv_frame(timeout=rem)
                    if frame is None:
                        break
                    obj = json.loads(frame.decode('utf-8', errors='ignore'))
                    if obj.get('method') and self.event_handler:
                        self.event_handler(obj)
                    count += 1
                except Exception:
                    break
            return count

    def _call_once(self, method, params=None, timeout=WS_TIMEOUT):
        self._id += 1
        mid = self._id
        self._send_frame(json.dumps({'id': mid, 'method': method,
                                     'params': params or {}}).encode())
        deadline = time.time() + timeout
        while time.time() < deadline:
            frame = self._recv_frame(timeout=max(0.1, deadline - time.time()))
            if frame is None:
                continue
            try:
                obj = json.loads(frame.decode('utf-8', errors='ignore'))
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
        with self._lock:
            if not self.sock:
                if not self._reconnect():
                    raise StepError(f'CDP 连接已断开且重连失败: {method}')
            try:
                return self._call_once(method, params, timeout)
            except (BrokenPipeError, ConnectionResetError, OSError, socket.error, AttributeError) as e:
                if not self._reconnect():
                    raise StepError(f'CDP 调用失败且重连无效: {method}: {e}')
                try:
                    return self._call_once(method, params, timeout)
                except Exception as e2:
                    raise StepError(f'CDP {method} 重连后仍失败: {e2}')
            except StepError as e:
                err_str = str(e)
                if any(k in err_str for k in ('连接被关闭', '握手', '中断', '未连接')) and self._reconnect():
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
    time.sleep(2)
    # 放宽页面加载完成等待时间 (从 20s 延长至 30s，适应高延迟代理)
    for _ in range(60):
        try:
            if ev(cdp, 'document.readyState') == 'complete':
                break
        except StepError:
            break
        time.sleep(0.5)
    time.sleep(random.uniform(1.5, 3.0))


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

      // 禁用/删除 PublicKeyCredential 并拦截 WebAuthn，防止将无头浏览器识别为支持 Passkey/设备密钥
      try {
        if ('PublicKeyCredential' in window) {
          try {
            delete window.PublicKeyCredential;
          } catch(e) {
            window.PublicKeyCredential = undefined;
          }
        }
        if (navigator.credentials) {
          navigator.credentials.get = () => Promise.reject(new DOMException('Not supported', 'NotSupportedError'));
          navigator.credentials.create = () => Promise.reject(new DOMException('Not supported', 'NotSupportedError'));
        }
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
            'Accept-Language': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7'
        }})
    except Exception as ex:
        log(f'默认语言头容忍: {ex}')


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
        targets = _http_json(_base_url(cdp) + '/json/list', timeout=5)
        # 严密过滤：只统计真正的普通页面(type == 'page')，绝不将 iframe、worker 或 browser_ui 误作为新标签
        return [t['id'] for t in targets if t.get('type') == 'page']
    except Exception:
        return []


def close_tab(cdp, tid):
    try:
        targets = _http_json(_base_url(cdp) + '/json/list', timeout=3)
        t = next((x for x in targets if x.get('id') == tid), None)
        if t and t.get('type') != 'page':
            return
        _http_json(_base_url(cdp) + '/json/close/' + tid, timeout=5)
    except Exception:
        pass


def mouse_move(cdp, x, y):
    cdp.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})


def mouse_click(cdp, x, y):
    """在指定视口坐标模拟真人左键点击，双重保障：CDP 原生事件分发 + DOM 辅助触发。"""
    ix, iy = int(x), int(y)
    try:
        cdp.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': ix, 'y': iy})
        time.sleep(0.02)
        cdp.call('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': ix, 'y': iy, 'button': 'left', 'clickCount': 1})
        time.sleep(0.05)
        cdp.call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': ix, 'y': iy, 'button': 'left', 'clickCount': 1})
    except Exception:
        pass

    try:
        click_script = f'''(() => {{
            const el = document.elementFromPoint({ix}, {iy});
            if (el) {{
                if (typeof el.focus === 'function') el.focus();
                el.click();
                const clickable = el.closest('button, a, input, [role="button"], [tabindex], .fui-Button, .btn');
                if (clickable && clickable !== el) {{
                    if (typeof clickable.focus === 'function') clickable.focus();
                    clickable.click();
                }}
            }}
        }})()'''
        ev(cdp, click_script)
    except Exception:
        pass


def capture_screenshot(cdp, quality=60):
    """截取当前页面快照, 返回 base64 字符串。"""
    try:
        res = cdp.call('Page.captureScreenshot', {'format': 'jpeg', 'quality': quality})
        return res.get('data', '')
    except Exception:
        return ''


def human_click(cdp, find_js, label='', close_new=False, timeout=8.0):
    """真人式点击: 滚动到可见 -> 随机轨迹接近 -> 按压释放。返回新标签 id 列表。

    2026-10-15 从现网 bing_daily_v2.human_click 移植。MSR 卡片的归因
    记在"点击卡片"这个动作上, 直接 navigate 到 destinationUrl 不落库,
    所以卡片任务必须走真实鼠标点击。
    """
    before = set(tab_ids(cdp))
    time.sleep(random.uniform(0.5, 1.2))

    # 网络与渲染延迟自适应容错：在 timeout 窗口内轮询直到目标出现且具有可见尺寸
    t_end = time.time() + max(1.0, timeout)
    pt = None
    query_js = ("(() => { const el = (%s); if (!el) return null;"
                " const r = el.getBoundingClientRect();"
                " if (r.width <= 0 || r.height <= 0) return null;"
                " el.scrollIntoView({block:'center'});"
                " const r2 = el.getBoundingClientRect();"
                " return {x: r2.x + r2.width/2, y: r2.y + r2.height/2}; })()" % find_js)
    while time.time() < t_end:
        try:
            pt = ev(cdp, query_js)
            if pt and 'x' in pt and 'y' in pt:
                break
        except Exception:
            pass
        time.sleep(0.4)

    if not pt:
        raise StepError('点击目标不存在: %s' % label)
    x, y = float(pt['x']), float(pt['y'])
    time.sleep(random.uniform(0.3, 0.8))
    sx, sy = x + random.uniform(-300, 300), y + random.uniform(-200, 200)
    for _ in range(random.randint(4, 6)):
        sx += (x - sx) * random.uniform(0.3, 0.6) + random.uniform(-20, 20)
        sy += (y - sy) * random.uniform(0.3, 0.6) + random.uniform(-20, 20)
        mouse_move(cdp, round(sx, 1), round(sy, 1))
        time.sleep(random.uniform(0.06, 0.15))
    mouse_move(cdp, round(x, 1), round(y, 1))
    time.sleep(random.uniform(0.12, 0.3))
    cdp.call('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': x, 'y': y,
                                          'button': 'left', 'clickCount': 1})
    time.sleep(random.uniform(0.05, 0.12))
    cdp.call('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': x, 'y': y,
                                          'button': 'left', 'clickCount': 1})
    time.sleep(random.uniform(0.8, 1.8))
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
                 fingerprint=None, headless=True, disable_webrtc_leak=True):
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
        self.headless = headless
        self.disable_webrtc_leak = disable_webrtc_leak
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
            '--disable-extensions',
            '--disable-component-update',
            '--disable-features=WebAuthentication,WebAuthenticationConditionalUI,WebAuthenticationPermitEnterpriseAttestation',
            '--disk-cache-size=33554432',          # 限制磁盘缓存为 32MB，保护 ARM 存储介质
            '--media-cache-size=16777216',         # 限制媒体缓存为 16MB
            '--js-flags=--max-old-space-size=256', # 限制 V8 堆内存为 256MB，防止 ARM OOM 崩溃
            f'--window-size={w},{h}'
        ]
        if self.headless:
            args.append('--headless=new')
        if self.disable_webrtc_leak:
            args.append('--force-webrtc-ip-handling-policy=disable_non_proxied_udp')
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
            targets = _http_json(f'http://127.0.0.1:{self.port}/json/list', timeout=3)
            t = next((x for x in targets if x.get('id') == tid), None)
            if t and t.get('type') != 'page':
                return
            _http_json(f'http://127.0.0.1:{self.port}/json/close/{tid}', method='GET')
        except Exception:
            pass

    def cleanup_extra_tabs(self, keep_tab_id=None):
        """关闭所有未使用的标签页，确保 ARM 设备仅保留当前活跃的单一工作标签以节约内存。"""
        try:
            tabs = _http_json(f'http://127.0.0.1:{self.port}/json/list', timeout=3)
            for t in tabs:
                if t.get('type') != 'page':
                    continue
                tid = t.get('id')
                if tid and (keep_tab_id is None or tid != keep_tab_id):
                    self.close_tab(tid)
        except Exception:
            pass

    def attach(self, tab):
        cdp = CDP(tab['webSocketDebuggerUrl'])
        cdp.fingerprint = self.fingerprint
        cdp.log = self.log
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

            if self.proc.poll() is not None:
                try:
                    self.proc.wait(timeout=1)
                except Exception:
                    pass
            else:
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

    kill = stop
    close = stop



# ---------- cookie 注入(B1 登录方案) ----------

def inject_cookies(cdp, cookies, log=print):
    """cookies: [{name,value,domain,path,...}] (Cookie-Editor 导出格式兼容)。

    在导航前调用。注入后首个导航生效; httpOnly cookie 必须由本接口写入
    (JS document.cookie 写不了 httpOnly)。
    优先使用 CDP Network.setCookies 批量写入 (1 次 IPC，耗时数毫秒)；若异常则降级为单枚逐条写入。
    """
    if not cookies:
        return 0
    try:
        cdp.call('Network.enable')
    except Exception:
        pass

    cookie_params = []
    for c in cookies:
        if not c.get('name'):
            continue
        p = {
            'name': c['name'],
            'value': str(c.get('value', '')),
            'domain': c.get('domain') or '.bing.com',
            'path': c.get('path') or '/',
            'secure': bool(c.get('secure', True)),
            'httpOnly': bool(c.get('httpOnly', False)),
        }
        if c.get('expirationDate'):
            try:
                p['expires'] = float(c['expirationDate'])
            except Exception:
                pass
        cookie_params.append(p)

    # 自动补全 Edge 官方标识 Cookie，确保 Bing Rewards 将会话识别为 Microsoft Edge 环境
    if not any(p.get('name') == '_EDGE_V' for p in cookie_params):
        cookie_params.append({
            'name': '_EDGE_V',
            'value': '1',
            'domain': '.bing.com',
            'path': '/',
            'secure': True,
            'httpOnly': False,
        })

    # 1. 优先尝试 CDP 批量原子写入 Network.setCookies (1 次交互完成数十/百枚 Cookie 注入)
    try:
        cdp.call('Network.setCookies', {'cookies': cookie_params})
        log(f'cookie 批量注入完成 ({len(cookie_params)} 枚)')
        return len(cookie_params)
    except Exception as ex:
        log(f'批量写入 Network.setCookies 异常，降级逐条写入: {ex}')

    # 2. 降级方案: 逐条写入
    ok = 0
    for p in cookie_params:
        try:
            r = cdp.call('Network.setCookie', p)
            if r.get('success'):
                ok += 1
        except Exception as ex:
            log(f'cookie {p["name"]} 注入容忍: {str(ex)[:60]}')
    log(f'cookie 逐条注入完成 {ok}/{len(cookie_params)}')
    return ok
