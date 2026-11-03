"""MSR 账号多方式登录与凭据固化系统。

支持两大核心登录与凭据闭环策略:
1. credential (推荐): 现代化账号密码自动化登录，原生适配 MSR Fluent UI，支持智能密码切换、保持登录确认与 2FA 提示，自动提取 Cookie 并无感换取 OAuth Token。
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
            username = input('请输入 MSR 账号邮箱 (例如 xxx@outlook.com): ').strip()
        if not password:
            import getpass
            password = getpass.getpass('请输入 MSR 账号密码: ').strip()
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
    print('正在打开 MSR 登录页面: https://login.live.com/ ...')

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


def inspect_login_page(cdp, can_switch_to_password=True):
    """自适应探测 MSR 登录页的当前状态与风控质询类型。"""
    from msr import browser
    can_switch_js = 'true' if can_switch_to_password else 'false'
    script = f'(() => {{ const canSwitchToPassword = {can_switch_js};\n' + '''
        const text = (document.body ? document.body.innerText : '') || '';
        const url = location.href || '';
        const lowerText = text.toLowerCase();
        
        // 1. 成功页面判断
        if (url.includes('bing.com') || url.includes('account.microsoft.com') || url.includes('rewards.bing.com') || url.includes('privacynotice') || url.includes('myaccount.microsoft.com')) {
            return { type: 'success_url', url: url };
        }
        
        // 2. 账号与风控错误拦截
        if (text.includes('此 Microsoft 帐户不存在') || lowerText.includes("that microsoft account doesn't exist") || lowerText.includes("account doesn't exist")) {
            return { type: 'error', error: '此 MSR 帐户不存在，请核对用户名' };
        }
        if (text.includes('密码不正确') || text.includes('此密码不正确') || text.includes('该密码不正确') || lowerText.includes('password is incorrect') || lowerText.includes('your account or password is incorrect')) {
            return { type: 'error', error: '密码不正确，请核对密码' };
        }
        if (text.includes('此帐户已被锁定') || lowerText.includes('account has been locked') || text.includes('我们检测到异常活动') || lowerText.includes('unusual activity')) {
            return { type: 'error', error: '该 MSR 账户触发了安全风控拦截或已被锁定，需通过官方渠道重置' };
        }

        // 检查输入字段的验证错误信息 (如 FluentUI field validationMessage / inline error)
        const valMsgEl = document.querySelector('[class*="validationMessage"], [id*="validationMessage"], [role="alert"].error, #idSpan_SAOTCC_Error_OTC, #idSpan_SAOTCS_Error_ProofConfirmation');
        if (valMsgEl && valMsgEl.innerText.trim()) {
            const vt = valMsgEl.innerText.trim();
            if (vt.toLowerCase().includes('incorrect') || vt.includes('不正确') || vt.toLowerCase().includes("didn't work") || vt.includes('代码无效')) {
                if (document.querySelector('#idTxtBx_SAOTCC_OTC, input#otc, input[name=otc], input[name=code]')) {
                    return { type: 'otp_input', prompt: '输入的验证码不正确，请重新输入', is_error: true };
                }
                return { type: 'error', error: '密码不正确，请核对密码' };
            }
            if (vt.includes('不匹配') || vt.includes('不符') || vt.toLowerCase().includes("doesn't match") || vt.toLowerCase().includes('does not match')) {
                return { type: 'proof_confirm', prompt: '输入的辅助安全邮箱与系统记录不匹配，请核对后重新输入', is_error: true };
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

        // 6. 检测是否可切换至“使用密码”登录
        // 核心准则：若用户提供了密码且尚未输入过密码，优先自动切换至密码输入模式，绝不擅自将用户拉入辅助邮箱验证
        if (canSwitchToPassword && document.body) {
            const switchPwdEl = Array.from(document.body.querySelectorAll('*')).find(x => {
                const t = (x.innerText || '').trim().toLowerCase();
                const isMatch = (t === '使用密码' || t === '使用你的密码' || t === 'use password' || t === 'use your password' || t === 'use password instead' || t === 'use a password instead');
                if (!isMatch || x.children.length > 0) return false;
                const r = x.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && (x.offsetParent !== null || r.top > 0);
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

        // 8. 辅助安全邮箱/手机确认页面 (Proof Confirmation - 需填入完整辅助邮箱以触发发送验证码)
        const proofConfirmInput = document.querySelector('#idTxtBx_SAOTCS_ProofConfirmation, input[name=ProofConfirmation], #proof-confirmation-email-input');
        const sendCodeBtn = document.querySelector('#idSubmit_SAOTCS_SendCode');
        const hasSendCodeBtn = sendCodeBtn || Array.from(document.querySelectorAll('button, input[type=submit], input[type=button]')).some(b => {
            if (b.offsetParent === null) return false;
            const bt = (b.innerText || b.value || '').trim().toLowerCase();
            return bt.includes('send code') || bt.includes('发送代码') || bt.includes('获取代码') || bt.includes('发送验证码');
        });

        const isProofConfirmPage = Boolean(
            (proofConfirmInput && proofConfirmInput.offsetParent !== null) ||
            (hasSendCodeBtn && (
                lowerText.includes('enter the complete email') ||
                lowerText.includes('verify that this is your email') ||
                lowerText.includes('to verify that this is your email address') ||
                lowerText.includes('enter it here') ||
                text.includes('为验证这是你的电子邮件地址') ||
                text.includes('要验证这是否是你的电子邮件') ||
                text.includes('输入完整的电子邮件') ||
                text.includes('隐藏的字符') ||
                lowerText.includes('hidden part')
            ))
        );

        if (isProofConfirmPage) {
            // 严格匹配带星号掩码的辅助邮箱 (必须包含至少2个星号)，彻底杜绝误匹配登录主账号
            const maskedMatches = text.match(/[a-zA-Z0-9._%+-]*\*{2,}[a-zA-Z0-9._%+-]*@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g) || [];
            const masked = maskedMatches.length > 0 ? maskedMatches[0] : '';
            const phoneMatch = text.match(/(?:\+?\d{1,3}[\s-]*)?(?:\*{2,}[\s-]*)+\d{2,4}/);
            const hint = masked || (phoneMatch ? phoneMatch[0].trim() : '');

            let prompt = masked ? `请确认辅助安全邮箱 (${masked}) 以发送安全验证码` : (hint ? `请确认安全验证方式 (${hint}) 以发送验证码` : '请输入完整的辅助安全邮箱以发送安全验证码');
            const descEl = document.querySelector('#idDiv_SAOTCS_Description, #idDiv_SAOTCS_Title, .ext-description, p.text-body');
            if (descEl && descEl.innerText.trim()) {
                const dt = descEl.innerText.trim().replace(/\s+/g, ' ');
                if (!dt.includes('@outlook.') && !dt.includes('@hotmail.') && !dt.includes('@live.') && dt.length > 10) {
                    prompt = dt.slice(0, 140);
                }
            }
            return { type: 'proof_confirm', prompt: prompt, masked: hint };
        }

        // 9. 接收并输入 OTC 验证码页面 (OTP Input - 验证码已由 MSR 发送，需填入验证码)
        const otcInput = document.querySelector(
            '#idTxtBx_SAOTCC_OTC, input#otc, input[name=otc], input[name=OTC], input[name=code], input[name=Code], input[autocomplete="one-time-code"], input[inputmode="numeric"], #idTxtBx_OTC, #iOttText'
        );
        const hasOtpText = lowerText.includes('enter code') ||
                           lowerText.includes('enter the code') ||
                           lowerText.includes('enter the 6-digit code') ||
                           lowerText.includes('enter the 7-digit code') ||
                           text.includes('输入代码') ||
                           text.includes('输入验证码') ||
                           text.includes('代码已发送') ||
                           text.includes('验证码已发送') ||
                           lowerText.includes('sent a code') ||
                           lowerText.includes('we sent a code') ||
                           lowerText.includes("we've sent a code") ||
                           lowerText.includes('verify your email') ||
                           text.includes('输入你在电子邮件中收到的代码') ||
                           text.includes('输入你在短信中收到的代码');
        const isOtpPage = Boolean(
            (otcInput && otcInput.offsetParent !== null) ||
            (hasOtpText && !isProofConfirmPage && document.querySelector('input[type=text], input[type=tel], input[type=number], input:not([type=hidden])'))
        );

        if (isOtpPage) {
            const maskedMatches = text.match(/[a-zA-Z0-9._%+-]*\*{2,}[a-zA-Z0-9._%+-]*@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g) || [];
            const masked = maskedMatches.length > 0 ? maskedMatches[0] : '';
            let prompt = masked ? `MSR 已向辅助安全邮箱 (${masked}) 发送验证码，请输入收到的 6 位或 7 位代码` : 'MSR 要求输入安全验证码';
            const descEl = document.querySelector('#idDiv_SAOTCC_Description, .ext-description, #idDiv_SAOTCC_Title');
            if (descEl && descEl.innerText.trim()) {
                const dt = descEl.innerText.trim().replace(/\s+/g, ' ');
                if (!dt.includes('@outlook.') && dt.length > 10) {
                    prompt = dt.slice(0, 140);
                }
            }
            return { type: 'otp_input', prompt: prompt, masked: masked };
        }

        // 10. 验证方式选择 / "Almost there" (Proof Selection)
        // 涵盖现代 Fluent UI: "Almost there", "Just one more step to verify it's you", "Send a code to ..."
        // 以及经典: #idDiv_SAOTCS_Proofs, "验证你的身份", "选择接收代码的方式"
        const hasAlmostThere = lowerText.includes('almost there') || text.includes('就快完成了') || text.includes('只差一步') ||
                               lowerText.includes('just one more step to verify') || text.includes('只需再执行一步即可验证你的身份');
        const hasProofSelectionText = hasAlmostThere ||
                                      text.includes('验证你的身份') || lowerText.includes('verify your identity') ||
                                      text.includes('选择接收代码的方式') || lowerText.includes('how would you like to get your security code') ||
                                      lowerText.includes('send a code to') || text.includes('发送代码') ||
                                      lowerText.includes('already have a code') || text.includes('已有代码') ||
                                      lowerText.includes("don't recognize or have any of these") || text.includes('这些信息均无法使用');
        const proofsEl = document.querySelector('#idDiv_SAOTCS_Proofs, .tile-container, #idDiv_SAOTCS_Proofs_List, [data-testid*="proof"]');

        if (hasProofSelectionText || proofsEl) {
            const candidates = Array.from(document.querySelectorAll('button, [role="button"], .tile, a, div[tabindex="0"]')).filter(el => {
                if (el.offsetParent === null) return false;
                const t = (el.innerText || el.getAttribute('aria-label') || '').trim();
                const lower = t.toLowerCase();
                return lower.includes('send a code to') ||
                       lower.includes('send code to') ||
                       (t.includes('向') && (t.includes('发送代码') || t.includes('发送验证码'))) ||
                       lower.includes('email to') ||
                       lower.includes('text to') ||
                       (lower.includes('@') && (lower.includes('code') || lower.includes('代码')));
            });
            const targetEl = candidates[0] || document.querySelector('#idDiv_SAOTCS_Proofs .table-row, [data-testid*="proof"], .tile-container .tile, #idDiv_SAOTCS_Proofs button');
            let hint = '';
            if (targetEl) {
                hint = (targetEl.innerText || targetEl.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
            }
            return {
                type: 'proof_selection',
                hint: hint || '辅助安全邮箱/手机验证',
                has_clickable_proof: Boolean(targetEl)
            };
        }

        // 11. 人机拼图验证码 (Arkose Labs FunCaptcha)
        const arkose = document.querySelector('iframe[src*="arkoselabs"], iframe[src*="arkose"], #enforcementFrame, div[id*="wlspisp"]');
        if (arkose || text.includes('请解决谜题') || text.includes('证明你不是机器人') || text.includes('Please solve the puzzle')) {
            return { type: 'arkose_challenge' };
        }

        // 12. 设备密钥 / 通行密钥 / 安全密钥 / 摆脱密码 / 无密码免密拦截与可跳过弹窗
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

        // 13. 通用下一步 / 协议同意
        const nextBtn = document.querySelector('#iNext, #iLandingViewAction, #idSIButton9, input[type=submit], button[type=submit]');
        if (nextBtn && (text.includes('下一步') || text.includes('继续') || lowerText.includes('next') || lowerText.includes('continue'))) {
            return { type: 'generic_next' };
        }

        return { type: 'other', url: url };
    })()'''
    try:
        res = browser.ev(cdp, script)
        return res if isinstance(res, dict) else {'type': 'other'}
    except Exception as e:
        logger.debug(f'页面检测异常: {e}')
        return {'type': 'other'}


class LoginSession:
    """账号登录多重交互与风控接管会话管理类。"""

    def __init__(self, name, username, password, proxy=None, log_cb=None, save_credential_flag=True, ch=None, cdp=None):
        self.name = name
        self.username = username
        self.password = password
        self.proxy = proxy
        self.log_cb = log_cb
        self.save_credential_flag = save_credential_flag
        self.status = 'starting'  # starting, running, wait_auth_push, wait_proof_confirm, wait_otp, wait_interactive, success, error
        self.message = '正在初始化安全 Chromium 实例...'
        self.auth_code = None
        self.otp_prompt = None
        self.proof_hint = None
        self.error_msg = None
        self.logs = []
        self.created_at = time.time()
        self.updated_at = time.time()
        self.ch = ch
        self.cdp = cdp
        self.own_ch = ch is None
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
        if self.own_ch and self.ch:
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
        self.log(f'正在向 MSR 页面填入信息 (安全验证码/辅助邮箱): {code} ...')
        script = f'''(() => {{
            // 1. 查找输入框 (优先查找 ProofConfirmation 或 OTC 或任何可见的文本/邮件输入框)
            let inp = document.querySelector('#idTxtBx_SAOTCS_ProofConfirmation, input[name=ProofConfirmation], #proof-confirmation-email-input');
            let isProof = Boolean(inp && inp.offsetParent !== null);
            if (!isProof) {{
                inp = document.querySelector('#idTxtBx_SAOTCC_OTC, input#otc, input[name=otc], input[name=OTC], input[name=code], input[name=Code], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[type=tel], #idTxtBx_OTC, #iOttText');
            }}
            if (!inp || inp.offsetParent === null) {{
                inp = Array.from(document.querySelectorAll('input[type=email], input[type=tel], input[type=text], input[type=number], input:not([type=hidden])')).find(x => x.offsetParent !== null && !x.disabled);
            }}
            if (!inp) {{
                return {{ success: false, error: '未能定位输入框' }};
            }}

            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
            if (setter) {{
                setter.call(inp, {json.dumps(code)});
            }} else {{
                inp.value = {json.dumps(code)};
            }}
            inp.dispatchEvent(new Event('input', {{bubbles: true}}));
            inp.dispatchEvent(new Event('change', {{bubbles: true}}));

            // 2. 点击对应提交/发送按钮
            const directSelectors = [
                '#idSubmit_SAOTCS_SendCode',
                '#idSubmit_SAOTCC_Continue',
                'button#idSubmit_SAOTCC_Continue',
                'button#idSubmit_SAOTCS_SendCode',
                '#idSIButton9',
                'button[type=submit]',
                'input[type=submit]'
            ];
            let clickedBtn = null;
            for (const s of directSelectors) {{
                const btn = document.querySelector(s);
                if (btn && btn.offsetParent !== null && !btn.disabled) {{
                    btn.click();
                    clickedBtn = s;
                    break;
                }}
            }}
            if (!clickedBtn) {{
                const btns = Array.from(document.querySelectorAll('button, input[type=button], input[type=submit], a[role=button]')).filter(b => b.offsetParent !== null && !b.disabled);
                const match = btns.find(b => {{
                    const t = (b.innerText || b.value || '').trim().toLowerCase();
                    return ['发送代码', '发送验证码', '获取代码', 'send code', 'send a code', '验证', 'verify', '下一步', 'next', '确定', 'submit'].some(k => t.includes(k));
                }});
                if (match) {{
                    match.click();
                    clickedBtn = match.innerText || match.value;
                }}
            }}

            // 回车键兜底
            try {{
                inp.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}}));
                inp.dispatchEvent(new KeyboardEvent('keyup', {{key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}}));
            }} catch(e) {{}}

            return {{ success: true, isProof: isProof, clickedBtn: clickedBtn }};
        }})()'''
        res = browser.ev(self.cdp, script)
        if res and res.get('success'):
            self.status = 'running'
            if res.get('isProof'):
                self.message = '辅助邮箱已提交，正在请求 MSR 发送安全验证码...'
                self.log(f'辅助安全邮箱已提交 ({code})，正在等待 MSR 响应与下发验证码...')
            else:
                self.message = '验证码已提交，正在等待 MSR 校验与凭据固化...'
                self.log(f'安全验证码已提交 ({code})，正在等待 MSR 校验...')
            return {'status': 'ok'}
        return {'status': 'error', 'message': (res and res.get('error')) or '未能定位输入框，请确认当前画面是否在输入验证码或辅助邮箱'}

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
                'proof_hint': self.proof_hint,
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
            if self.own_ch or not self.ch or not self.cdp:
                self.log(f'正在启动安全无头 Chromium 实例 (账号: {self.username})...')
                self.ch = browser.Chromium(user_data_dir=chrome_dir, proxy=self.proxy, log=self.log)
                from msr import fingerprint
                fp = fingerprint.load_or_create(self.name)
                self.ch.fingerprint = fp

                tabs = self.ch.start()
                page_tabs = [t for t in tabs if t.get('type') == 'page']
                tab = page_tabs[0] if page_tabs else self.ch.new_tab('about:blank')
                self.cdp = self.ch.attach(tab)
            else:
                self.log(f'复用当前活跃 Chromium 实例原地执行登录 (账号: {self.username})...')

            self.status = 'running'
            self.message = 'Chromium 已就绪，正在准备纯净登录环境...'
            self.updated_at = time.time()

            # 启动前主动清理残留 Cookie 与网络缓存，杜绝历史状态或前序失败会话污染
            try:
                self.cdp.call('Network.clearBrowserCookies')
                self.cdp.call('Network.clearBrowserCache')
            except Exception:
                pass

            self.log('正在打开 MSR 登录网关 (https://login.live.com/)...')
            self.message = '正在加载 MSR 登录网关 (login.live.com)...'
            browser.navigate(self.cdp, 'https://login.live.com/')
            time.sleep(2)

            username_injected = False
            username_injected_at = 0
            password_injected = False
            password_injected_at = 0
            proof_clicks = 0
            switch_pwd_clicks = 0

            deadline = time.time() + 300
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
                        self.message = '网络或代理连接失败，无法加载 MSR 登录页，请核对网络与代理'
                        self.log(f'❌ 浏览器网络加载失败: {curr_url}')
                        break

                    # A. 优先检查当前是否有有效 MSR 登录态并同步激活 Bing
                    try:
                        res = self.cdp.call('Network.getCookies', {'urls': ['https://login.live.com', 'https://www.bing.com', 'https://account.microsoft.com', 'https://rewards.bing.com']})
                        cks = res.get('cookies', [])
                        diag = cookie_mod.validate_cookies(cks)
                        cookie_names = {c.get('name') for c in cks}
                        has_ms_auth = bool({'ESTSAUTH', 'ESTSAUTHPERSISTENT', 'WLSSC', 'RPSTAuth'} & cookie_names)
                        has_post_url = any(u in curr_url for u in ('account.microsoft.com', 'rewards.bing.com', 'myaccount.microsoft.com', 'privacynotice.account.microsoft.com', 'complete-client-signin'))
                        
                        # 严防在依然处于账号输入、密码输入、保持登录确认、验证码或人机质询阶段时误判登录完成
                        can_switch = bool(self.password) and (not password_injected) and (switch_pwd_clicks < 3)
                        state_peek = inspect_login_page(self.cdp, can_switch_to_password=can_switch)
                        st_peek_type = state_peek.get('type')
                        in_challenge = st_peek_type in ('username_entry', 'password_entry', 'auth_push', 'otp_input', 'proof_confirm', 'dismissible_prompt', 'arkose_challenge', 'proof_selection', 'switch_to_password', 'kmsi')
                        is_authenticated = diag.get('has_auth_u') or has_post_url or (st_peek_type == 'success_url') or (password_injected and has_ms_auth and not in_challenge and 'login.live.com' not in curr_url)

                        if is_authenticated:
                            self.status = 'running'
                            self.message = '检测到已进入 MSR 会话，正在同步激活 Bing / MSR 登录态...'
                            self.log('检测到已进入 MSR 授权会话，正在导航至 Bing 激活登录态与固化凭据...')
                            
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
                            for _ in range(12):
                                final_cks = cookie_mod.extract_cookies_from_cdp(self.cdp, log=self.log)
                                diag2 = cookie_mod.validate_cookies(final_cks)
                                if diag2['has_auth_u']:
                                    break
                                time.sleep(2.0)

                            if diag2['has_auth_u']:
                                cookie_mod.save_cookies_safely(self.name, final_cks, config=config, log=self.log)
                                self.log(f'✅ 成功捕获并保存全量 Cookie (共 {len(final_cks)} 条, 含 _U=True)')
                                self.message = '成功捕获全量 Cookie，正在静默换取 OAuth2 Token...'
                                self.log('正在自动通过登录会话静默换取 OAuth2 Token...')
                                tok = exchange_token_from_cookies(self.name, proxy=self.proxy, cdp=self.cdp, ch=self.ch, log_cb=self.log)
                                rg = config.load_json(config.pfile(self.name, 'region.json'), {}) or {}

                                if self.save_credential_flag and self.username and self.password:
                                    try:
                                        config.save_credential(self.name, self.username, self.password)
                                        self.log('已安全保存 MSR 账号密码凭据，后续会话失效时将支持全自动静默自愈重登')
                                    except Exception as ex:
                                        self.log(f'保存账号密码凭据异常: {ex}')

                                self.result = {
                                    'status': 'ok',
                                    'message': 'MSR 账号自动化登录成功！Cookie 与 Token 双凭据已全部就绪。',
                                    'cookie_count': len(final_cks),
                                    'has_token': bool(tok),
                                    'balance': rg.get('balance', 0),
                                    'country': rg.get('country', '未知')
                                }
                                self.status = 'success'
                                self.message = f'🎉 登录成功！所有凭据已自动固化就绪（积分: {rg.get("balance", 0)}, 区域: {rg.get("country", "未知")}）。'
                                break
                            else:
                                self.log('⚠️ MSR 会话已建立但 Bing 尚未就绪 _U 凭据，继续等待页面完成 SSO 同步...')
                    except Exception:
                        pass

                    # B. 检测页面状态 (若用户提供了密码且尚未输入过密码，优先嗅探“使用密码”通道切换)
                    can_switch = bool(self.password) and (not password_injected) and (switch_pwd_clicks < 3)
                    state = inspect_login_page(self.cdp, can_switch_to_password=can_switch)
                    st_type = state.get('type')

                    if st_type == 'error':
                        self.status = 'error'
                        self.error_msg = state.get('error')
                        self.message = f'登录异常: {self.error_msg}'
                        self.log(f'❌ MSR 错误提示: {self.error_msg}')
                        break

                    elif st_type == 'username_entry':
                        if not username_injected or (time.time() - username_injected_at > 8):
                            self.status = 'running'
                            self.message = f'正在自动填入 MSR 账号 ({self.username})...'
                            self.log(f'正在填入 MSR 账号 ({self.username})...')
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
                                self.message = '账号已提交，正在等待 MSR 响应...'
                                time.sleep(2.5)

                    elif st_type == 'switch_to_password':
                        if password_injected:
                            self.log('已完成密码输入，当前处于后续二次质询阶段，忽略“使用密码”选项')
                            time.sleep(1.5)
                        else:
                            switch_pwd_clicks += 1
                            self.status = 'running'
                            self.message = '检测到非密码默认选项，正在自动切换至“使用密码”模式...'
                            self.log('检测到提供“使用密码”通道，正在自动切换至密码输入模式...')
                            x = state.get('x')
                            y = state.get('y')
                            if x is not None and y is not None and (x > 0 or y > 0):
                                try:
                                    browser.mouse_click(self.cdp, x, y)
                                except Exception:
                                    pass
                            browser.ev(self.cdp, '''(() => {
                                const leaf = Array.from(document.body ? document.body.querySelectorAll('*') : []).find(x => {
                                    const t = (x.innerText || '').trim().toLowerCase();
                                    return (t === '使用密码' || t === '使用你的密码' || t === 'use password' || t === 'use your password' || t === 'use password instead' || t === 'use a password instead') && x.children.length === 0;
                                });
                                if (leaf) {
                                    const target = leaf.closest('a, button, [role=button], .fui-Link') || leaf;
                                    target.click();
                                }
                            })()''')
                            time.sleep(2.5)

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
                                self.message = '密码已提交，正在等待 MSR 校验与凭据固化...'
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
                        self.message = f'MSR 触发手机 Authenticator 验证，请在手机应用上选择数字：【 {code} 】'
                        self.log(f'>>> [安全提示] MSR 触发手机 Authenticator 二次验证，匹配数字: 【 {code} 】')
                        time.sleep(3)

                    elif st_type == 'proof_confirm':
                        masked = state.get('masked') or self.proof_hint or '辅助安全邮箱'
                        prompt = state.get('prompt') or f'请输入完整的辅助安全邮箱 ({masked}) 以发送安全验证码'
                        self.status = 'wait_proof_confirm'
                        self.otp_prompt = prompt
                        self.proof_hint = masked
                        self.message = f'MSR 要求确认辅助安全邮箱: {prompt}'
                        deadline = max(deadline, time.time() + 180)
                        time.sleep(2)

                    elif st_type == 'otp_input':
                        prompt = state.get('prompt') or '请输入安全验证码'
                        self.status = 'wait_otp'
                        self.otp_prompt = prompt
                        self.message = f'MSR 需要输入安全验证码: {prompt}'
                        deadline = max(deadline, time.time() + 180)
                        time.sleep(2)

                    elif st_type == 'proof_selection':
                        proof_hint = state.get('hint') or self.proof_hint or '辅助安全邮箱/手机'
                        self.proof_hint = proof_hint
                        if proof_clicks < 2:
                            self.status = 'running'
                            self.message = f'检测到 MSR 身份验证选项 ({proof_hint})，正在自动点击以发送验证码...'
                            self.log(f'检测到 MSR 身份验证页面 (Almost there: {proof_hint})，正在自动点击进入验证码发送流程...')
                            click_res = browser.ev(self.cdp, '''(() => {
                                const candidates = Array.from(document.querySelectorAll('button, [role="button"], .tile, a, div[tabindex="0"]')).filter(el => {
                                    if (el.offsetParent === null) return false;
                                    const t = (el.innerText || el.getAttribute('aria-label') || '').trim();
                                    const lower = t.toLowerCase();
                                    return lower.includes('send a code to') ||
                                           lower.includes('send code to') ||
                                           (t.includes('向') && (t.includes('发送代码') || t.includes('发送验证码'))) ||
                                           lower.includes('email to') ||
                                           lower.includes('text to') ||
                                           (lower.includes('@') && (lower.includes('code') || lower.includes('代码')));
                                });
                                const target = candidates[0] || document.querySelector('#idDiv_SAOTCS_Proofs .table-row, [data-testid*="proof"], .tile-container .tile, #idDiv_SAOTCS_Proofs button');
                                if (target) {
                                    const el = target.closest('button, [role=button], a, .tile, div[tabindex]') || target;
                                    const rect = el.getBoundingClientRect();
                                    try {
                                        el.focus();
                                        el.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, cancelable: true}));
                                        el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
                                        el.dispatchEvent(new PointerEvent('pointerup', {bubbles: true, cancelable: true}));
                                        el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true}));
                                        el.click();
                                    } catch(e) {
                                        el.click();
                                    }
                                    return {
                                        success: true,
                                        text: el.innerText || el.getAttribute('aria-label'),
                                        x: (rect && rect.width > 0) ? (rect.x + rect.width / 2) : 0,
                                        y: (rect && rect.height > 0) ? (rect.y + rect.height / 2) : 0
                                    };
                                }
                                return { success: false };
                            })()''')
                            if isinstance(click_res, dict) and click_res.get('x') and click_res.get('y'):
                                try:
                                    browser.mouse_click(self.cdp, click_res['x'], click_res['y'])
                                except Exception:
                                    pass
                            proof_clicks += 1
                            time.sleep(2.5)
                        else:
                            self.status = 'wait_interactive'
                            self.message = f'检测到 MSR 身份验证方式选择 ({proof_hint})，请在下方实时交互窗口中点击选择'
                            time.sleep(2)

                    elif st_type == 'arkose_challenge':
                        self.status = 'wait_interactive'
                        self.message = '检测到人机拼图验证码(Arkose)，请在下方实时交互窗口中点击完成验证'
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
                        self.message = f'正在等待 MSR 页面加载 ({title or curr_url[:40]})...'
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
                    self.message = '登录超时，可能存在未通过的风控验证或等待验证输入超时'

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


def start_session(name, username, password, proxy=None, log_cb=None, save_credential_flag=True, ch=None, cdp=None):
    with _sessions_lock:
        if name in _active_sessions:
            try:
                _active_sessions[name].cancel()
            except Exception:
                pass
        sess = LoginSession(name, username, password, proxy=proxy, log_cb=log_cb,
                            save_credential_flag=save_credential_flag, ch=ch, cdp=cdp)
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


def login_with_credential(name, username, password, proxy=None, log_cb=None, save_credential_flag=True, ch=None, cdp=None, timeout=300):
    """现代化 MSR 账号密码全自动登录与多重风控质询接管。支持复用已有 Chromium 实例原地登录。"""
    session = start_session(name, username, password, proxy=proxy, log_cb=log_cb,
                            save_credential_flag=save_credential_flag, ch=ch, cdp=cdp)
    deadline = time.time() + timeout
    prompted_auth_code = None
    prompted_otp = False
    prompted_proof = False

    while time.time() < deadline:
        if session.status == 'success':
            return session.result
        if session.status == 'error':
            raise RuntimeError(session.error_msg or session.message)

        if session.status == 'wait_auth_push' and session.auth_code != prompted_auth_code:
            prompted_auth_code = session.auth_code
            if log_cb:
                log_cb(f'>>> [安全提示] MSR 触发手机 Authenticator 验证，请在手机上选择数字：【 {session.auth_code} 】')

        if session.status == 'wait_proof_confirm' and not prompted_proof:
            prompted_proof = True
            if sys.stdin and sys.stdin.isatty():
                print(f'\n>>> MSR 要求确认辅助安全邮箱 ({session.otp_prompt or session.proof_hint or "辅助邮箱"}):', flush=True)
                try:
                    email_val = input('请输入完整辅助安全邮箱: ').strip()
                    if email_val:
                        session.submit_otp(email_val)
                except Exception:
                    pass
            else:
                if log_cb:
                    log_cb(f'>>> [安全提示] MSR 要求确认辅助安全邮箱: {session.otp_prompt} (请在 Web 控制台输入完整辅助邮箱以发送验证码)')

        if session.status == 'wait_otp' and not prompted_otp:
            prompted_otp = True
            if sys.stdin and sys.stdin.isatty():
                print(f'\n>>> MSR 要求输入安全验证码 ({session.otp_prompt or "邮箱/短信"}):', flush=True)
                try:
                    code = input('请输入验证码: ').strip()
                    if code:
                        session.submit_otp(code)
                except Exception:
                    pass
            else:
                if log_cb:
                    log_cb(f'>>> [安全提示] MSR 要求输入安全验证码: {session.otp_prompt} (请在 Web 控制台输入提交)')

        if session.status == 'wait_interactive':
            if log_cb:
                log_cb('>>> [风控提示] 检测到 MSR 人机拼图/复杂安全质询，请在 Web 控制台进行远程交互操作')

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
