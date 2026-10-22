"""微软账号多方式登录与凭据固化系统。

支持两大核心登录与凭据闭环策略:
1. credential (推荐): 现代化账号密码自动化登录，原生适配微软 Fluent UI，支持智能密码切换、保持登录确认与 2FA 提示，自动提取 Cookie 并无感换取 OAuth Token。
2. cookies (备选): 导入 Cookie-Editor JSON 数组或 Raw 请求头文本，自动清洗并基于已登录会话静默换取 OAuth Token。
"""
import base64
import json
import os
import sys
import threading
import time
import urllib.parse

from msr import cookies as cookie_mod


def cmd_login(args):
    from msr import config

    name = args.name
    method = getattr(args, 'method', None) or 'credential'
    proxy = getattr(args, 'proxy', None)
    username = getattr(args, 'username', None)
    password = getattr(args, 'password', None)
    cookie_file = getattr(args, 'cookies', None)

    p = config.profile(name)
    config.log(name, '=== 登录引导开始 (method=%s proxy=%s) ===' % (method, proxy or '直连'))

    if cookie_file or method == 'cookies':
        _login_by_cookies(name, cookie_file, proxy=proxy)
    elif method == 'interactive':
        _login_by_interactive(name, p, config, proxy=proxy)
    else:
        # 默认推荐: 账号密码自动化登录
        if not username:
            username = input('请输入微软账号邮箱 (例如 xxx@outlook.com): ').strip()
        if not password:
            import getpass
            password = getpass.getpass('请输入微软账号密码: ').strip()
        login_with_credential(name, username, password, proxy=proxy, log_cb=print)

    # 更新或登记 accounts.json
    accs = config.accounts()
    found = False
    for a in accs:
        if a['name'] == name:
            found = True
            if proxy is not None:
                a['proxy'] = proxy
            break
    if not found:
        accs.append({
            'name': name,
            'enabled': True,
            'proxy': proxy,
            'offset_minutes': 0
        })
    config.save_accounts(accs)
    config.log(name, '=== 登录引导完成 ===')


def exchange_token_from_cookies(name, proxy=None, cdp=None, ch=None, log_cb=None):
    """通过无头浏览器中已注入的微软 Cookie，静默单点登录自动获取 OAuth2 Token。"""
    from msr import config, api, probe, browser
    log = log_cb or (lambda m: config.log(name, m))
    ck_path = config.pfile(name, 'cookie.json')
    cookies = config.load_json(ck_path)
    if not cookies:
        log('未找到 cookie.json，无法换取 Token')
        return None

    own_ch = False
    if ch is None or cdp is None:
        chrome_dir = config.profile(name) + '/chrome'
        ch = browser.Chromium(user_data_dir=chrome_dir, proxy=proxy, log=log)
        tabs = ch.start()
        page_tabs = [t for t in tabs if t.get('type') == 'page']
        tab = page_tabs[0] if page_tabs else ch.new_tab('about:blank')
        cdp = ch.attach(tab)
        own_ch = True

    try:
        browser.inject_cookies(cdp, cookies, log=log)
        cdp.call('Network.enable')

        code_holder = []
        def on_event(ev):
            if ev.get('method') == 'Network.requestWillBeSent':
                url = ev.get('params', {}).get('request', {}).get('url', '')
                if 'code=' in url and 'oauth20_desktop' in url:
                    parsed = urllib.parse.urlparse(url)
                    qs = urllib.parse.parse_qs(parsed.query)
                    c = qs.get('code', [None])[0]
                    if c:
                        code_holder.append(c)

        prev_handler = cdp.event_handler
        cdp.event_handler = on_event

        client_id = api.CLIENT_ID
        scope = api.SCOPE
        redirect = api.REDIRECT
        auth_url = 'https://login.live.com/oauth20_authorize.srf?' + urllib.parse.urlencode({
            'client_id': client_id,
            'scope': scope,
            'response_type': 'code',
            'redirect_uri': redirect,
            'prompt': 'none'
        })
        cdp.call('Page.navigate', {'url': auth_url})

        deadline = time.time() + 10
        while time.time() < deadline:
            if code_holder:
                break
            time.sleep(0.3)

        cdp.event_handler = prev_handler
        if not code_holder:
            log('未能静默获取 OAuth 授权码 (可能缺少 login.live.com 会话 Cookie)')
            return None

        code = code_holder[0]
        body = {
            'grant_type': 'authorization_code',
            'client_id': client_id,
            'code': code,
            'redirect_uri': redirect,
            'scope': scope
        }
        tok = api._token_post(name, body, proxy=proxy)
        if not tok.get('refresh_token') and not tok.get('access_token'):
            return None

        api.save_token(name, tok, config.profile(name))
        log('通过 Cookie 静默生成并固化 OAuth Token 成功！')

        # 探测账号属性与区域
        try:
            pr = probe.probe_account(name, tok['access_token'], proxy=proxy)
            config.save_json(config.pfile(name, 'region.json'), pr)
            log(f'自动探测账号信息成功: 区域={pr.get("country")} 余额={pr.get("balance")}')
        except Exception as e:
            log(f'探测账号属性提示: {e}')

        return tok
    except Exception as ex:
        log(f'静默换取 Token 异常: {ex}')
        return None
    finally:
        if own_ch:
            try:
                ch.stop()
            except Exception:
                pass


def _login_by_cookies(name, cookie_file=None, proxy=None):
    """方式 2: Cookie 导入并自动换取 Token。"""
    from msr import config
    if cookie_file and os.path.isfile(cookie_file):
        print(f'\n正在从文件导入 Cookie: {cookie_file}')
        diag = cookie_mod.import_file(name, cookie_file)
    else:
        print('\n请输入或粘贴 Cookie 内容 (支持 Cookie-Editor JSON 数组或 raw 字符串，回车后按 Ctrl+D 结束):')
        lines = []
        try:
            for line in sys.stdin:
                lines.append(line)
        except Exception:
            pass
        raw = ''.join(lines).strip()
        if not raw:
            print('输入为空，取消导入')
            return
        cookies = cookie_mod.import_content(raw)
        if not cookies:
            print('Cookie 格式无法解析，请确保是合法的 JSON 数组或键值对字符串')
            return
        dest = config.pfile(name, 'cookie.json')
        config.save_json(dest, cookies)
        diag = cookie_mod.validate_cookies(cookies)

    print(f"Cookie 校验结果: 条数={diag['count']}, 核心登录凭据(_U)={diag['has_auth_u']}, 状态={diag['reason']}")
    print("正在尝试通过 Cookie 自动生成 OAuth2 Token...")
    tok = exchange_token_from_cookies(name, proxy=proxy, log_cb=print)
    if tok:
        print("🎉 Token 自动换取成功！双凭据 (Cookie+Token) 已全部就绪。")
    else:
        print("💡 Cookie 已就绪。未能自动换取 Token，系统将优先以纯浏览器模式执行搜索赚分任务。")


def _login_by_interactive(name, profile_path, config, proxy=None):
    """方式 3: 启动带有远程调试的 Headless Chromium，用户远程在浏览器中手动登录。"""
    from msr import browser
    chrome_dir = os.path.join(profile_path, 'chrome')
    ch = browser.Chromium(user_data_dir=chrome_dir, proxy=proxy, log=lambda m: config.log(name, m))
    tabs = ch.start()
    port = ch.port
    print('\n[交互式远程登录]')
    print(f'已启动浏览器实例 (调试端口: {port})')
    print('如在远程服务器，请在本地电脑通过 SSH 隧道映射端口:')
    print(f'  ssh -L {port}:127.0.0.1:{port} <user>@<server_ip>')
    print(f'然后在本地 Chrome 打开: chrome://inspect 点击 inspect 进行网页登录。')
    print('正在打开微软登录页面: https://login.live.com/ ...')

    tab = ch.new_tab('https://login.live.com/')
    cdp = ch.attach(tab)

    print('等待登录完成 (脚本自动监听 Cookie 落地，超时 5 分钟)...', flush=True)
    deadline = time.time() + 300
    success = False
    while time.time() < deadline:
        try:
            res = cdp.call('Network.getCookies', {'urls': ['https://login.live.com', 'https://www.bing.com']})
            cks = res.get('cookies', [])
            diag = cookie_mod.validate_cookies(cks)
            if diag['has_auth_u'] or diag['has_muid']:
                config.save_json(config.pfile(name, 'cookie.json'), cks)
                print(f"\n[OK] 成功捕获登录 Cookie! (条数: {len(cks)}, _U={diag['has_auth_u']})")
                success = True
                break
        except Exception:
            pass
        time.sleep(3)

    if not success:
        print('\n登录超时或未检测到有效 Cookie')
    else:
        print("正在自动通过捕获的会话换取 OAuth Token...")
        exchange_token_from_cookies(name, proxy=proxy, cdp=cdp, ch=ch, log_cb=print)

    try:
        ch.stop()
    except Exception:
        pass


def inspect_login_page(cdp):
    """自适应探测微软登录页的当前状态与风控质询类型。"""
    from msr import browser
    script = '''(() => {
        const text = (document.body ? document.body.innerText : '') || '';
        const url = location.href || '';
        
        // 1. 成功页面判断
        if (url.includes('bing.com') || url.includes('account.microsoft.com') || url.includes('rewards.bing.com') || url.includes('privacynotice') || url.includes('myaccount.microsoft.com')) {
            return { type: 'success_url', url: url };
        }
        
        // 2. 账号与风控错误拦截
        const lowerText = text.toLowerCase();
        if (text.includes('此 Microsoft 帐户不存在') || lowerText.includes("that microsoft account doesn't exist") || lowerText.includes("account doesn't exist")) {
            return { type: 'error', error: '此 Microsoft 帐户不存在，请核对用户名' };
        }
        if (text.includes('密码不正确') || text.includes('此密码不正确') || text.includes('该密码不正确') || lowerText.includes('password is incorrect') || lowerText.includes('your account or password is incorrect')) {
            return { type: 'error', error: '密码不正确，请核对密码' };
        }
        if (text.includes('此帐户已被锁定') || lowerText.includes('account has been locked') || text.includes('我们检测到异常活动') || lowerText.includes('unusual activity')) {
            return { type: 'error', error: '该微软账户触发了安全风控拦截或已被锁定，需通过官方渠道重置' };
        }

        // 检查输入字段的验证错误信息 (如 FluentUI field validationMessage)
        const valMsgEl = document.querySelector('[class*="validationMessage"], [id*="validationMessage"], [role="alert"].error');
        if (valMsgEl && valMsgEl.innerText.trim()) {
            const vt = valMsgEl.innerText.trim();
            if (vt.toLowerCase().includes('incorrect') || vt.includes('不正确')) {
                return { type: 'error', error: '密码不正确，请核对密码' };
            }
        }

        // 3. 保持登录状态 (KMSI) 弹窗 (涵盖 FluentUI: 保持登录状态? / 无需每次都登录 / Stay signed in?)
        if (text.includes('保持登录状态') || text.includes('Stay signed in') || text.includes('无需每次都登录') || text.includes('reduce the number of times you are asked to sign in')) {
            return { type: 'kmsi' };
        }

        // 4. 手机 Authenticator 匹配数字 (Number Matching)
        const signEl = document.querySelector('#idRichContext_DisplaySign, .display-sign-in-large-text, #displaySign, [data-test-id=displaySign]');
        if (signEl && signEl.innerText.trim()) {
            return { type: 'auth_push', code: signEl.innerText.trim() };
        }

        // 5. 密码输入页 (若密码输入框已可见，优先进入填密阶段)
        const pwdInput = document.querySelector('#passwordEntry, input[name=passwd], input[type=password], #i0118');
        if (pwdInput && pwdInput.offsetParent !== null) {
            return { type: 'password_entry' };
        }

        // 6. 检测是否提供“使用密码”切换选项 (针对默认推 Authenticator 或发邮件验证码的账号)
        if (document.body) {
            const switchPwdEl = Array.from(document.body.querySelectorAll('*')).find(x => {
                const t = (x.innerText || '').trim().toLowerCase();
                const isMatch = (t === '使用密码' || t === '使用你的密码' || t === 'use password' || t === 'use your password' || t === 'use password instead' || t === 'use a password instead');
                if (!isMatch || x.children.length > 0) return false;
                const r = x.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            });
            if (switchPwdEl) {
                const r = switchPwdEl.getBoundingClientRect();
                return { type: 'switch_to_password', x: r.x + r.width/2, y: r.y + r.height/2 };
            }
        }

        // 7. 用户名输入页 (确保仅在尚未输入用户名时匹配)
        const userInput = document.querySelector('#usernameEntry, input[name=loginfmt], input[type=email], #i0116');
        if (userInput && userInput.offsetParent !== null) {
            return { type: 'username_entry' };
        }

        // 8. 二次验证/手机/邮箱 OTC 验证码输入框
        const otcInput = document.querySelector('#idTxtBx_SAOTCC_OTC, input[name=otc], input[type=tel], #idTxtBx_SAOTCS_ProofConfirmation, input[name=ProofConfirmation], #idTxtBx_OTC, #proof-confirmation-email-input');
        if (otcInput && otcInput.offsetParent !== null) {
            let prompt = '微软要求输入安全验证码';
            const pEl = document.querySelector('#idDiv_SAOTCC_Description, .text-body, #idDiv_SAOTCS_Description, [role=heading]');
            if (pEl && pEl.innerText.trim()) {
                prompt = pEl.innerText.trim().slice(0, 100);
            }
            return { type: 'otp_input', prompt: prompt };
        }

        // 9. 人机拼图验证码 (Arkose Labs FunCaptcha)
        const arkose = document.querySelector('iframe[src*="arkoselabs"], iframe[src*="arkose"], #enforcementFrame, div[id*="wlspisp"]');
        if (arkose || text.includes('请解决谜题') || text.includes('证明你不是机器人') || text.includes('Please solve the puzzle')) {
            return { type: 'arkose_challenge' };
        }

        // 10. 设备密钥 / 通行密钥 / 安全密钥 / 摆脱密码 / 无密码免密拦截与可跳过弹窗
        const hasPasskeyPrompt = text.includes('设备密钥') || text.includes('通行密钥') || 
                                 text.includes('安全密钥') || text.includes('无需密码') || 
                                 text.includes('摆脱密码') || text.includes('加快登录速度') ||
                                 lowerText.includes('passkey') || lowerText.includes('security key') || 
                                 lowerText.includes('break free from your passwords') || 
                                 lowerText.includes('windows hello');

        const hasDismissBtn = document.querySelector('#iCancel, #cancelButton, [data-test-id=cancelButton], #iShowSkip, #skipButton, [data-test-id=skipButton], #idBtn_Back');

        // 检测是否存在可跳过/取消的按钮或链接 (包括以后再说、暂不、跳过、取消等)
        const skipOrCancelBtn = Array.from(document.querySelectorAll('button, a, input[type=button], input[type=submit], [role=button]')).find(el => {
            if (el.offsetParent === null) return false;
            const t = (el.innerText || el.value || '').trim().toLowerCase();
            return ['取消', '以后再说', '暂不', '跳过', 'cancel', 'skip for now', 'not now', 'no thanks', 'skip'].includes(t);
        });

        if (hasPasskeyPrompt || hasDismissBtn || (skipOrCancelBtn && (text.includes('保护') || text.includes('安全') || text.includes('体验') || text.includes('应用') || text.includes('关联')))) {
            return { type: 'dismissible_prompt', has_passkey: hasPasskeyPrompt };
        }

        // 11. 验证方式选择列表 / 辅助邮箱确认 (Proof Selection)
        const proofs = document.querySelector('#idDiv_SAOTCS_Proofs, .tile-container, #idDiv_SAOTCS_Proofs_List');
        if (proofs || text.includes('验证你的身份') || text.includes('Verify your identity') || text.includes('选择接收代码的方式')) {
            return { type: 'proof_selection' };
        }

        // 12. 通用下一步 / 协议同意
        const nextBtn = document.querySelector('#iNext, #iLandingViewAction, #idSIButton9, input[type=submit], button[type=submit]');
        if (nextBtn && (text.includes('下一步') || text.includes('继续') || text.includes('Next') || text.includes('Continue'))) {
            return { type: 'generic_next' };
        }

        return { type: 'other', url: url };
    })()'''
    try:
        res = browser.ev(cdp, script)
        return res if isinstance(res, dict) else {'type': 'other'}
    except Exception:
        return {'type': 'other'}


class LoginSession:
    """账号登录多重交互与风控接管会话管理类。"""

    def __init__(self, name, username, password, proxy=None, log_cb=None, save_credential_flag=True):
        self.name = name
        self.username = username
        self.password = password
        self.proxy = proxy
        self.log_cb = log_cb
        self.save_credential_flag = save_credential_flag
        self.status = 'starting'  # starting, running, wait_auth_push, wait_otp, wait_interactive, success, error
        self.message = '正在初始化安全 Chromium 实例...'
        self.auth_code = None
        self.otp_prompt = None
        self.error_msg = None
        self.logs = []
        self.created_at = time.time()
        self.updated_at = time.time()
        self.ch = None
        self.cdp = None
        self.thread = None
        self.stop_requested = False
        self.result = None
        self._lock = threading.Lock()
        self._last_screenshot = None
        self._last_screenshot_at = 0
        self._screenshot_lock = threading.Lock()

    def log(self, msg):
        ts = time.strftime('%H:%M:%S')
        line = f'[{ts}] {msg}'
        with self._lock:
            self.logs.append(line)
            if len(self.logs) > 80:
                self.logs.pop(0)
        if self.log_cb:
            try:
                self.log_cb(msg)
            except Exception:
                pass

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def cancel(self):
        self.stop_requested = True
        self.status = 'error'
        self.message = '用户已取消登录会话'
        self._cleanup()

    def _cleanup(self):
        if self.ch:
            try:
                self.ch.stop()
            except Exception:
                pass
            self.ch = None
            self.cdp = None

    def get_screenshot(self):
        if not self.cdp:
            return None
        now = time.time()
        if self._last_screenshot and (now - self._last_screenshot_at < 0.3):
            return self._last_screenshot
        if not self._screenshot_lock.acquire(blocking=False):
            return self._last_screenshot
        try:
            from msr import browser
            b64 = browser.capture_screenshot(self.cdp, quality=65)
            if b64:
                try:
                    self._last_screenshot = base64.b64decode(b64)
                    self._last_screenshot_at = time.time()
                    return self._last_screenshot
                except Exception:
                    pass
            return self._last_screenshot
        finally:
            self._screenshot_lock.release()

    def submit_otp(self, code):
        if not self.cdp:
            return {'status': 'error', 'message': '浏览器未就绪'}
        from msr import browser
        import json
        self.log(f'正在向微软页面填入安全验证码/确认信息: {code} ...')
        script = f'''(() => {{
            const inp = document.querySelector('#idTxtBx_SAOTCC_OTC, input[name=otc], input[type=tel], #idTxtBx_SAOTCS_ProofConfirmation, input[name=ProofConfirmation], #idTxtBx_OTC, #proof-confirmation-email-input');
            if (inp) {{
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(inp, {json.dumps(code)});
                inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                const btn = document.querySelector('#idSubmit_SAOTCC_Continue, #idSIButton9, button[type=submit], input[type=submit]');
                if (btn) btn.click();
                return true;
            }}
            return false;
        }})()'''
        ok = browser.ev(self.cdp, script)
        if ok:
            self.status = 'running'
            self.message = '验证码已提交，正在等待微软校验...'
            return {'status': 'ok'}
        return {'status': 'error', 'message': '未能定位验证码输入框，请确认当前画面是否在输入验证码'}

    def dispatch_mouse_click(self, x, y):
        if not self.cdp:
            return {'status': 'error', 'message': '浏览器未就绪'}
        from msr import browser
        try:
            browser.mouse_click(self.cdp, x, y)
            self.log(f'用户远程交互点击视口坐标: ({x}, {y})')
            return {'status': 'ok'}
        except Exception as e:
            return {'status': 'error', 'message': str(e)}

    def to_dict(self):
        with self._lock:
            vp = {'w': 1280, 'h': 757}
            if self.ch and hasattr(self.ch, 'fingerprint') and self.ch.fingerprint:
                vp['w'] = self.ch.fingerprint.get('screen_width', 1280)
                vp['h'] = self.ch.fingerprint.get('screen_height', 757)
            return {
                'name': self.name,
                'status': self.status,
                'message': self.message,
                'auth_code': self.auth_code,
                'otp_prompt': self.otp_prompt,
                'error_msg': self.error_msg,
                'logs': list(self.logs),
                'result': self.result,
                'viewport': vp,
                'updated_at': self.updated_at
            }

    def _run(self):
        import json
        from msr import config, browser, cookies as cookie_mod
        profile_path = config.profile(self.name)
        chrome_dir = os.path.join(profile_path, 'chrome')

        try:
            self.log(f'正在启动安全无头 Chromium 实例 (账号: {self.username})...')
            self.ch = browser.Chromium(user_data_dir=chrome_dir, proxy=self.proxy, log=self.log)
            from msr import fingerprint
            fp = fingerprint.load_or_create(self.name)
            self.ch.fingerprint = fp

            tabs = self.ch.start()
            page_tabs = [t for t in tabs if t.get('type') == 'page']
            tab = page_tabs[0] if page_tabs else self.ch.new_tab('about:blank')
            self.cdp = self.ch.attach(tab)

            self.status = 'running'
            self.message = 'Chromium 已就绪，正在准备纯净登录环境...'
            self.updated_at = time.time()

            # 启动前主动清理残留 Cookie 与网络缓存，杜绝历史状态或前序失败会话污染
            try:
                self.cdp.call('Network.clearBrowserCookies')
                self.cdp.call('Network.clearBrowserCache')
            except Exception:
                pass

            self.log('正在打开微软登录网关 (https://login.live.com/)...')
            self.message = '正在加载微软登录网关 (login.live.com)...'
            browser.navigate(self.cdp, 'https://login.live.com/')
            time.sleep(2)

            username_injected = False
            username_injected_at = 0
            password_injected = False
            password_injected_at = 0

            deadline = time.time() + 180
            while time.time() < deadline and not self.stop_requested:
                self.updated_at = time.time()

                # 检查浏览器进程健康状态
                if self.ch and self.ch.proc and self.ch.proc.poll() is not None:
                    self.status = 'error'
                    self.message = 'Chromium 浏览器进程异常终止'
                    self.log('❌ Chromium 浏览器进程已退出')
                    break

                try:
                    curr_url = browser.ev(self.cdp, 'location.href') or ''
                    if curr_url.startswith('chrome-error://') or 'ERR_' in curr_url:
                        self.status = 'error'
                        self.message = '网络或代理连接失败，无法加载微软登录页，请核对网络与代理'
                        self.log(f'❌ 浏览器网络加载失败: {curr_url}')
                        break

                    # A. 优先检查当前是否有有效微软登录态并同步激活 Bing
                    try:
                        res = self.cdp.call('Network.getCookies', {'urls': ['https://login.live.com', 'https://www.bing.com', 'https://account.microsoft.com', 'https://rewards.bing.com']})
                        cks = res.get('cookies', [])
                        diag = cookie_mod.validate_cookies(cks)
                        cookie_names = {c.get('name') for c in cks}
                        has_ms_auth = bool({'ESTSAUTH', 'ESTSAUTHPERSISTENT', 'WLSSC', 'RPSTAuth'} & cookie_names)
                        has_post_url = any(u in curr_url for u in ('account.microsoft.com', 'rewards.bing.com', 'myaccount.microsoft.com', 'privacynotice.account.microsoft.com', 'complete-client-signin'))
                        
                        # 严防在依然处于账号输入、密码输入、保持登录确认、验证码或人机质询阶段时误判登录完成
                        state_peek = inspect_login_page(self.cdp)
                        st_peek_type = state_peek.get('type')
                        in_challenge = st_peek_type in ('username_entry', 'password_entry', 'auth_push', 'otp_input', 'dismissible_prompt', 'arkose_challenge', 'proof_selection', 'switch_to_password', 'kmsi')
                        is_authenticated = diag.get('has_auth_u') or has_post_url or (st_peek_type == 'success_url') or (password_injected and has_ms_auth and not in_challenge and 'login.live.com' not in curr_url)

                        if is_authenticated:
                            self.status = 'running'
                            self.message = '检测到已进入微软会话，正在同步激活 Bing / Rewards 登录态...'
                            self.log('检测到已进入微软授权会话，正在导航至 Bing 激活登录态与固化凭据...')
                            
                            # 导航至 Bing 并触发 SSO 认证握手
                            browser.navigate(self.cdp, 'https://www.bing.com/')
                            time.sleep(3)
                            browser.ev(self.cdp, '''(() => {
                                const el = document.querySelector("#id_l, #id_s, a[href*='fd/auth/signin'], a[href*='windows_live_id']");
                                if (el) el.click();
                            })()''')
                            time.sleep(3)
                            
                            curr_u = browser.ev(self.cdp, 'location.href') or ''
                            if not any(k in curr_u for k in ('wlsso', 'wlexpsignin')):
                                browser.navigate(self.cdp, 'https://www.bing.com/fd/auth/signin/v2?action=interactive&provider=windows_live_id&return_url=https%3a%2f%2fwww.bing.com%2f')
                                time.sleep(3)

                            browser.navigate(self.cdp, 'https://rewards.bing.com/')
                            time.sleep(3)

                            final_cks = []
                            diag2 = {'has_auth_u': False}
                            for _ in range(8):
                                res2 = self.cdp.call('Network.getCookies', {'urls': ['https://login.live.com', 'https://www.bing.com', 'https://account.microsoft.com', 'https://rewards.bing.com']})
                                final_cks = res2.get('cookies', [])
                                diag2 = cookie_mod.validate_cookies(final_cks)
                                if diag2['has_auth_u']:
                                    break
                                time.sleep(1.5)

                            if diag2['has_auth_u']:
                                config.save_json(config.pfile(self.name, 'cookie.json'), final_cks)
                                self.log(f'✅ 成功捕获并保存全量 Cookie (共 {len(final_cks)} 条, 含 _U=True)')
                                self.message = '成功捕获全量 Cookie，正在静默换取 OAuth2 Token...'
                                self.log('正在自动通过登录会话静默换取 OAuth2 Token...')
                                tok = exchange_token_from_cookies(self.name, proxy=self.proxy, cdp=self.cdp, ch=self.ch, log_cb=self.log)
                                rg = config.load_json(config.pfile(self.name, 'region.json'), {}) or {}

                                if self.save_credential_flag and self.username and self.password:
                                    try:
                                        config.save_credential(self.name, self.username, self.password)
                                        self.log('已安全保存微软账号密码凭据，后续会话失效时将支持全自动静默自愈重登')
                                    except Exception as ex:
                                        self.log(f'保存账号密码凭据异常: {ex}')

                                self.result = {
                                    'status': 'ok',
                                    'message': '微软账号自动化登录成功！Cookie 与 Token 双凭据已全部就绪。',
                                    'cookie_count': len(final_cks),
                                    'has_token': bool(tok),
                                    'balance': rg.get('balance', 0),
                                    'country': rg.get('country', '未知')
                                }
                                self.status = 'success'
                                self.message = f'🎉 登录成功！所有凭据已自动固化就绪（积分: {rg.get("balance", 0)}, 区域: {rg.get("country", "未知")}）。'
                                break
                            else:
                                self.log('⚠️ 微软会话已建立但 Bing 尚未就绪 _U 凭据，继续等待页面完成 SSO 同步...')
                    except Exception:
                        pass

                    # B. 检测页面状态
                    state = inspect_login_page(self.cdp)
                    st_type = state.get('type')

                    if st_type == 'error':
                        self.status = 'error'
                        self.error_msg = state.get('error')
                        self.message = f'登录异常: {self.error_msg}'
                        self.log(f'❌ 微软错误提示: {self.error_msg}')
                        break

                    elif st_type == 'username_entry':
                        if not username_injected or (time.time() - username_injected_at > 8):
                            self.status = 'running'
                            self.message = f'正在自动填入微软账号 ({self.username})...'
                            self.log(f'正在填入微软账号 ({self.username})...')
                            user_ok = browser.ev(self.cdp, f'''(() => {{
                                const input = document.querySelector('#usernameEntry, input[name=loginfmt], input[type=email], #i0116');
                                if (input) {{
                                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                                    if (setter) {{
                                        setter.call(input, {json.dumps(self.username)});
                                    }} else {{
                                        input.value = {json.dumps(self.username)};
                                    }}
                                    input.dispatchEvent(new Event('input', {{bubbles: true}}));
                                    input.dispatchEvent(new Event('change', {{bubbles: true}}));
                                    return true;
                                }}
                                return false;
                            }})()''')
                            if user_ok:
                                username_injected = True
                                username_injected_at = time.time()
                                self.message = '账号已填入，正在提交...'
                                time.sleep(0.8)
                                browser.ev(self.cdp, '''(() => {
                                    const btn = document.querySelector('#idSIButton9, button[type=submit], input[type=submit], #iNext');
                                    if (btn) {
                                        btn.removeAttribute('disabled');
                                        btn.click();
                                    }
                                    const inp = document.querySelector('#usernameEntry, input[name=loginfmt], input[type=email], #i0116');
                                    if (inp) {
                                        inp.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                                        inp.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                                    }
                                })()''')
                                self.message = '账号已提交，正在等待微软响应...'
                                time.sleep(2.5)

                    elif st_type == 'switch_to_password':
                        self.status = 'running'
                        self.message = '检测到非密码默认选项，正在自动切换至“使用密码”模式...'
                        self.log('检测到非密码默认选项，自动切换至“使用密码”模式')
                        x = state.get('x')
                        y = state.get('y')
                        if x is not None and y is not None and (x > 0 or y > 0):
                            browser.mouse_click(self.cdp, x, y)
                        else:
                            browser.ev(self.cdp, '''(() => {
                                const leaf = Array.from(document.body ? document.body.querySelectorAll('*') : []).find(x => {
                                    const t = (x.innerText || '').trim().toLowerCase();
                                    return (t === '使用密码' || t === '使用你的密码' || t === 'use password' || t === 'use your password' || t === 'use password instead') && x.children.length === 0;
                                });
                                if (leaf) {
                                    leaf.click();
                                    const target = leaf.closest('a, button, [role=button], .fui-Link');
                                    if (target) target.click();
                                }
                            })()''')
                        time.sleep(3)

                    elif st_type == 'password_entry':
                        if not password_injected or (time.time() - password_injected_at > 8):
                            self.status = 'running'
                            self.message = '正在填入密码并提交登录...'
                            self.log('正在填入密码并提交...')
                            pwd_ok = browser.ev(self.cdp, f'''(() => {{
                                const p = document.querySelector('#passwordEntry, input[name=passwd], input[type=password], #i0118');
                                if (p) {{
                                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                                    if (setter) {{
                                        setter.call(p, {json.dumps(self.password)});
                                    }} else {{
                                        p.value = {json.dumps(self.password)};
                                    }}
                                    p.dispatchEvent(new Event('input', {{bubbles: true}}));
                                    p.dispatchEvent(new Event('change', {{bubbles: true}}));
                                    return true;
                                }}
                                return false;
                            }})()''')
                            if pwd_ok:
                                password_injected = True
                                password_injected_at = time.time()
                                self.message = '密码已填入，正在提交...'
                                time.sleep(0.8)
                                browser.ev(self.cdp, '''(() => {
                                    const btn = document.querySelector('#idSIButton9, button[type=submit], input[type=submit]');
                                    if (btn) {
                                        btn.removeAttribute('disabled');
                                        btn.click();
                                    }
                                    const p = document.querySelector('#passwordEntry, input[name=passwd], input[type=password], #i0118');
                                    if (p) {
                                        p.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                                        p.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
                                    }
                                })()''')
                                self.message = '密码已提交，正在等待微软校验与凭据固化...'
                                time.sleep(2.5)

                    elif st_type == 'kmsi':
                        self.status = 'running'
                        self.message = '正在自动确认“保持登录状态” (KMSI)...'
                        self.log('处理“保持登录状态” (KMSI) 自动确认...')
                        browser.ev(self.cdp, '''(() => {
                            const btns = Array.from(document.querySelectorAll('button, input[type=submit]'));
                            const yesBtn = btns.find(b => {
                                const t = (b.innerText || b.value || '').trim().toLowerCase();
                                return t === '是' || t === 'yes';
                            }) || document.querySelector('#acceptButton, #idSIButton9, input[value="是"], input[value="Yes"]');
                            if (yesBtn) yesBtn.click();
                        })()''')
                        time.sleep(2.5)

                    elif st_type == 'auth_push':
                        code = state.get('code')
                        self.status = 'wait_auth_push'
                        self.auth_code = code
                        self.message = f'微软触发手机 Authenticator 验证，请在手机应用上选择数字：【 {code} 】'
                        self.log(f'>>> [安全提示] 微软触发手机 Authenticator 二次验证，匹配数字: 【 {code} 】')
                        time.sleep(3)

                    elif st_type == 'otp_input':
                        prompt = state.get('prompt') or '请输入安全验证码'
                        self.status = 'wait_otp'
                        self.otp_prompt = prompt
                        self.message = f'微软需要输入安全验证码: {prompt}'
                        time.sleep(2)

                    elif st_type in ('arkose_challenge', 'proof_selection'):
                        self.status = 'wait_interactive'
                        if st_type == 'arkose_challenge':
                            self.message = '检测到人机拼图验证码(Arkose)，请在下方实时交互窗口中点击完成验证'
                        else:
                            self.message = '检测到微软身份验证方式选择，请在下方实时交互窗口中点击选择'
                        time.sleep(2)

                    elif st_type == 'dismissible_prompt':
                        self.status = 'running'
                        self.message = '检测到设备密钥/通行密钥/推广弹窗，正在自动跳过/取消以返回登录流程...'
                        self.log('检测到设备密钥/安全密钥/免密推广弹窗，正在自动点击取消或跳过...')
                        dismiss_res = browser.ev(self.cdp, '''(() => {
                            const directSelectors = [
                                '#iCancel', '#cancelButton', '[data-test-id="cancelButton"]',
                                '#iShowSkip', '#skipButton', '[data-test-id="skipButton"]',
                                '#idBtn_Back', '#idA_PWD_SwitchToPassword', '#signInAnotherWay'
                            ];
                            for (const s of directSelectors) {
                                const el = document.querySelector(s);
                                if (el && el.offsetParent !== null) {
                                    el.click();
                                    return { success: true, target: s };
                                }
                            }
                            const btns = Array.from(document.querySelectorAll('button, a, input[type=button], input[type=submit], [role=button]'));
                            const candidate = btns.find(el => {
                                if (el.offsetParent === null) return false;
                                const t = (el.innerText || el.value || '').trim().toLowerCase();
                                return ['取消', '以后再说', '暂不', '跳过', '使用密码', '使用你的密码', 'cancel', 'skip for now', 'not now', 'no thanks', 'skip', 'use password', 'use your password'].includes(t);
                            });
                            if (candidate) {
                                candidate.click();
                                return { success: true, target: candidate.innerText || candidate.value };
                            }
                            return { success: false };
                        })()''')
                        if dismiss_res and dismiss_res.get('success'):
                            self.log(f'已自动点击跳过/取消目标: {dismiss_res.get("target")}')
                        else:
                            self.log('未能自动定位跳过/取消按钮，等待用户实时交互接管')
                            self.status = 'wait_interactive'
                            self.message = '检测到设备密钥/验证弹窗，请在下方实时交互画面中点击“取消”或“跳过”'
                        time.sleep(2.5)

                    elif st_type == 'generic_next':
                        browser.ev(self.cdp, 'document.querySelector("#iNext, #iLandingViewAction, #idSIButton9, input[type=submit]")?.click()')
                        time.sleep(2)

                    else:
                        title = browser.ev(self.cdp, 'document.title') or ''
                        self.status = 'running'
                        self.message = f'正在等待微软页面加载 ({title or curr_url[:40]})...'
                        time.sleep(1.5)

                except browser.StepError as cdp_err:
                    self.log(f'CDP 通信瞬态恢复中: {cdp_err}')
                    try:
                        if self.cdp:
                            self.cdp._reconnect()
                    except Exception:
                        pass
                    time.sleep(1.5)
                except Exception as loop_ex:
                    self.log(f'登录循环暂态容错: {loop_ex}')
                    time.sleep(1.5)

            if not self.result and self.status != 'error':
                if self.stop_requested:
                    self.status = 'error'
                    self.message = '登录已取消'
                else:
                    self.status = 'error'
                    self.message = '登录超时 (180秒未完成)，可能存在未通过的风控验证'

        except Exception as ex:
            self.status = 'error'
            self.error_msg = str(ex)
            self.message = f'异常: {ex}'
            self.log(f'登录过程发生异常: {ex}')
        finally:
            self._cleanup()


_active_sessions = {}
_sessions_lock = threading.Lock()


def get_session(name):
    with _sessions_lock:
        return _active_sessions.get(name)


def start_session(name, username, password, proxy=None, log_cb=None, save_credential_flag=True):
    with _sessions_lock:
        if name in _active_sessions:
            try:
                _active_sessions[name].cancel()
            except Exception:
                pass
        sess = LoginSession(name, username, password, proxy=proxy, log_cb=log_cb, save_credential_flag=save_credential_flag)
        _active_sessions[name] = sess
        sess.start()
        return sess


def cancel_session(name):
    with _sessions_lock:
        sess = _active_sessions.pop(name, None)
        if sess:
            sess.cancel()


def submit_session_otp(name, code):
    sess = get_session(name)
    if not sess:
        return {'status': 'error', 'message': '未找到活跃的登录会话'}
    return sess.submit_otp(code)


def session_mouse_click(name, x, y):
    sess = get_session(name)
    if not sess:
        return {'status': 'error', 'message': '未找到活跃的登录会话'}
    return sess.dispatch_mouse_click(x, y)


def get_session_screenshot(name):
    sess = get_session(name)
    if not sess:
        return None
    return sess.get_screenshot()


def login_with_credential(name, username, password, proxy=None, log_cb=None, save_credential_flag=True):
    """现代化微软账号密码全自动登录与多重风控质询接管。"""
    session = start_session(name, username, password, proxy=proxy, log_cb=log_cb, save_credential_flag=save_credential_flag)
    deadline = time.time() + 180
    prompted_auth_code = None
    prompted_otp = False

    while time.time() < deadline:
        if session.status == 'success':
            return session.result
        if session.status == 'error':
            raise RuntimeError(session.error_msg or session.message)

        if session.status == 'wait_auth_push' and session.auth_code != prompted_auth_code:
            prompted_auth_code = session.auth_code
            if log_cb:
                log_cb(f'>>> [安全提示] 微软触发手机 Authenticator 验证，请在手机上选择数字：【 {session.auth_code} 】')

        if session.status == 'wait_otp' and not prompted_otp:
            prompted_otp = True
            if sys.stdin and sys.stdin.isatty():
                print(f'\n>>> 微软要求输入安全验证码 ({session.otp_prompt or "邮箱/短信"}):', flush=True)
                try:
                    code = input('请输入验证码: ').strip()
                    if code:
                        session.submit_otp(code)
                except Exception:
                    pass
            else:
                if log_cb:
                    log_cb(f'>>> [安全提示] 微软要求输入安全验证码: {session.otp_prompt} (请在 Web 控制台输入提交)')

        if session.status == 'wait_interactive':
            if log_cb:
                log_cb('>>> [风控提示] 检测到微软人机拼图/复杂安全质询，请在 Web 控制台进行远程交互操作')

        time.sleep(1)

    session.cancel()
    raise RuntimeError('登录超时或等待验证超时')


def get_auth_status(name):
    """检查账号登录凭据的有效性状态。返回 dict。"""
    from msr import config, cookies as cookie_mod
    p = config.profile(name)
    tok_f = os.path.join(p, 'token.json')
    ck_f = os.path.join(p, 'cookie.json')

    has_token = False
    if os.path.isfile(tok_f):
        t = config.load_json(tok_f, {}) or {}
        if t.get('refresh_token') or t.get('access_token'):
            has_token = True

    has_cookie = False
    cookie_count = 0
    has_u = False
    if os.path.isfile(ck_f):
        cks = config.load_json(ck_f, []) or []
        cookie_count = len(cks)
        diag = cookie_mod.validate_cookies(cks)
        has_cookie = bool(cks)
        has_u = diag.get('has_auth_u', False)

    valid = has_token or has_u
    if has_token and has_u:
        auth_type = 'full'
        summary = '有效 (Token+Cookie双就绪)'
    elif has_u:
        auth_type = 'cookie'
        summary = '有效 (Cookie 就绪)'
    elif has_token:
        auth_type = 'oauth'
        summary = '有效 (OAuth 就绪，待补Cookie)'
    else:
        auth_type = 'none'
        summary = '未登录或凭据失效'

    return {
        'valid': valid,
        'auth_type': auth_type,
        'has_token': has_token,
        'has_cookie': has_cookie,
        'has_u': has_u,
        'cookie_count': cookie_count,
        'summary': summary
    }
