"""阶段执行器。API 侧先落地(签到/阅读/Edge打卡), 浏览器侧后续迁移。"""
import json
import random
import secrets
import time
import uuid

from msr.browser import navigate


def run_api_phase(name, phase, token, pr, profile_path, config, api):
    rg = {'country': pr['country'], 'language': pr['language']}
    eh = api.edge_headers(token, rg)
    ah = api.app_headers(token, rg)
    c = rg['country'].lower()

    if phase == 'app':
        old = api.get_me(name, token, rg).get('balance')
        r = api.api(name, 'POST', '/me/activities', token, ah,
                    {'amount': 1, 'attributes': {}, 'id': str(uuid.uuid4()), 'type': 103,
                     'country': c, 'risk_context': {}, 'channel': 'SAAndroid'})
        new = (r.get('response') or {}).get('balance', old)
        gained = (new or 0) - (old or 0)
        return '+%s' % gained

    if phase == 'read':
        total, done = 0, 0
        for i in range(10):
            old = api.get_me(name, token, rg).get('balance')
            r = api.api(name, 'POST', '/me/activities', token, ah,
                        {'amount': 1, 'id': secrets.token_hex(64), 'type': 101,
                         'attributes': {'offerid': 'ENUS_readarticle3_30points'}, 'country': c})
            new = (r.get('response') or {}).get('balance', old)
            g = (new or 0) - (old or 0)
            total += g
            done = i + 1
            if g <= 0:
                break
            time.sleep(random.uniform(60, 120))
        return '%d篇+%d' % (done, total)

    if phase == 'edge_api':
        promos = api.get_me(name, token, rg)
        flight = api.find_promo(promos, 'edge_browsing_streak_flight')
        if flight is None:
            elig = api.find_promo(promos, 'edge_browsing_streak_eligible')
            if not elig:
                return '无资格'
            api.api(name, 'POST', '/me/activities', token, eh,
                    {'amount': 1, 'attributes': {'offerid': elig.get('offerid')},
                     'request_user_info': True, 'type': elig.get('report_offer_type')})
            time.sleep(15)
            flight = api.find_promo(api.get_me(name, token, rg), 'edge_browsing_streak_flight')
            if flight is None:
                return '资格后无flight'
        if str(flight.get('complete')).lower() == 'true':
            return '已完成'
        interval = int(flight.get('report_per_minutes') or 5) * 60 + 5
        for i in range(6):
            api.api(name, 'POST', '/me/activities', token, eh,
                    {'amount': 1, 'attributes': {'offerid': flight.get('offerid')},
                     'request_user_info': True, 'type': flight.get('report_offer_type')})
            time.sleep(20)
            st = api.find_promo(api.get_me(name, token, rg), 'edge_browsing_streak_flight') or {}
            config.log(name, 'edge上报%d progress=%s/%s' % (i + 1, st.get('progress'), st.get('max')))
            if str(st.get('complete')).lower() == 'true':
                return '完成(%d次)' % (i + 1)
            if i < 5:
                time.sleep(interval)
        return '上报6次未complete'

    return '未知API阶段'


def browser_cfg(config):
    """浏览器可配置项(全部有默认值, 环境变量/配置文件可覆盖)。"""
    from msr import config as C
    cfg = C.load_json(C.HOME + '/browser.json', {}) or {}
    import os
    return {
        'binary': cfg.get('binary') or os.environ.get('MSR_CHROMIUM', 'chromium'),
        'version': cfg.get('version') or os.environ.get('MSR_EDGE_VERSION', '141'),
        'search_budget_min': int(cfg.get('search_budget_minutes', 60)),
        'cards_budget_min': int(cfg.get('cards_budget_minutes', 25)),
        'headless': cfg.get('headless', True),
    }


def run_browser_phase(name, phase, token, pr, profile_path, config):
    """浏览器阶段: 起 headless chromium -> 注入 cookie -> 执行 -> 停。"""
    from msr import browser
    from msr import tasks_browser as tb

    bc = browser_cfg(config)
    chrome_dir = profile_path + '/chrome'
    ch = browser.Chromium(binary=bc['binary'], user_data_dir=chrome_dir,
                          version=bc['version'], log=config.log)
    tabs = ch.start()
    tab = ch.new_tab('about:blank')
    try:
        cdp = ch.attach(tab)
        # cookie: profile 里 cookie.json (B1 登录方案)
        cookies = config.load_json(config.pfile(name, 'cookie.json'))
        if cookies:
            browser.inject_cookies(cdp, cookies, log=config.log)

        if phase == 'search':
            return tb.phase_search(name, cdp, pr,
                                   budget_seconds=bc['search_budget_min'] * 60,
                                   log=config.log)
        if phase == 'quick':
            navigate(cdp, 'https://rewards.bing.com/')
            return tb.phase_quick(name, cdp, pr,
                                  budget_seconds=bc['cards_budget_min'] * 60,
                                  log=config.log)
        if phase == 'earn':
            navigate(cdp, 'https://rewards.bing.com/')
            return tb.phase_earn(name, cdp, pr,
                                 budget_seconds=bc['cards_budget_min'] * 60,
                                 log=config.log)
        return '未知浏览器阶段 %s' % phase
    finally:
        try:
            ch.close_tab(tab.get('id'))
        except Exception:
            pass
        ch.stop()
