"""阶段执行器。API 侧(签到/阅读/Edge打卡)与浏览器侧(搜索/每日活动/打卡任务/日常活动/视觉打卡)。"""
import json
import random
import secrets
import time
import uuid

from msr import browser, fingerprint
from msr.browser import navigate


def run_api_phase(name, phase, token, pr, profile_path, config, api, proxy=None):
    """API 阶段执行，支持可选独立代理 proxy。"""
    rg = {'country': pr['country'], 'language': pr['language']}
    eh = api.edge_headers(token, rg)
    ah = api.app_headers(token, rg)
    c = rg['country'].lower()

    if phase in ('appcheckin', 'appchekin', 'app', 'checkin'):
        old = api.get_me(name, token, rg, proxy=proxy).get('balance')
        r = api.api(name, 'POST', '/me/activities', token, ah,
                    {'amount': 1, 'attributes': {}, 'id': str(uuid.uuid4()), 'type': 103,
                     'country': c, 'risk_context': {}, 'channel': 'SAAndroid'}, proxy=proxy)
        new = (r.get('response') or {}).get('balance', old)
        gained = (new or 0) - (old or 0)
        return '+%s' % gained

    if phase in ('appread', 'read'):
        from msr import settings
        st = settings.load_settings()
        fast_mode = bool(st.get('schedule', {}).get('fast_mode'))
        target_count = int(st.get('mobile', {}).get('read_article_count', 10))
        w_min = 2 if fast_mode else int(st.get('mobile', {}).get('read_interval_min', 60))
        w_max = 5 if fast_mode else int(st.get('mobile', {}).get('read_interval_max', 120))

        total, done = 0, 0
        for i in range(target_count):
            old = api.get_me(name, token, rg, proxy=proxy).get('balance')
            r = api.api(name, 'POST', '/me/activities', token, ah,
                        {'amount': 1, 'id': secrets.token_hex(64), 'type': 101,
                         'attributes': {'offerid': 'ENUS_readarticle3_30points'}, 'country': c}, proxy=proxy)
            new = (r.get('response') or {}).get('balance', old)
            g = (new or 0) - (old or 0)
            if g <= 0:
                if i == 0:
                    config.log(name, 'App阅读: 今日额度已满或暂无加分')
                else:
                    config.log(name, f'App阅读: 已达今日上限 (完成 {done} 篇, 累计 +{total} 分)')
                break
            total += g
            done = i + 1
            if i < target_count - 1:
                wait_sec = random.uniform(w_min, w_max)
                config.log(name, f'App阅读 [{done}/{target_count}篇]: 本篇+{g}分, 累计+{total}分, 等待 {wait_sec:.0f}s 模拟真实翻阅...')
                time.sleep(wait_sec)
            else:
                config.log(name, f'App阅读 [{done}/{target_count}篇]: 本篇+{g}分, 累计+{total}分 (全部完成)')
        return '%d篇+%d' % (done, total)


    if phase in ('edge30m', 'edge_api', 'edge'):
        promos = api.get_me(name, token, rg, proxy=proxy)
        flight = api.find_promo(promos, 'edge_browsing_streak_flight')
        elig = api.find_promo(promos, 'edge_browsing_streak_eligible')

        # 若处于未激活状态，统一交由前台主浏览器执行页面交互激活，严禁在后台线程私起第二个 Chromium 造成锁冲突
        if flight is None or elig is None or str(elig.get('complete', '')).lower() != 'true':
            config.log(name, '检测到本账号 Edge 浏览打卡尚未激活 (交由主浏览器页面激活后联动补跑)')
            return '未下发Edge任务(跳过)'

        if str(flight.get('complete')).lower() == 'true':
            return '已完成'
        from msr import settings
        st_edge = settings.load_settings()
        fast_mode = bool(st_edge.get('schedule', {}).get('fast_mode', False))
        edge_reports = int(st_edge.get('mobile', {}).get('edge30m_reports', 6))
        interval = 10 if fast_mode else int(st_edge.get('mobile', {}).get('edge30m_interval', 305))
        for i in range(edge_reports):
            aid = None
            for attempt_no in range(3):
                r = api.api(name, 'POST', '/me/activities', token, eh,
                            {'amount': 1, 'attributes': {'offerid': flight.get('offerid')},
                             'request_user_info': True, 'type': int(flight.get('report_offer_type', 29))}, proxy=proxy)
                try:
                    aid = r['response']['activity']['id']
                except (KeyError, TypeError):
                    aid = None
                if aid:
                    break
                config.log(name, 'report%d 第%d次响应缺activity, 30s后重发'
                           % (i + 1, attempt_no + 1))
                time.sleep(5 if fast_mode else 30)
            config.log(name, 'report%d act=%s 已上报' % (i + 1, (aid or '?')[:8]))
            if i < edge_reports - 1:
                read_wait = min(5 if fast_mode else 180, interval // 2)
                time.sleep(read_wait)
                st = api.find_promo(api.get_me(name, token, rg, proxy=proxy),
                                    'edge_browsing_streak_flight') or {}
                config.log(name, 'report%d 回读: progress=%s/%s complete=%s'
                           % (i + 1, st.get('progress'), st.get('max'), st.get('complete')))
                if str(st.get('complete')).lower() == 'true':
                    return '完成(%d次)' % (i + 1)
                time.sleep(max(0, interval - read_wait))
        time.sleep(5 if fast_mode else 600)
        resp = api.get_me(name, token, rg, proxy=proxy)
        st = api.find_promo(resp, 'edge_browsing_streak_flight') or {}
        bal = resp.get('balance')
        config.log(name, '最终回读: progress=%s/%s complete=%s 余额=%s'
                   % (st.get('progress'), st.get('max'), st.get('complete'), bal))
        if str(st.get('complete')).lower() == 'true':
            return 'Edge完成(%d次), 余额=%s' % (edge_reports, bal)
        return ('Edge上报%d次, progress=%s/%s complete=%s 余额=%s(计分可能延迟)'
                % (edge_reports, st.get('progress'), st.get('max'), st.get('complete'), bal))

    return '未知API阶段'


def browser_cfg(config):
    """浏览器可配置项(从 settings.json 统一读取)。"""
    import os
    import shutil
    from msr import settings as S
    st = S.load_settings()
    b_cfg = st.get('browser', {})
    s_cfg = st.get('search', {})
    c_cfg = st.get('cards', {})
    default_bin = 'chromium'
    for cand in ('google-chrome', 'chromium', 'chromium-browser', 'edge', 'msedge'):
        if shutil.which(cand):
            default_bin = cand
            break
    return {
        'binary': b_cfg.get('binary') or os.environ.get('MSR_CHROMIUM', default_bin),
        'version': b_cfg.get('version') or os.environ.get('MSR_EDGE_VERSION', '141'),
        'search_budget_min': int(s_cfg.get('budget_minutes', 120)),
        'cards_budget_min': int(c_cfg.get('budget_minutes', 35)),
        'headless': b_cfg.get('headless', True),
        'disable_webrtc_leak': b_cfg.get('disable_webrtc_leak', True)
    }


def execute_single_browser_phase(phase, name, cdp, pr, ch, bc, config, logger, token=None, proxy=None):
    """在现有 CDP 会话中分发具体浏览器任务。"""
    from msr import tasks_browser as tb
    p = phase.lower()
    if p in ('dailyset', 'quick'):
        return tb.phase_dailyset(name, cdp, pr, ch=ch,
                                 budget_seconds=bc['cards_budget_min'] * 60,
                                 log=logger)
    if p in ('quests', 'quest', 'punchcard'):
        return tb.phase_quests(name, cdp, pr, ch=ch,
                               budget_seconds=bc['cards_budget_min'] * 60,
                               log=logger)
    if p in ('explore', 'exploreonbing'):
        return tb.phase_explore(name, cdp, pr, ch=ch, log=logger)
    if p in ('moreactivities', 'earn', 'more'):
        return tb.phase_moreactivities(name, cdp, pr, ch=ch,
                                      budget_seconds=bc['cards_budget_min'] * 60,
                                      log=logger)
    if p in ('search', 'pcsearch'):
        return tb.phase_search(name, cdp, pr, ch=ch,
                               budget_seconds=bc['search_budget_min'] * 60,
                               log=logger, token=token, proxy=proxy)
    if p in ('visual', 'visualsearch'):
        from msr import tasks_visual
        return tasks_visual.run(name, cdp, pr, log=logger, ch=ch,
                                used_path=config.pfile(name, 'visual_used.json'))
    if p in ('claim', 'pointclaim', 'claimpoints'):
        return tb.phase_claim(name, cdp, pr, ch=ch, log=logger)
    if p in ('perks', 'perk', 'activate', 'promotions', 'promotion'):
        return tb.phase_perks(name, cdp, pr, ch=ch, log=logger)
    return '未知浏览器阶段 %s' % phase


class BrowserSession:
    """账号专属独立 Chromium 会话生命周期管理。

    支持阶段 3-9 连续执行及阶段 10 容错回补共用同一浏览器会话，
    避免频繁起停浏览器造成的系统开销与延迟。
    """

    def __init__(self, name, token, pr, profile_path, config, proxy=None, cdp_port=None):
        self.name = name
        self.token = token
        self.pr = pr
        self.profile_path = profile_path
        self.config = config
        self.proxy = proxy
        self.cdp_port = cdp_port
        self.logger = lambda msg: config.log(name, msg)
        self.ch = None
        self.cdp = None
        self.tab = None
        self.bc = None
        self.newly_activated = []
        self.is_authenticated = False
        self.degraded_mode = False

    def start(self):
        from msr import browser, fingerprint
        self.bc = browser_cfg(self.config)
        chrome_dir = self.profile_path + '/chrome'

        fp = fingerprint.load_or_create(self.name, country=self.pr.get('country'))
        self.logger('应用账号专属硬件指纹: %s (%s, %s, %s核%dG, %dx%d, %s)' % (
            self.name, fp.get('os_name'), fp.get('webgl_renderer', '').split('Direct3D')[0].strip(),
            fp.get('hardware_concurrency'), fp.get('device_memory'),
            fp.get('screen_width'), fp.get('screen_height'), fp.get('timezone')
        ))

        self.ch = browser.Chromium(binary=self.bc['binary'], user_data_dir=chrome_dir,
                                   version=fp.get('edge_version', self.bc['version']), log=self.logger,
                                   proxy=self.proxy, cdp_port=self.cdp_port, fingerprint=fp,
                                   headless=self.bc.get('headless', True),
                                   disable_webrtc_leak=self.bc.get('disable_webrtc_leak', True))
        tabs = self.ch.start()
        self.tab = tabs[0] if tabs else self.ch.new_tab('about:blank')
        self.cdp = self.ch.attach(self.tab)
        self.ch.cleanup_extra_tabs(keep_tab_id=self.tab.get('id'))

        cookies = self.config.load_json(self.config.pfile(self.name, 'cookie.json'))
        if cookies:
            browser.inject_cookies(self.cdp, cookies, log=self.logger)

    def check_login_state(self, timeout=25, max_retries=1):
        """正向校验登录态：访问 rewards.bing.com 并等待水合提取可用积分。
        
        严格解耦网络/代理抖动与真正会话失效：
        - 网络报错 / 代理超时：返回 (False, None, 'NETWORK_ERROR')
        - 成功解析到可用积分 (>=0)：返回 (True, balance, 'OK')
        - 明确重定向到 login.live.com / welcome 或出现登录 CTA：返回 (False, None, 'SESSION_EXPIRED')
        """
        import time
        from msr import browser
        for attempt in range(max_retries + 1):
            try:
                self.logger(f'正在执行前置登录态正向测活 (第 {attempt + 1} 次)...')
                browser.navigate(self.cdp, 'https://rewards.bing.com/')
            except Exception as ex:
                self.logger(f'导航至 rewards.bing.com 异常: {ex}')
                if attempt < max_retries:
                    time.sleep(3)
                    continue
                return False, None, 'NETWORK_ERROR'

            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    curr_url = browser.ev(self.cdp, 'location.href', timeout=5) or ''
                    # 1. 检查 Chromium 网络/代理错误页 (如 ERR_PROXY_CONNECTION_FAILED)
                    if curr_url.startswith('chrome-error://') or 'ERR_' in curr_url:
                        self.logger(f'检测到 Chromium 网络/代理错误页面: {curr_url}')
                        if attempt < max_retries:
                            time.sleep(3)
                            break
                        return False, None, 'NETWORK_ERROR'

                    # 2. 检查是否重定向至微软登录网关
                    if any(k in curr_url for k in ('login.live.com', 'rewards.bing.com/welcome', 'fd/auth/signin')):
                        # 进一步确认页面是否加载了登录表单元素
                        is_login_page = browser.ev(self.cdp, '''(() => {
                            return Boolean(document.querySelector('#usernameEntry, #i0116, input[name=loginfmt], #passwordEntry, input[name=passwd], #idSIButton9') ||
                                           (document.body && (document.body.innerText.includes('Sign in') || document.body.innerText.includes('登录你的 Microsoft 帐户'))));
                        })()''', timeout=5)
                        if is_login_page:
                            self.logger(f'检测到页面已明确重定向至登录网关: {curr_url}')
                            return False, None, 'SESSION_EXPIRED'

                    # 3. 正向提取用户可用积分
                    bal = browser.ev(self.cdp, '''(() => {
                        const t = document.body ? document.body.innerText : '';
                        const m = t.match(/(?:可用积分|Available points|balance)[^\\d]*([\\d,]+)/i) || 
                                  t.match(/([\\d,]+)\\s*(?:可用积分|points)/i);
                        if (m) return parseInt(m[1].replace(/,/g, ''), 10);
                        const el = document.querySelector('#balance-card, [class*="balance"], [class*="pointsBreakdown"], #id_rc, [data-testid="user-points"]');
                        if (el) {
                            const m2 = (el.innerText || '').match(/([\\d,]+)/);
                            if (m2) return parseInt(m2[1].replace(/,/g, ''), 10);
                        }
                        return null;
                    })()''', timeout=5)

                    if bal is not None:
                        self.is_authenticated = True
                        self.logger(f'前置登录态测活成功: 账户当前积分={bal}')
                        return True, bal, 'OK'

                    # 4. 检查是否出现明确的游客提示 (未水合且显示登录按钮)
                    is_guest = browser.ev(self.cdp, '''(() => {
                        return Boolean(document.querySelector('#id_s, a[href*="fd/auth/signin"], a[href*="windows_live_id"]') &&
                                       !document.querySelector('#id_rh, #id_rc, #id_n, #b_idRh, .mee-icon-SkypeCircleCheck'));
                    })()''', timeout=5)
                    if is_guest and time.time() - (deadline - timeout) > 8:
                        self.logger('页面渲染完成但未包含用户身份信息且仅展示登录入口，确诊会话失效')
                        return False, None, 'SESSION_EXPIRED'

                except Exception:
                    pass
                time.sleep(1.5)

            if attempt < max_retries:
                self.logger('首轮测活未能提取积分，正在刷新页面重试...')
                time.sleep(2)
                continue

        return False, None, 'SESSION_EXPIRED'

    def in_situ_relogin(self, username, password, timeout=180):
        """在当前浏览器实例中原地执行全自动登录自愈 (不关进程、不重新拉起、零锁竞争)。"""
        from msr import auth
        self.logger(f'正在当前浏览器中原地执行自愈登录 (账号: {username})...')
        try:
            try:
                self.cdp.call('Network.clearBrowserCookies')
                self.cdp.call('Network.clearBrowserCache')
            except Exception:
                pass

            res = auth.login_with_credential(
                self.name, username, password,
                proxy=self.proxy,
                log_cb=self.logger,
                ch=self.ch,
                cdp=self.cdp,
                timeout=timeout
            )
            if res.get('status') == 'ok':
                self.is_authenticated = True
                self.logger('原地自愈登录成功！全套凭据已刷新就绪。')
                return True, res
            else:
                self.logger(f'原地自愈登录未成功: {res.get("message") or res.get("error")}')
                return False, res
        except Exception as ex:
            self.logger(f'原地自愈登录异常: {ex}')
            return False, {'status': 'error', 'error': str(ex)}

    def safe_writeback_cookies(self):
        """在会话结束时，安全提取最新多域 Cookie 并写回 cookie.json。"""
        if getattr(self, 'degraded_mode', False):
            self.logger('当前为降级模式，跳过 Cookie 回写以保护已有凭据备份')
            return False
        if not getattr(self, 'is_authenticated', False):
            self.logger('当前会话未处于已登录状态，跳过 Cookie 回写')
            return False
        if not self.cdp:
            return False

        import os
        from msr import cookies as cookie_mod
        try:
            urls = [
                'https://login.live.com',
                'https://www.bing.com',
                'https://account.microsoft.com',
                'https://rewards.bing.com'
            ]
            res = self.cdp.call('Network.getCookies', {'urls': urls})
            cks = res.get('cookies', [])
            diag = cookie_mod.validate_cookies(cks)
            if diag.get('has_auth_u'):
                cookie_mod.save_cookies_safely(self.name, cks, config=self.config, log=self.logger)
                return True
            else:
                self.logger('提取的 Cookie 缺少核心 _U 凭据，取消回写')
                return False
        except Exception as ex:
            self.logger(f'Cookie 回写异常(容忍): {ex}')
            return False

    def run_phases(self, phases, is_retry=False):
        results = {}
        if not is_retry and self.newly_activated:
            results['_newly_activated'] = self.newly_activated

        for idx, phase in enumerate(phases):
            prefix = ' [回补]' if is_retry else ''
            self.logger('>>> 开始浏览器阶段%s: %s' % (prefix, phase))
            try:
                r = execute_single_browser_phase(phase, self.name, self.cdp, self.pr, self.ch, self.bc, self.config, self.logger,
                                                 token=self.token, proxy=self.proxy)
                results[phase] = r
                self.logger('<<< 浏览器阶段%s %s 完成: %s' % (prefix, phase, r))
            except Exception as ex:
                results[phase] = '失败(%s)' % str(ex)[:60]
                self.logger('!!! 浏览器阶段%s %s 异常: %s' % (prefix, phase, str(ex)[:120]))
            finally:
                cur_tid = self.cdp.path.split('/')[-1].split('?')[0] if (hasattr(self.cdp, 'path') and self.cdp.path) else (self.tab.get('id') if self.tab else None)
                if cur_tid:
                    self.ch.cleanup_extra_tabs(keep_tab_id=cur_tid)

                if phase != phases[-1]:
                    from msr import settings as S
                    st_cfg = S.load_settings()
                    fast_mode = bool(st_cfg.get('schedule', {}).get('fast_mode', False))
                    s_min = 2 if fast_mode else int(st_cfg.get('schedule', {}).get('inter_stage_wait_min', 3)) * 60
                    s_max = 5 if fast_mode else int(st_cfg.get('schedule', {}).get('inter_stage_wait_max', 5)) * 60
                    wait_stage = random.uniform(s_min, s_max)
                    self.logger('阶段过渡: 等待 %.1f 秒后进入下一阶段...' % wait_stage)
                    time.sleep(wait_stage)

        return results

    def close(self):
        if self.ch:
            try:
                self.safe_writeback_cookies()
            except Exception as ex_wb:
                self.logger(f'Cookie 回写异常(容忍): {ex_wb}')
            try:
                cur_tid = self.cdp.path.split('/')[-1].split('?')[0] if (hasattr(self.cdp, 'path') and self.cdp.path) else (self.tab.get('id') if self.tab else None)
                if cur_tid:
                    self.ch.close_tab(cur_tid)
            except Exception:
                pass
            try:
                self.ch.stop()
            except Exception:
                pass
            self.ch = None
            self.logger('已完全关闭浏览器并释放当前账号 Profile 与系统内存')


def run_browser_session(name, phases, token, pr, profile_path, config, proxy=None, cdp_port=None):
    """单浏览器会话: 起独立 profile headless chromium -> 注入账号专属指纹与 cookie -> 连续执行所有浏览器阶段 -> 彻底终结。

    返回 dict: {phase: result}。
    """
    sess = BrowserSession(name, token, pr, profile_path, config, proxy=proxy, cdp_port=cdp_port)
    try:
        sess.start()
        return sess.run_phases(phases)
    finally:
        sess.close()


def run_browser_phase(name, phase, token, pr, profile_path, config, proxy=None, cdp_port=None):
    """单阶段调用封装(兼容历史与独立调试)。"""
    res = run_browser_session(name, [phase], token, pr, profile_path, config, proxy=proxy, cdp_port=cdp_port)
    return res.get(phase, '未执行')
