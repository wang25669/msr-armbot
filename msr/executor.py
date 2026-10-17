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
        # 2026-10-09 定稿节奏(与现网 edge_app_streak.do_edge 对齐):
        #   每次上报只 POST, 不依赖回读; 上报后等 180s 回读一次仅记日志;
        #   两次上报间补足微软要求的间隔; 最后一次上报后等 600s 拿最终结果。
        # 2026-10-11 修复: 微软偶发 200 但响应体缺 activity, 防御式取 id,
        # 缺失则隔 30s 重发(最多 3 次), 不让单次异常炸掉整个任务。
        for i in range(6):
            aid = None
            for attempt_no in range(3):
                r = api.api(name, 'POST', '/me/activities', token, eh,
                            {'amount': 1, 'attributes': {'offerid': flight.get('offerid')},
                             'request_user_info': True, 'type': flight.get('report_offer_type')})
                try:
                    aid = r['response']['activity']['id']
                except (KeyError, TypeError):
                    aid = None
                if aid:
                    break
                config.log(name, 'report%d 第%d次响应缺activity, 30s后重发'
                           % (i + 1, attempt_no + 1))
                time.sleep(30)
            config.log(name, 'report%d act=%s 已上报' % (i + 1, (aid or '?')[:8]))
            if i < 5:
                time.sleep(180)
                st = api.find_promo(api.get_me(name, token, rg),
                                    'edge_browsing_streak_flight') or {}
                config.log(name, 'report%d 回读: progress=%s/%s complete=%s'
                           % (i + 1, st.get('progress'), st.get('max'), st.get('complete')))
                if str(st.get('complete')).lower() == 'true':
                    return '完成(%d次)' % (i + 1)
                time.sleep(max(0, interval - 180))
        time.sleep(600)
        resp = api.get_me(name, token, rg)
        st = api.find_promo(resp, 'edge_browsing_streak_flight') or {}
        bal = resp.get('balance')
        config.log(name, '最终回读: progress=%s/%s complete=%s 余额=%s'
                   % (st.get('progress'), st.get('max'), st.get('complete'), bal))
        if str(st.get('complete')).lower() == 'true':
            return 'Edge完成(6次), 余额=%s' % bal
        return ('Edge上报6次, progress=%s/%s complete=%s 余额=%s(计分可能延迟)'
                % (st.get('progress'), st.get('max'), st.get('complete'), bal))

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
        if phase == 'visual':
            from msr import tasks_visual
            return tasks_visual.run(name, cdp, pr, log=config.log,
                                    used_path=config.pfile(name, 'visual_used.json'))
        return '未知浏览器阶段 %s' % phase
    finally:
        try:
            ch.close_tab(tab.get('id'))
        except Exception:
            pass
        ch.stop()
