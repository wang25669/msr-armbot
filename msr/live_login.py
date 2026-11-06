"""交互式手动登录会话 (live login)。

设计原则 (刻意做减法):
1. 不判定微软页面处于哪一步 —— 登录过程完全由人在实时画面里操作, 程序只判定
   "是否已经离开微软登录域", 彻底不依赖 OTP/拼图/推送/辅助邮箱等风控分支。
2. 浏览器走该账号自己的代理出口 (--proxy-server), 时区/语言/硬件指纹按代理出口对齐,
   会话一开始就在浏览器内自检出口 IP, 证明请求确实从代理出去 (不是 Python 侧推断)。
3. 复用账号名对应的 profile 目录 (profiles/<name>/chrome), 与日常任务共享同一套会话。
4. 登录完成后自动: 抓全量微软 Cookie -> 校验 _U -> 安全写回 -> 静默换取 OAuth Token -> 探测区域。
5. 会话与任务互斥 (同一个 runner.lock), 避免两个 Chromium 抢同一 profile。

对外只暴露: start() / frame() / input() / finish_async() / status() / close()。
"""
import json
import os
import threading
import time

from msr import browser, config, cookies as cookie_mod, fingerprint

# 同时最多几个交互登录会话 (每个约 400MB headless chromium)
MAX_ACTIVE = 2
# 多久没人看画面/没人操作就自动回收 (秒)
IDLE_CLOSE_SECONDS = 180
# 微软登录域: 停在这些域名上说明还没登录完成
LOGIN_HOSTS = ('login.live.com', 'login.microsoftonline.com', 'account.live.com')
# 默认打开的登录入口 (供联调/自测覆盖)
LOGIN_URL = 'https://login.live.com/'

_sessions = {}
_sessions_lock = threading.Lock()


def get(name):
    with _sessions_lock:
        return _sessions.get(name)


def active_names():
    with _sessions_lock:
        return list(_sessions.keys())


class LiveLogin:
    """单个账号的交互式登录会话。"""

    def __init__(self, name, proxy=None):
        self.name = name
        self.proxy = proxy
        self.ch = None
        self.cdp = None
        self.tab = None
        self.lock_path = None
        self.fp = None
        self.status = 'starting'
        self.message = '正在启动浏览器会话...'
        self.egress = None
        self.error = None
        self.result = None
        self.viewport = {'w': 1280, 'h': 900}
        # 页面真实视口 (window.innerWidth/Height), 坐标换算必须用它, 不能用画面帧尺寸:
        # screencast 会把帧缩放到 maxWidth/maxHeight, 用帧尺寸换算会导致点击整体偏移
        self.page_viewport = {'w': 1280, 'h': 800, 'dpr': 1}
        self.start_url = LOGIN_URL
        self.started_at = time.time()
        self.last_seen = time.time()
        self._stop = False
        self._closing = False
        self._finishing = False
        # 自动抓取只尝试一次; 失败后必须由用户点"我已登录完成"重试, 避免死循环劫持页面
        self._auto_finish_done = False
        self._auto_finish_blocked = False
        self._bing_sync_done = False
        self._known_targets = set()
        self._thread = None
        self._frame_lock = threading.Lock()
        self._frame = None
        self._frame_seq = 0
        self._ack_sid = None
        self._log = []
        self._login_seen_at = None

    # ---------- 生命周期 ----------

    def log(self, msg):
        line = '[%s] %s' % (time.strftime('%H:%M:%S'), msg)
        self._log.append(line)
        if len(self._log) > 120:
            self._log.pop(0)
        try:
            config.log(self.name, '[手动登录] ' + msg)
        except Exception:
            pass

    def start(self):
        from msr import runner

        lock = runner.acquire_runner_lock(config, self.name)
        if not lock:
            raise RuntimeError('该账号正在执行任务，请等它结束或在控制台先停用后再登录')
        self.lock_path = lock

        try:
            rg = config.load_json(config.pfile(self.name, 'region.json'), {}) or {}
            self.fp = fingerprint.load_or_create(
                self.name, country=rg.get('country'), proxy=self.proxy)
            self._align_fingerprint_with_proxy()
            self.viewport = {'w': self.fp.get('screen_width', 1280),
                             'h': self.fp.get('screen_height', 900)}

            from msr import settings
            st = settings.load_settings()
            b_cfg = st.get('browser', {})
            chrome_dir = config.profile(self.name) + '/chrome'
            self.log('启动无头 Chromium (出口: %s, 指纹: %s/%s, %s)' % (
                self.proxy or '直连', self.fp.get('os_name'),
                self.fp.get('hardware_concurrency'), self.fp.get('timezone')))

            self.ch = browser.Chromium(
                binary=b_cfg.get('binary') or self._default_binary(),
                user_data_dir=chrome_dir,
                version=self.fp.get('edge_version', '141'),
                proxy=self.proxy,
                log=self.log,
                fingerprint=self.fp,
                headless=b_cfg.get('headless', True),
                disable_webrtc_leak=b_cfg.get('disable_webrtc_leak', True),
            )
            tabs = self.ch.start()
            pages = [t for t in (tabs or []) if t.get('type') == 'page']
            self.tab = pages[0] if pages else self.ch.new_tab('about:blank')
            self.cdp = self.ch.attach(self.tab)
            self.ch.cleanup_extra_tabs(keep_tab_id=self.tab.get('id'))

            self.status = 'checking_egress'
            self.message = '正在用浏览器本体确认代理出口...'
            self.egress = self._probe_egress()

            self.status = 'waiting'
            self.message = '请在画面中完成微软账号登录（密码、验证码、拼图都由你自己输入）'
            self.log('加载微软登录页: %s' % self.start_url)
            browser.navigate(self.cdp, self.start_url)
            self._read_page_viewport()
            self._focus_first_input()
            self._start_screencast()

            self._thread = threading.Thread(target=self._loop, name='live-login-%s' % self.name,
                                            daemon=True)
            self._thread.start()
            return self.status_dict()
        except Exception:
            self.close('start_failed')
            raise

    @staticmethod
    def _default_binary():
        import shutil
        for cand in ('chromium', 'chromium-browser', 'google-chrome', 'msedge', 'edge'):
            if shutil.which(cand):
                return cand
        return 'chromium'

    def _align_fingerprint_with_proxy(self):
        """指纹时区/语言跟随代理出口; 代理不可达时保留原值 (不阻塞登录)。"""
        if not self.proxy:
            return
        try:
            from msr import geo
            p_geo = geo.probe_proxy_geo(self.proxy)
            if p_geo.get('status') != 'ok':
                self.log('代理地理探测失败(保留现有指纹): %s' % p_geo.get('message'))
                return
            c = (p_geo.get('country') or '').upper()
            tz = p_geo.get('timezone')
            changed = False
            if c and self.fp.get('country') != c:
                self.fp['country'] = c
                changed = True
            if tz and self.fp.get('timezone') != tz:
                self.fp['timezone'] = tz
                changed = True
            loc = fingerprint.TIMEZONE_MAP.get(c)
            if loc and self.fp.get('locale') != loc[1]:
                self.fp['locale'] = loc[1]
                changed = True
            if changed:
                fingerprint.save_fingerprint(self.name, self.fp)
                self.log('指纹已按代理出口对齐: %s / %s' % (c or '?', tz or '?'))
        except Exception as ex:
            self.log('指纹对齐容忍: %s' % ex)

    # ---------- 出口自检 ----------

    def _probe_egress(self):
        """在浏览器内部访问 IP 回显, 证明浏览器请求确实走代理 (而不是 Python 侧)。"""
        info = {'status': 'unknown'}
        checks = (
            ('https://api.ip.sb/geoip',
             '((t) => { try { const d = JSON.parse(t); return {ip: d.ip, country: d.country_code,'
             ' city: d.city, timezone: d.timezone}; } catch(e) { return null; } })'),
            ('https://ipwho.is/',
             '((t) => { try { const d = JSON.parse(t); return d.success ? {ip: d.ip,'
             ' country: d.country_code, city: d.city,'
             ' timezone: (d.timezone && d.timezone.id) || null} : null; } catch(e) { return null; } })'),
        )
        tab = None
        try:
            for url, extract in checks:
                try:
                    tab = self.ch.new_tab(url)
                    cdp2 = self.ch.attach(tab)
                    body = ''
                    for _ in range(16):
                        time.sleep(0.5)
                        body = browser.ev(cdp2, 'document.body ? document.body.innerText : ""') or ''
                        if '{' in body:
                            break
                    parsed = browser.ev(cdp2, '%s(%s)' % (extract, json.dumps(body[:4000])))
                    try:
                        cdp2.close()
                    except Exception:
                        pass
                    self.ch.close_tab(tab.get('id'))
                    tab = None
                    if parsed and parsed.get('ip'):
                        parsed['status'] = 'ok'
                        parsed['source'] = url
                        info = parsed
                        break
                except Exception as ex:
                    self.log('出口探测容忍(%s): %s' % (url, str(ex)[:60]))
        finally:
            if tab:
                try:
                    self.ch.close_tab(tab.get('id'))
                except Exception:
                    pass

        # 与账号市场对比, 给出漫游提示 (不阻断登录)
        rg = config.load_json(config.pfile(self.name, 'region.json'), {}) or {}
        market = (rg.get('rewards_market') or rg.get('country') or '').upper()
        egress_c = (info.get('country') or '').upper()
        if market and egress_c and market != egress_c:
            info['warning'] = '出口地区 %s 与账号市场 %s 不一致，可能触发漫游锁分' % (egress_c, market)
        try:
            config.save_json(config.pfile(self.name, 'login_egress.json'), {
                'time': time.strftime('%F %T'),
                'proxy': self.proxy,
                'egress': info,
            })
        except Exception:
            pass
        if info.get('status') == 'ok':
            self.log('浏览器出口自检: %s (%s, %s)' % (info.get('ip'), info.get('country'),
                                                 info.get('timezone') or '?'))
        else:
            self.log('浏览器出口自检失败 (页面可能无法加载, 请检查代理)')
        return info

    # ---------- 视口与焦点 ----------

    def _read_page_viewport(self):
        """读取页面真实视口尺寸 (点击坐标的唯一换算基准)。"""
        try:
            v = browser.ev(self.cdp, '({w: window.innerWidth, h: window.innerHeight,'
                                     ' dpr: window.devicePixelRatio || 1})', timeout=5)
            if isinstance(v, dict) and v.get('w'):
                self.page_viewport = {'w': int(v['w']), 'h': int(v['h']), 'dpr': v.get('dpr', 1)}
        except Exception:
            pass
        return self.page_viewport

    def _focus_first_input(self):
        """登录页首个可输入框自动聚焦, 这样用户不点也能直接打字。"""
        try:
            return browser.ev(self.cdp, """(() => {
              const el = document.querySelector(
                '#usernameEntry, input[name=loginfmt], input[type=email], #i0116,'
                + ' #passwordEntry, input[name=passwd], input[type=password], #i0118,'
                + ' input[type=tel], input[type=text]');
              if (!el || el.offsetParent === null) return false;
              el.focus();
              return document.activeElement === el;
            })()""", timeout=5)
        except Exception:
            return False

    def _has_focused_editable(self):
        try:
            return bool(browser.ev(self.cdp, """(() => {
              const a = document.activeElement;
              return Boolean(a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || a.isContentEditable));
            })()""", timeout=5))
        except Exception:
            return False

    # ---------- 实时画面 (screencast) ----------

    def _start_screencast(self):
        def on_event(ev):
            if ev.get('method') == 'Page.screencastFrame':
                p = ev.get('params') or {}
                data = p.get('data')
                if not data:
                    return
                try:
                    import base64
                    raw = base64.b64decode(data)
                except Exception:
                    return
                with self._frame_lock:
                    self._frame = raw
                    self._frame_seq += 1
                    self._ack_sid = p.get('sessionId')

        self.cdp.event_handler = on_event
        try:
            vw = int(self.page_viewport.get('w') or self.viewport['w'])
            vh = int(self.page_viewport.get('h') or self.viewport['h'])
            self.cdp.call('Page.startScreencast', {
                'format': 'jpeg', 'quality': 70,
                'maxWidth': min(vw, 1600),
                'maxHeight': min(vh, 1000),
                'everyNthFrame': 1,
            })
            self.log('实时画面推流已启动')
        except Exception as ex:
            self.log('画面推流启动失败 (降级为按需截图): %s' % str(ex)[:80])

    def frame(self, since=0):
        """返回 (seq, jpeg_bytes|None)。since 与当前 seq 相同则返回 None (前端可跳过重绘)。"""
        self.last_seen = time.time()
        with self._frame_lock:
            if self._frame is None or self._frame_seq == since:
                return self._frame_seq, None
            return self._frame_seq, self._frame

    def _fallback_snapshot(self):
        """screencast 不可用时按需截图兜底。"""
        import base64
        try:
            r = self.cdp.call('Page.captureScreenshot', {'format': 'jpeg', 'quality': 55})
            data = r.get('data')
            if data:
                with self._frame_lock:
                    self._frame = base64.b64decode(data)
                    self._frame_seq += 1
        except Exception:
            pass

    # ---------- 输入转发 ----------

    def input(self, ev):
        """把前端事件转发给 CDP。kind: mouse|wheel|key|text"""
        if self.status in ('finishing', 'closing', 'success', 'error', 'cancelled'):
            return {'status': 'error', 'message': '当前会话不接受输入'}
        if not self.cdp:
            return {'status': 'error', 'message': '浏览器未就绪'}
        self.last_seen = time.time()
        kind = (ev.get('kind') or '').strip()

        def num(k, lo, hi, default=0.0):
            try:
                v = float(ev.get(k, default))
            except (TypeError, ValueError):
                v = default
            return max(lo, min(hi, v))

        try:
            if kind in ('mouse', 'wheel'):
                ty = ev.get('type')
                allowed = ('mouseMoved', 'mousePressed', 'mouseReleased', 'mouseWheel')
                if ty not in allowed:
                    return {'status': 'error', 'message': '无效的鼠标事件'}
                params = {
                    'type': ty,
                    'x': num('x', 0, 4000),
                    'y': num('y', 0, 4000),
                    'button': ev.get('button') if ev.get('button') in ('left', 'right', 'middle') else 'none',
                    'clickCount': int(num('clickCount', 0, 3, 1)),
                }
                if ty == 'mouseWheel':
                    params['deltaX'] = num('deltaX', -4000, 4000)
                    params['deltaY'] = num('deltaY', -4000, 4000)
                self.cdp.call('Input.dispatchMouseEvent', params)
            elif kind == 'key':
                ty = ev.get('type') if ev.get('type') in ('keyDown', 'keyUp') else 'keyDown'
                params = {
                    'type': ty,
                    'key': str(ev.get('key') or '')[:32],
                    'code': str(ev.get('code') or '')[:32],
                    'windowsVirtualKeyCode': int(num('vk', 0, 255)),
                    'nativeVirtualKeyCode': int(num('vk', 0, 255)),
                    'modifiers': int(num('modifiers', 0, 15)),
                }
                text = str(ev.get('text') or '')
                if text and ty == 'keyDown':
                    params['text'] = text[:8]
                    params['unmodifiedText'] = text[:8]
                self.cdp.call('Input.dispatchKeyEvent', params)
                # Enter 等按键后部分输入法/表单需要 keyUp 才算完成
                if ty == 'keyDown' and ev.get('press'):
                    up = dict(params)
                    up['type'] = 'keyUp'
                    up.pop('text', None)
                    up.pop('unmodifiedText', None)
                    self.cdp.call('Input.dispatchKeyEvent', up)
            elif kind == 'text':
                text = str(ev.get('text') or '')
                if not text:
                    return {'status': 'error', 'message': '文本为空'}
                focused = self._has_focused_editable()
                if not focused:
                    focused = bool(self._focus_first_input())
                    if focused:
                        time.sleep(0.15)
                self.cdp.call('Input.insertText', {'text': text[:4096]})
                return {'status': 'ok', 'refocused': bool(focused)} if not focused else {'status': 'ok'}
            else:
                return {'status': 'error', 'message': '无效的输入类型'}
            return {'status': 'ok'}
        except Exception as ex:
            return {'status': 'error', 'message': str(ex)[:120]}

    # ---------- 主循环 ----------

    def _loop(self):
        last_check = 0
        while not self._stop:
            try:
                if self._finishing:
                    time.sleep(0.2)
                    continue
                if self._closing:
                    break
                if self.ch and getattr(self.ch, 'proc', None) and self.ch.proc.poll() is not None:
                    self.status = 'error'
                    self.message = '浏览器进程已退出'
                    self.error = 'chromium exited'
                    break
                if not self.cdp:
                    time.sleep(0.3)
                    continue
                # 排空事件 (screencast 帧), 同时不吞掉任何 call 的响应
                self.cdp.pump_events(0.08)
                sid = self._ack_sid
                if sid:
                    self._ack_sid = None
                    try:
                        self.cdp.call('Page.screencastFrameAck', {'sessionId': sid}, timeout=5)
                    except Exception:
                        self._fallback_snapshot()
                now = time.time()
                if now - last_check >= 1.0:
                    last_check = now
                    if int(now) % 5 == 0:
                        self._read_page_viewport()
                    # 跟随新窗口(登录弹窗等), 否则画面永远停在旧标签上
                    self._sync_target()
                    # 微软登录已完成但 Bing 会话缺失时, 自动同步一次(不点击, 直接走 SSO 地址)
                    self._maybe_sync_bing()
                    # 只在"Bing 会话真的建立(_U 存在)"时才自动抓取, 且一次会话只自动抓一次。
                    # 之前用"离开了登录域"当信号, rewards/about 也会命中 -> 每 13 秒把页面
                    # 跳去 bing/rewards 一次, 用户点什么都没反应。
                    if (not self._auto_finish_done and not self._auto_finish_blocked
                            and self._has_bing_session_cookie()):
                        self._auto_finish_done = True
                        self.log('检测到 Bing 会话已建立 (_U)，开始自动抓取凭据...')
                        self._finishing = True
                        threading.Thread(target=self._finish, name='live-finish-%s' % self.name,
                                         daemon=True).start()
                        continue
                if now - self.last_seen > IDLE_CLOSE_SECONDS:
                    self.log('画面与操作已闲置 %d 秒，自动关闭会话' % IDLE_CLOSE_SECONDS)
                    self.close('idle')
                    break
            except Exception as ex:
                self.log('会话循环容忍: %s' % str(ex)[:90])
                time.sleep(0.5)

    # ---------- 标签/弹窗跟随 ----------

    def _page_targets(self):
        if not self.ch:
            return []
        try:
            items = browser._http_json('http://127.0.0.1:%d/json/list' % self.ch.port, timeout=3) or []
        except Exception:
            return []
        return [t for t in items if t.get('type') == 'page']

    def _sync_target(self):
        """跟随用户应该看到的那个页面: 新弹出的登录/授权窗口自动接管, 关掉后自动回退。"""
        if not self.ch or not self.cdp or self._finishing:
            return
        tabs = self._page_targets()
        if not tabs:
            return
        ids = {t.get('id') for t in tabs}
        cur = (self.tab or {}).get('id')
        if cur and cur not in ids:
            self._switch_target(tabs[0], '当前页面已关闭')
            return
        known = self._known_targets or {cur}
        new = [t for t in tabs if t.get('id') not in known]
        self._known_targets = ids
        for t in new:
            u = (t.get('url') or '')
            if u.startswith('http') and 'chrome-error' not in u:
                self._switch_target(t, '检测到新打开的页面')
                return

    def _switch_target(self, tab, why):
        try:
            old = self.cdp
            self.log('%s，画面切到: %s' % (why, (tab.get('url') or '')[:70]))
            new_cdp = self.ch.attach(tab)
            self.cdp = new_cdp
            self.tab = tab
            self._known_targets = {t.get('id') for t in self._page_targets()}
            with self._frame_lock:
                self._frame = None
                self._ack_sid = None
                self._frame_seq += 1
            self._start_screencast()
            self._read_page_viewport()
            try:
                if old is not None and old is not new_cdp:
                    old.close()
            except Exception:
                pass
        except Exception as ex:
            self.log('画面切换容忍: %s' % str(ex)[:80])

    # ---------- 微软会话 -> Bing 会话 (一次性自动同步) ----------

    def _has_ms_auth_cookie(self):
        names = self._cookie_names(['https://login.live.com'])
        return bool({'ESTSAUTH', 'ESTSAUTHPERSISTENT', 'WLSSC', 'RPSTAuth'} & names)

    def _maybe_sync_bing(self):
        """微软账号已登录、但 Bing 还没有会话时, 自动同步一次。

        只做一次 (self._bing_sync_done), 失败就停在 needs_action 等人, 绝不反复跳页 ——
        之前就是因为没有这道闸, 每 13 秒把页面跳去 bing/rewards, 用户点什么都点不动。
        """
        if self._bing_sync_done or self._auto_finish_blocked or self._finishing or not self.cdp:
            return
        if self._has_bing_session_cookie():
            return
        if not self._has_ms_auth_cookie():
            return
        try:
            url = browser.ev(self.cdp, 'location.href', timeout=5) or ''
        except Exception:
            return
        if any(h in url for h in ('login.live.com', 'login.microsoftonline.com')):
            return          # 还在登录流程里, 别插手
        self._bing_sync_done = True
        self.log('微软账号已登录，但 Bing 会话缺失，自动同步一次 Bing 登录态...')
        threading.Thread(target=self._sync_bing_once, name='live-sync-%s' % self.name,
                         daemon=True).start()

    def _sync_bing_once(self):
        if self._finishing:
            return
        self._finishing = True       # 暂停画面循环, 避免两个线程抢 CDP
        try:
            ok = self._ensure_bing_session()
        except Exception as ex:
            self.log('Bing 会话同步异常: %s' % str(ex)[:100])
            ok = False
        finally:
            self._finishing = False
        if self._closing:
            return
        if ok and not self._auto_finish_done and not self._auto_finish_blocked:
            self._auto_finish_done = True
            self._finishing = True
            self.log('Bing 会话已建立，开始自动抓取凭据...')
            threading.Thread(target=self._finish, name='live-finish-%s' % self.name, daemon=True).start()
        elif not ok:
            self.status = 'needs_action'
            self.message = ('微软账号已登录，但 Bing 会话同步失败。请在画面里打开 https://www.bing.com '
                            '确认右上角是已登录状态，再点“我已登录完成”重试')
            self.log(self.message)

    # ---------- Bing 会话判定 ----------

    def _cookie_names(self, urls):
        if not self.cdp:
            return set()
        try:
            res = self.cdp.call('Network.getCookies', {'urls': urls}, timeout=10)
            return {c.get('name') for c in res.get('cookies', [])}
        except Exception:
            return set()

    def _has_bing_session_cookie(self):
        names = self._cookie_names(['https://www.bing.com', 'https://rewards.bing.com', 'https://cn.bing.com'])
        return '_U' in names

    def _wait_bing_cookie(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if self._has_bing_session_cookie():
                return True
            time.sleep(1.0)
        return False

    def _ensure_bing_session(self):
        """让 bing.com 建立登录会话 (_U)。不靠点击(点击会开弹窗), 直接走 SSO 地址。"""
        cdp = self.cdp
        if not cdp:
            return False
        if self._has_bing_session_cookie():
            return True

        self.log('Bing 尚未登录，先打开首页让 WLS 握手...')
        browser.navigate(cdp, 'https://www.bing.com/?form=QBLH')
        if self._wait_bing_cookie(8):
            return True

        sso_urls = (
            'https://www.bing.com/fd/auth/signin/v2?action=interactive&provider=windows_live_id'
            '&return_url=https%3a%2f%2fwww.bing.com%2f%3fform%3dQBLH',
            'https://www.bing.com/fd/auth/signin/v2?action=interactive&provider=windows_live_id'
            '&return_url=https%3a%2f%2frewards.bing.com%2f',
        )
        for url in sso_urls:
            self.log('尝试 Bing SSO: %s' % url[:90])
            browser.navigate(cdp, url)
            if self._wait_bing_cookie(20):
                return True
            self._sync_target()
            cur = browser.ev(self.cdp or cdp, 'location.href') or ''
            if any(h in cur for h in ('login.live.com', 'login.microsoftonline.com')):
                self.log('Bing SSO 又回到微软登录页，说明微软会话没有真正完成')
                break

        self.log('访问 rewards 触发一次会话同步...')
        browser.navigate(self.cdp or cdp, 'https://rewards.bing.com/')
        return self._wait_bing_cookie(10)

    # ---------- 收尾: 抓 Cookie + 换 Token ----------

    def finish_async(self):
        if self._finishing or self.status in ('success', 'cancelled', 'error'):
            return self.status_dict()
        if not self.cdp:
            return self.status_dict()
        self._auto_finish_blocked = False      # 用户主动点了"完成", 允许再试
        self._finishing = True
        self.status = 'finishing'
        self.message = '正在抓取 Cookie 并换取 Token...'
        threading.Thread(target=self._finish, name='live-finish-%s' % self.name, daemon=True).start()
        return self.status_dict()

    def _finish(self):
        from msr import auth, probe, api
        if not self.cdp:
            self._finishing = False
            return
        self.status = 'finishing'
        self._auto_finish_blocked = True     # 失败后不再自动重试, 等用户再点
        try:
            if not self._ensure_bing_session():
                if self._closing:
                    return
                self.status = 'needs_action'
                self.message = ('微软账号已登录，但 Bing 会话仍未建立（没有 _U）。'
                                '请确认浏览器处于"已登录 bing.com"状态，然后点“我已登录完成”重试')
                self.log(self.message)
                self._finishing = False
                return

            cdp = self.cdp
            if not cdp:
                return
            cks = cookie_mod.extract_cookies_from_cdp(cdp, log=self.log)
            diag = cookie_mod.validate_cookies(cks)
            if not diag.get('has_auth_u'):
                self.status = 'needs_action'
                self.message = '抓取到 Cookie 但不含 _U，请刷新 bing.com 后点“我已登录完成”重试'
                self.log(self.message)
                self._finishing = False
                return
            cookie_mod.save_cookies_safely(self.name, cks, config=config, log=self.log)
            self.log('已保存 %d 枚 Cookie (含 _U)' % len(cks))

            tok = auth.exchange_token_from_cookies(self.name, proxy=self.proxy,
                                                   cdp=cdp, ch=self.ch, log_cb=self.log)
            has_tok = bool(tok)
            rg = config.load_json(config.pfile(self.name, 'region.json'), {}) or {}
            if not rg.get('country') or not rg.get('balance'):
                try:
                    access = api.refresh_access(self.name, config.profile(self.name), {}, proxy=self.proxy)
                    rg = probe.probe_account(self.name, access, proxy=self.proxy)
                    config.save_json(config.pfile(self.name, 'region.json'), rg)
                except Exception as ex:
                    self.log('区域探测容忍: %s' % str(ex)[:80])

            self.result = {
                'cookie_count': len(cks),
                'has_token': has_tok,
                'country': rg.get('country'),
                'balance': rg.get('balance'),
            }
            self.status = 'success'
            self.message = '登录成功：Cookie %d 枚%s%s' % (
                len(cks),
                '，Token 已换取' if has_tok else '，Token 换取失败(可仅用浏览器模式跑)',
                ('，区域 %s' % rg.get('country')) if rg.get('country') else '')
            self.log(self.message)
            time.sleep(0.8)
            self.close('success')
        except Exception as ex:
            if self._closing:
                return
            self.status = 'error'
            self.error = str(ex)
            self.message = '登录收尾失败: %s' % str(ex)[:120]
            self.log(self.message)
            self._finishing = False

    # ---------- 状态与关闭 ----------

    def status_dict(self):
        return {
            'name': self.name,
            'status': self.status,
            'message': self.message,
            'error': self.error,
            'proxy': self.proxy,
            'egress': self.egress,
            'viewport': self.viewport,
            'page_viewport': self.page_viewport,
            'result': self.result,
            'frame_seq': self._frame_seq,
            'logs': list(self._log[-12:]),
            'started_at': self.started_at,
        }

    def close(self, reason='closed'):
        self._stop = True
        self._closing = True
        if self.status not in ('success', 'error'):
            self.status = 'cancelled'
            self.message = {'idle': '会话因闲置已关闭', 'success': self.message}.get(reason, '会话已关闭')
        try:
            if self.cdp:
                try:
                    self.cdp.call('Page.stopScreencast', timeout=3)
                except Exception:
                    pass
            if self.ch:
                try:
                    self.ch.stop()
                except Exception:
                    pass
        finally:
            self.ch = None
            self.cdp = None
            # 兜底: 确认这个 profile 下没有残留 Chromium (必须在释放锁之前做完,
            # 否则任务可能刚好启动并复用同一 profile)
            try:
                kill_profile_chromium(self.name)
            except Exception:
                pass
            if self.lock_path:
                try:
                    from msr import runner
                    runner.release_runner_lock(self.lock_path)
                except Exception:
                    pass
                self.lock_path = None
            with _sessions_lock:
                if _sessions.get(self.name) is self:
                    _sessions.pop(self.name, None)


_start_locks = {}
_start_locks_guard = threading.Lock()


def _start_lock(name):
    with _start_locks_guard:
        lk = _start_locks.get(name)
        if lk is None:
            lk = threading.Lock()
            _start_locks[name] = lk
        return lk


def kill_profile_chromium(name):
    """杀掉某账号 profile 目录残留的 Chromium 进程 (孤儿会话留下的)。

    只按 --user-data-dir=<该账号 profile>/chrome 精确匹配, 不碰其它账号/任务。
    """
    import glob
    import signal
    target = os.path.join(config.profile(name), 'chrome')
    killed = []
    for pid_dir in glob.glob('/proc/[0-9]*'):
        try:
            with open(os.path.join(pid_dir, 'cmdline'), 'rb') as f:
                cmd = f.read().decode('utf-8', 'ignore')
        except Exception:
            continue
        if 'user-data-dir=' + target not in cmd:
            continue
        try:
            pid = int(os.path.basename(pid_dir))
        except ValueError:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except Exception:
            pass
    if killed:
        time.sleep(1.0)
        for pid in killed:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
    return killed


def reap_orphans():
    """启动时自愈: 清掉"锁文件残留 + 浏览器还在跑"的孤儿会话。

    判定: 该账号没有活跃登录会话, 但存在 runner.lock 且里面有 Chromium 用着这个 profile,
    且锁里记录的 PID 不可能是真正的 msr run 任务 (PID<=1 或进程已不存在)。
    """
    from msr import runner
    cleaned = []
    try:
        names = [a['name'] for a in config.accounts()]
    except Exception:
        names = []
    for name in names:
        with _sessions_lock:
            if name in _sessions:
                continue
        lock_f = os.path.join(config.profile(name), 'runner.lock')
        if not os.path.isfile(lock_f):
            continue
        holder = ''
        try:
            with open(lock_f, 'r', encoding='utf-8') as f:
                holder = f.read().strip()
        except Exception:
            pass
        pid = int(holder) if holder.isdigit() else 0
        if pid > 1 and runner.is_runner_process_alive(pid):
            continue          # 真的是任务在跑, 不动
        killed = kill_profile_chromium(name)
        try:
            os.remove(lock_f)
        except Exception:
            pass
        if killed:
            cleaned.append((name, killed))
            try:
                config.log(name, '[自愈] 清理孤儿登录会话: 关闭残留 Chromium %s, 删除残留锁' % killed)
            except Exception:
                pass
    return cleaned


def stop_session(name, reason='cancelled'):
    """关闭某个账号的登录会话 (幂等)。"""
    with _sessions_lock:
        sess = _sessions.get(name)
    if not sess:
        return False
    try:
        sess.close(reason)
    except Exception:
        try:
            kill_profile_chromium(name)
        except Exception:
            pass
        with _sessions_lock:
            if _sessions.get(name) is sess:
                _sessions.pop(name, None)
    return True


def start_session(name, proxy=None):
    """启动或复用某个账号的交互登录会话。

    同一账号的启动是串行的: 并发点击"打开登录页"不会把正在运行的会话顶掉,
    也不会把旧的会话对象从注册表里抹掉 (那会留下关不掉、还占着锁的孤儿浏览器)。
    """
    with _start_lock(name):
        with _sessions_lock:
            cur = _sessions.get(name)
            if cur and cur.status not in ('cancelled', 'error'):
                return cur
            stale = cur
            others = len([n for n in _sessions if n != name])
            if others >= MAX_ACTIVE:
                raise RuntimeError('当前已有 %d 个登录会话在进行，请先完成或关闭' % others)

        if stale is not None:
            # 上一个会话已结束(取消/出错)但对象还在: 先彻底清掉再新建
            try:
                stale.close('restart')
            except Exception:
                kill_profile_chromium(name)

        sess = LiveLogin(name, proxy=proxy)
        with _sessions_lock:
            _sessions[name] = sess
        try:
            sess.start()
        except Exception:
            with _sessions_lock:
                if _sessions.get(name) is sess:
                    _sessions.pop(name, None)
            try:
                sess.close('start_failed')
            except Exception:
                kill_profile_chromium(name)
            raise
        return sess
