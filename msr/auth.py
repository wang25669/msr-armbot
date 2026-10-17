"""微软账号多方式登录与凭据固化系统。

支持四种应对微软不同风控与环境的登录策略:
1. device-code (推荐): 官方 OAuth2 设备码流，纯终端输出链接与代码，手机/任意浏览器授权，免密且无惧 2FA。
2. cookies: 导入 Cookie-Editor / EditThisCookie / Raw 请求头文本，100% 绕过人机滑块与验证码。
3. interactive: 启动 Headless/GUI 远程调试浏览器，用户在浏览器中手动登录，脚本自动嗅探会话并固化 Cookie。
4. credential: 自动填入账号密码，智能处理“保持登录”确认，遇到二次验证(2FA)自动降级等待用户手机确认。
"""
import json
import os
import sys
import time

from msr import cookies as cookie_mod


def cmd_login(args):
    from msr import config, api, probe

    name = args.name
    method = getattr(args, 'method', None) or 'device'
    proxy = getattr(args, 'proxy', None)
    username = getattr(args, 'username', None)
    password = getattr(args, 'password', None)
    cookie_file = getattr(args, 'cookies', None)

    p = config.profile(name)
    config.log(name, '=== 登录引导开始 (method=%s proxy=%s) ===' % (method, proxy or '直连'))

    # 若指定了 cookie_file 或显式选择 cookies 方式
    if cookie_file or method == 'cookies':
        _login_by_cookies(name, cookie_file)
    elif method == 'interactive':
        _login_by_interactive(name, p, config, proxy=proxy)
    elif method == 'credential' or (username and password):
        _login_by_credential(name, p, config, username, password, proxy=proxy)
    else:
        # 默认推荐: 设备码流
        _login_by_device_code(name, p, config, api, probe, proxy=proxy)

    # 检查并引导浏览器 Cookie (若尚未导入)
    ck_path = config.pfile(name, 'cookie.json')
    if not os.path.isfile(ck_path):
        print('\n提示: 该账号尚未配置浏览器 Cookie。若要跑浏览器任务(搜索/每日活动/打卡)，建议补:')
        print(f'  msr login {name} --cookies <导出的cookies.json>')
        print(f'  或使用交互式登录: msr login {name} --method interactive')

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


def _login_by_device_code(name, profile_path, config, api, probe, proxy=None):
    """方式 1: 微软官方 OAuth2 设备码流 (Device Code Flow)。"""
    print('\n[1/2] 微软官方设备码授权 (免密码、支持 2FA、手机/任意浏览器打开):')
    dc = api.device_code_login(name, proxy=proxy)
    uri = dc.get('verification_uri', 'https://microsoft.com/devicelogin')
    code = dc.get('user_code')
    print(f'  1. 打开授权网址: {uri}')
    print(f'  2. 输入设备代码: {code}')
    print('  3. 登录并同意授权中...', flush=True)

    t = api.poll_device_code(name, dc['device_code'],
                             interval=int(dc.get('interval', 5)),
                             proxy=proxy)
    api.save_token(name, t, profile_path)
    config.log(name, '设备码授权成功，OAuth2 Token 已持久化保存')

    print('\n[2/2] 探测账号区域与任务特权:')
    rg_path = config.pfile(name, 'region.json')
    tok = config.load_json(config.pfile(name, 'token.json'))
    pr = probe.probe_account(name, tok['access_token'], proxy=proxy)
    config.save_json(rg_path, pr)
    print('  国家/区域 : %s' % pr.get('country'))
    print('  系统语言  : %s' % pr.get('language'))
    print('  当前等级  : %s' % (pr.get('levelInfo') or {}).get('activeLevel'))
    print('  当前余额  : %s' % pr.get('balance'))
    print('  可用任务  : %s' % ', '.join(pr.get('task_names', [])))


def _login_by_cookies(name, cookie_file=None):
    """方式 2: Cookie 结构化导入与智能清洗。"""
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
        from msr import config
        dest = config.pfile(name, 'cookie.json')
        config.save_json(dest, cookies)
        diag = cookie_mod.validate_cookies(cookies)

    print(f"Cookie 校验结果: 条数={diag['count']}, 核心登录凭据(_U)={diag['has_auth_u']}, 状态={diag['reason']}")


def _login_by_interactive(name, profile_path, config, proxy=None):
    """方式 3: 启动带有远程调试的 Headless Chromium，自动嗅探已登录 Cookie。"""
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
    try:
        ch.stop()
    except Exception:
        pass


def _login_by_credential(name, profile_path, config, username, password, proxy=None):
    """方式 4: 自动键入账号密码，自适应 2FA 提示。"""
    from msr import browser
    chrome_dir = os.path.join(profile_path, 'chrome')
    ch = browser.Chromium(user_data_dir=chrome_dir, proxy=proxy, log=lambda m: config.log(name, m))
    ch.start()
    tab = ch.new_tab('https://login.live.com/')
    cdp = ch.attach(tab)

    print(f'\n正在以自动化表单方式登录账号: {username} ...')
    try:
        time.sleep(3)
        # 输入用户名
        cdp.call('Runtime.evaluate', {
            'expression': f'''(() => {{
                const i = document.querySelector('input[type=email], input[name=loginfmt]');
                if (i) {{ i.value = "{username}"; i.dispatchEvent(new Event("input")); return true; }}
                return false;
            }})()'''
        })
        time.sleep(1)
        cdp.call('Runtime.evaluate', {'expression': 'document.querySelector("#idSIButton9, input[type=submit]")?.click()'})
        time.sleep(3)

        # 输入密码
        cdp.call('Runtime.evaluate', {
            'expression': f'''(() => {{
                const p = document.querySelector('input[type=password], input[name=passwd]');
                if (p) {{ p.value = "{password}"; p.dispatchEvent(new Event("input")); return true; }}
                return false;
            }})()'''
        })
        time.sleep(1)
        cdp.call('Runtime.evaluate', {'expression': 'document.querySelector("#idSIButton9, input[type=submit]")?.click()'})
        time.sleep(4)

        # 处理 "保持登录状态?" 提示
        cdp.call('Runtime.evaluate', {'expression': 'document.querySelector("#acceptButton, input[type=submit]")?.click()'})
        time.sleep(3)

        # 检查是否要求二次验证
        needs_2fa = browser.ev(cdp, '''(() => {
            const t = document.body.innerText || "";
            return t.includes("验证您的身份") || t.includes("Authenticator") || t.includes("批准请求") || t.includes("安全代码");
        })()''')
        if needs_2fa:
            print('>>> [安全提示] 微软触发了二次验证 (2FA / Authenticator)，请在手机微软验证器上点击确认或输入代码，脚本等待最多 90 秒...')
            for _ in range(30):
                time.sleep(3)
                url = browser.ev(cdp, 'location.href') or ''
                if 'bing.com' in url or 'account.microsoft.com' in url or 'myaccount' in url:
                    break

        # 导航到 Bing 落地并提取 Cookie
        browser.navigate(cdp, 'https://www.bing.com/')
        time.sleep(3)
        res = cdp.call('Network.getCookies', {'urls': ['https://login.live.com', 'https://www.bing.com']})
        cks = res.get('cookies', [])
        diag = cookie_mod.validate_cookies(cks)
        if diag['has_auth_u'] or diag['has_muid']:
            config.save_json(config.pfile(name, 'cookie.json'), cks)
            print(f"[OK] 账号密码自动化登录成功! Cookie 已保存 (共 {len(cks)} 条)")
        else:
            print(f"登录完成但 Cookie 待确认 (条数: {len(cks)})")
    finally:
        ch.stop()


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
    elif has_token:
        auth_type = 'oauth'
        summary = '有效 (OAuth 就绪，待补Cookie)'
    elif has_u:
        auth_type = 'cookie'
        summary = '有效 (Cookie 就绪)'
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

