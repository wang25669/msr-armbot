"""MSR 账号多方式登录与凭据固化系统。

支持两大核心登录与凭据闭环策略:
1. credential (推荐): 现代化账号密码自动化登录，原生适配 MSR Fluent UI，支持智能密码切换、保持登录确认与 2FA 提示，自动提取 Cookie 并无感换取 OAuth Token。
2. cookies (备选): 导入 Cookie-Editor JSON 数组或 Raw 请求头文本，自动清洗并基于已登录会话静默换取 OAuth Token。
"""
import json
import os
import sys
import time
import urllib.parse

from msr import browser
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
    else:
        # 账号密码登录 (只走账号+密码; 遇到验证码/推送/拼图会返回 challenge, 需去控制台手动登录)
        if not username:
            username = input('请输入 MSR 账号邮箱 (例如 xxx@outlook.com): ').strip()
        if not password:
            import getpass
            password = getpass.getpass('请输入 MSR 账号密码: ').strip()
        try:
            res = login_with_credential(name, username, password, proxy=proxy, log_cb=print)
        except Exception as ex:
            config.log(name, '登录失败: %s' % str(ex)[:160])
            return
        st = (res or {}).get('status')
        if st == 'challenge':
            config.log(name, (res or {}).get('message') or '需要人工接管')
        elif st != 'ok':
            config.log(name, '登录未完成: %s' % res)
            return

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
    """通过无头浏览器中已注入的 MSR Cookie，静默单点登录自动获取 OAuth2 Token。"""
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
        # 若是复用已有登录会话的 cdp，浏览器已持有全量 Cookie，批量同步仅需数毫秒
        browser.inject_cookies(cdp, cookies, log=log)
        try:
            cdp.call('Network.enable')
        except Exception:
            pass

        code_holder = []
        def on_event(ev):
            if ev.get('method') == 'Network.requestWillBeSent':
                url = ev.get('params', {}).get('request', {}).get('url', '')
                if 'code=' in url and ('oauth20_desktop' in url or 'login.live.com' in url):
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

        # 放宽超时等待时间至 30s，适应高延迟网络/代理
        deadline = time.time() + 30
        while time.time() < deadline:
            if hasattr(cdp, 'pump_events'):
                cdp.pump_events(timeout=0.1)
            if code_holder:
                break
            # 双重冗余保障：主动检查当前页面的 location.href 是否已完成重定向
            try:
                curr_url = browser.ev(cdp, 'window.location.href', timeout=1) or ''
                if 'code=' in curr_url and ('oauth20_desktop' in curr_url or 'login.live.com' in curr_url):
                    parsed = urllib.parse.urlparse(curr_url)
                    qs = urllib.parse.parse_qs(parsed.query)
                    c = qs.get('code', [None])[0]
                    if c:
                        code_holder.append(c)
                        break
            except Exception:
                pass
            time.sleep(0.1)

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


# 登录流程入口 (供联调/自测覆盖)
LOGIN_ENTRY = 'https://login.live.com/'

# 仍处于登录流程的域名 (停在这些域上不算登录完成)
LOGIN_HOSTS = ('login.live.com', 'login.microsoftonline.com', 'login.microsoft.com',
               'account.live.com', 'privacynotice.account.microsoft.com')
# 登录完成后会落到的业务域名
DONE_HOSTS = ('bing.com', 'rewards.bing.com', 'account.microsoft.com', 'myaccount.microsoft.com',
              'microsoft.com')


def is_logged_in_url(url):
    """当前 URL 是否已经离开登录流程 (落到 bing / rewards / account 等业务域)。"""
    if not url or url.startswith('chrome-error://') or 'ERR_' in url:
        return False
    try:
        host = (urllib.parse.urlparse(url).hostname or '').lower()
    except Exception:
        return False
    if not host:
        return False
    if any(host == h or host.endswith('.' + h) for h in LOGIN_HOSTS):
        return False
    return any(host == h or host.endswith('.' + h) for h in DONE_HOSTS)


# ---------- 登录页识别: 只认账号密码登录需要的几种状态 ----------
# 刻意不再处理 OTP / 辅助邮箱 / Authenticator 推送 / 人机拼图:
# 一旦出现这些质询, 说明该账号需要人工接管, 由控制台的「手动登录」完成。

_DETECT_JS = r"""(() => {
  const canSwitch = __CAN_SWITCH__;
  const text = (document.body ? document.body.innerText : '') || '';
  const url = location.href || '';
  const vis = el => el && el.offsetParent !== null;

  if (/此 Microsoft 帐户不存在|that microsoft account doesn't exist|account doesn't exist/i.test(text)) {
    return {type: 'error', error: '此微软账号不存在，请核对账号'};
  }
  if (/此帐户已被锁定|account has been locked|我们检测到异常活动|unusual activity/i.test(text)) {
    return {type: 'error', error: '账号被锁定或触发安全拦截，需人工处理'};
  }
  if (/密码不正确|password is incorrect|your account or password is incorrect/i.test(text)) {
    return {type: 'error', error: '密码不正确，请核对密码'};
  }

  // 强质询信号(有专门的验证码/拼图/推送元素): 无论是否有“使用密码”入口都交给人
  const challengeEl = Boolean(
    document.querySelector('iframe[src*="arkoselabs"], iframe[src*="arkose"], #enforcementFrame')
    || vis(document.querySelector('#idTxtBx_SAOTCC_OTC, input[name=otc], input[name=OTC], input[autocomplete="one-time-code"], #idTxtBx_OTC'))
    || vis(document.querySelector('#idTxtBx_SAOTCS_ProofConfirmation, input[name=ProofConfirmation]'))
    || document.querySelector('#idRichContext_DisplaySign, [data-test-id=displaySign]')
  );
  if (challengeEl) return {type: 'challenge'};

  if (/保持登录状态|stay signed in|无需每次都登录|reduce the number of times/i.test(text)) {
    return {type: 'kmsi'};
  }
  if (vis(document.querySelector('#passwordEntry, input[name=passwd], input[type=password], #i0118'))) {
    return {type: 'password'};
  }
  if (canSwitch) {
    const leaf = Array.from(document.body ? document.body.querySelectorAll('*') : []).find(x => {
      if (x.children.length > 0) return false;
      const t = (x.innerText || '').trim().toLowerCase();
      if (!(t === '使用密码' || t === '使用你的密码' || t === '使用密码登录' || t === 'use password'
            || t === 'use your password' || t === 'use password instead' || t === 'use a password instead')) return false;
      const r = x.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    });
    if (leaf) {
      const r = leaf.getBoundingClientRect();
      return {type: 'switch_password', x: r.x + r.width / 2, y: r.y + r.height / 2};
    }
  }
  if (vis(document.querySelector('#usernameEntry, input[name=loginfmt], input[type=email], #i0116'))) {
    return {type: 'username'};
  }
  // 文本型质询(没有专门元素, 只能靠文案): 放在“使用密码”之后, 否则会误判把可自动登录的页面当质询
  if (/输入验证码|输入代码|enter the code|almost there|就快完成了|验证你的身份|verify your identity|请解决谜题|证明你不是机器人/i.test(text)) {
    return {type: 'challenge'};
  }
  return {type: 'other', url: url};
})()"""


def probe_login_page(cdp, can_switch_to_password=True):
    """返回登录页当前状态: username / password / switch_password / kmsi / done / error / challenge / other。"""
    js = _DETECT_JS.replace('__CAN_SWITCH__', 'true' if can_switch_to_password else 'false')
    try:
        res = browser.ev(cdp, js)
        return res if isinstance(res, dict) else {'type': 'other'}
    except Exception as ex:
        return {'type': 'other', 'error': str(ex)[:80]}


_FILL_JS = """(() => {
  const el = document.querySelector(%s);
  if (!el) return false;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
  if (setter) setter.call(el, %s); else el.value = %s;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
})()"""

_SUBMIT_JS = """(() => {
  const btn = document.querySelector('#idSIButton9, #iNext, button[type=submit], input[type=submit]');
  if (btn) { btn.removeAttribute('disabled'); btn.click(); }
  const inp = document.querySelector('#usernameEntry, input[name=loginfmt], input[type=email], #i0116, #passwordEntry, input[name=passwd], input[type=password]');
  if (inp) {
    inp.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
    inp.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
  }
  return true;
})()"""

_KMSI_JS = """(() => {
  const btns = Array.from(document.querySelectorAll('button, input[type=submit]'));
  const yes = btns.find(b => {
    const t = (b.innerText || b.value || '').trim().toLowerCase();
    return t === '是' || t === 'yes';
  });
  if (yes) { yes.click(); return true; }
  return false;
})()"""

_SWITCH_PWD_JS = """(() => {
  const leaf = Array.from(document.body ? document.body.querySelectorAll('*') : []).find(x => {
    if (x.children.length > 0) return false;
    const t = (x.innerText || '').trim().toLowerCase();
    return t === '使用密码' || t === '使用你的密码' || t === '使用密码登录' || t === 'use password'
        || t === 'use your password' || t === 'use password instead' || t === 'use a password instead';
  });
  if (leaf) { (leaf.closest('a, button, [role=button], .fui-Link') || leaf).click(); return true; }
  return false;
})()"""

_SIGNIN_BTN_JS = """(() => {
  const el = document.querySelector('#id_l, #id_s, a[href*="fd/auth/signin"], a[href*="windows_live_id"]');
  if (el) { el.click(); return true; }
  return false;
})()"""

_BING_GUEST_JS = """(() => {
  const signIn = document.querySelector('#id_s, a[href*="fd/auth/signin"], a[href*="windows_live_id"]');
  const signed = document.querySelector('#id_rh, #id_rc, #id_n, #b_idRh');
  return Boolean(signIn && !signed);
})()"""


def _fill(cdp, selector, value):
    js = _FILL_JS % (json.dumps(selector), json.dumps(value), json.dumps(value))
    try:
        return bool(browser.ev(cdp, js))
    except Exception:
        return False


def drive_password_login(cdp, username, password, log=print, timeout=240):
    """只驱动「账号 -> (切换到密码) -> 密码 -> 保持登录」这四步。

    返回 (state, info):
      ('ok', info)        已登录
      ('challenge', info) 出现验证码/推送/拼图等质询 —— 不自动处理
      ('error', info)     账号/密码错误或账号被锁
      ('timeout', info)   超时
    """
    browser.navigate(cdp, LOGIN_ENTRY)
    time.sleep(2)
    deadline = time.time() + timeout
    injected = {'username': 0.0, 'password': 0.0}
    switch_clicks = 0
    while time.time() < deadline:
        can_switch = bool(password) and injected['password'] == 0 and switch_clicks < 3
        try:
            href = browser.ev(cdp, 'location.href', timeout=5) or ''
        except Exception:
            href = ''
        if is_logged_in_url(href):
            return 'ok', {'type': 'done', 'url': href}

        st = probe_login_page(cdp, can_switch)
        t = st.get('type')

        if t == 'done':
            return 'ok', st
        if t == 'error':
            return 'error', st
        if t == 'challenge':
            return 'challenge', st

        if t == 'username' and time.time() - injected['username'] > 8:
            if _fill(cdp, '#usernameEntry, input[name=loginfmt], input[type=email], #i0116', username):
                injected['username'] = time.time()
                log('已填入账号并提交')
                time.sleep(0.8)
                browser.ev(cdp, _SUBMIT_JS)
                time.sleep(2.2)
                continue

        elif t == 'switch_password':
            switch_clicks += 1
            log('检测到“使用密码”入口，切换到密码登录')
            try:
                browser.mouse_click(cdp, st.get('x') or 0, st.get('y') or 0)
            except Exception:
                pass
            browser.ev(cdp, _SWITCH_PWD_JS)
            time.sleep(2.0)
            continue

        elif t == 'password' and time.time() - injected['password'] > 8:
            if _fill(cdp, '#passwordEntry, input[name=passwd], input[type=password], #i0118', password):
                injected['password'] = time.time()
                log('已填入密码并提交')
                time.sleep(0.8)
                browser.ev(cdp, _SUBMIT_JS)
                time.sleep(2.5)
                continue

        elif t == 'kmsi':
            log('确认“保持登录状态”')
            browser.ev(cdp, _KMSI_JS)
            time.sleep(2.0)
            continue

        time.sleep(1.2)

    return 'timeout', {}


def password_login(name, username, password, proxy=None, log_cb=None, save_credential_flag=True,
                   ch=None, cdp=None, timeout=240):
    """账号密码自动登录（只走账号+密码，不处理任何风控质询）。

    遇到验证码/推送/辅助邮箱/拼图时立刻返回 {'status': 'challenge'}，
    由调用方通知用户去控制台「手动登录」完成；不会卡死等待。
    """
    from msr import fingerprint as fp_mod
    log = log_cb or (lambda m: config.log(name, m))
    own = ch is None or cdp is None
    if own:
        fp = fp_mod.load_or_create(name, proxy=proxy)
        ch = browser.Chromium(user_data_dir=os.path.join(config.profile(name), 'chrome'),
                              proxy=proxy, log=log, fingerprint=fp,
                              version=fp.get('edge_version', '141'))
        tabs = ch.start()
        pages = [t for t in (tabs or []) if t.get('type') == 'page']
        cdp = ch.attach(pages[0] if pages else ch.new_tab('about:blank'))
        log('已启动浏览器实例 (出口: %s)' % (proxy or '直连'))

    try:
        state, info = drive_password_login(cdp, username, password, log=log, timeout=timeout)
        if state == 'challenge':
            msg = '登录触发验证码/推送/辅助邮箱验证，无法自动完成，请在控制台用「手动登录」接管'
            log(msg)
            return {'status': 'challenge', 'message': msg}
        if state == 'error':
            raise RuntimeError(info.get('error') or '登录失败')
        if state == 'timeout':
            raise RuntimeError('登录超时（%.0f 秒内未完成）' % timeout)

        log('登录成功，正在同步 Bing 会话并固化凭据...')
        browser.navigate(cdp, 'https://www.bing.com/')
        time.sleep(1.5)
        try:
            if browser.ev(cdp, _BING_GUEST_JS):
                browser.ev(cdp, _SIGNIN_BTN_JS)
                time.sleep(2.5)
                cur = browser.ev(cdp, 'location.href') or ''
                if not any(k in cur for k in ('wlsso', 'wlexpsignin')):
                    browser.navigate(cdp, 'https://www.bing.com/fd/auth/signin/v2?action=interactive'
                                          '&provider=windows_live_id&return_url=https%3a%2f%2fwww.bing.com%2f')
                    time.sleep(2.5)
        except Exception as ex:
            log('Bing SSO 容忍: %s' % str(ex)[:80])

        browser.navigate(cdp, 'https://rewards.bing.com/')
        time.sleep(2.0)

        cks = cookie_mod.extract_cookies_from_cdp(cdp, log=log)
        diag = cookie_mod.validate_cookies(cks)
        if diag.get('has_auth_u'):
            cookie_mod.save_cookies_safely(name, cks, config=config, log=log)
        else:
            log('警告: 未取到 Bing 会话 Cookie (_U)，浏览器任务可能不可用')

        tok = exchange_token_from_cookies(name, proxy=proxy, cdp=cdp, ch=ch, log_cb=log)
        if save_credential_flag and username and password:
            try:
                config.save_credential(name, username, password)
                log('已保存账号密码凭据，下次会话失效可静默重登')
            except Exception as ex:
                log('保存凭据异常: %s' % ex)

        rg = config.load_json(config.pfile(name, 'region.json'), {}) or {}
        return {
            'status': 'ok',
            'message': '登录成功',
            'cookie_count': len(cks),
            'has_token': bool(tok),
            'balance': rg.get('balance', 0),
            'country': rg.get('country', '未知'),
        }
    finally:
        if own and ch:
            try:
                ch.stop()
            except Exception:
                pass


def login_with_credential(name, username, password, proxy=None, log_cb=None, save_credential_flag=True,
                          ch=None, cdp=None, timeout=240):
    """兼容旧调用点 (runner/executor/web) 的别名。"""
    return password_login(name, username, password, proxy=proxy, log_cb=log_cb,
                          save_credential_flag=save_credential_flag, ch=ch, cdp=cdp, timeout=timeout)


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
