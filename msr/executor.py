"""阶段执行器。API 侧(签到/阅读/Edge打卡)与浏览器侧(搜索/每日活动/打卡任务/日常活动/视觉打卡)。"""
import json
import random
import secrets
import time
import uuid

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
        total, done = 0, 0
        for i in range(10):
            old = api.get_me(name, token, rg, proxy=proxy).get('balance')
            r = api.api(name, 'POST', '/me/activities', token, ah,
                        {'amount': 1, 'id': secrets.token_hex(64), 'type': 101,
                         'attributes': {'offerid': 'ENUS_readarticle3_30points'}, 'country': c}, proxy=proxy)
            new = (r.get('response') or {}).get('balance', old)
            g = (new or 0) - (old or 0)
            total += g
            done = i + 1
            if g <= 0:
                break
            time.sleep(random.uniform(60, 120))
        return '%d篇+%d' % (done, total)

    if phase in ('edge30m', 'edge_api', 'edge'):
        promos = api.get_me(name, token, rg, proxy=proxy)
        flight = api.find_promo(promos, 'edge_browsing_streak_flight')
        if flight is None:
            elig = api.find_promo(promos, 'edge_browsing_streak_eligible')
            if not elig:
                return '无资格'
            api.api(name, 'POST', '/me/activities', token, eh,
                    {'amount': 1, 'attributes': {'offerid': elig.get('offerid')},
                     'request_user_info': True, 'type': elig.get('report_offer_type')}, proxy=proxy)
            time.sleep(15)
            flight = api.find_promo(api.get_me(name, token, rg, proxy=proxy), 'edge_browsing_streak_flight')
            if flight is None:
                return '资格后无flight'
        if str(flight.get('complete')).lower() == 'true':
            return '已完成'
        interval = int(flight.get('report_per_minutes') or 5) * 60 + 5
        for i in range(6):
            aid = None
            for attempt_no in range(3):
                r = api.api(name, 'POST', '/me/activities', token, eh,
                            {'amount': 1, 'attributes': {'offerid': flight.get('offerid')},
                             'request_user_info': True, 'type': flight.get('report_offer_type')}, proxy=proxy)
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
                st = api.find_promo(api.get_me(name, token, rg, proxy=proxy),
                                    'edge_browsing_streak_flight') or {}
                config.log(name, 'report%d 回读: progress=%s/%s complete=%s'
                           % (i + 1, st.get('progress'), st.get('max'), st.get('complete')))
                if str(st.get('complete')).lower() == 'true':
                    return '完成(%d次)' % (i + 1)
                time.sleep(max(0, interval - 180))
        time.sleep(600)
        resp = api.get_me(name, token, rg, proxy=proxy)
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
    import os
    import shutil
    from msr import config as C
    cfg = C.load_json(C.HOME + '/browser.json', {}) or {}
    default_bin = 'chromium'
    for cand in ('google-chrome', 'chromium', 'chromium-browser', 'edge', 'msedge'):
        if shutil.which(cand):
            default_bin = cand
            break
    return {
        'binary': cfg.get('binary') or os.environ.get('MSR_CHROMIUM', default_bin),
        'version': cfg.get('version') or os.environ.get('MSR_EDGE_VERSION', '141'),
        'search_budget_min': int(cfg.get('search_budget_minutes', 60)),
        'cards_budget_min': int(cfg.get('cards_budget_minutes', 25)),
        'headless': cfg.get('headless', True),
    }


def execute_single_browser_phase(phase, name, cdp, pr, ch, bc, config, logger):
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
    if p in ('moreactivities', 'earn', 'more'):
        return tb.phase_moreactivities(name, cdp, pr, ch=ch,
                                      budget_seconds=bc['cards_budget_min'] * 60,
                                      log=logger)
    if p in ('search', 'pcsearch'):
        return tb.phase_search(name, cdp, pr, ch=ch,
                               budget_seconds=bc['search_budget_min'] * 60,
                               log=logger)
    if p in ('visual', 'visualsearch'):
        from msr import tasks_visual
        return tasks_visual.run(name, cdp, pr, log=logger,
                                used_path=config.pfile(name, 'visual_used.json'))
    return '未知浏览器阶段 %s' % phase


def run_browser_session(name, phases, token, pr, profile_path, config, proxy=None, cdp_port=None):
    """单浏览器会话: 起独立 profile headless chromium -> 注入账号专属指纹与 cookie -> 连续执行所有浏览器阶段 -> 彻底终结。

    返回 dict: {phase: result}。
    """
    from msr import browser, fingerprint
    bc = browser_cfg(config)
    chrome_dir = profile_path + '/chrome'
    logger = lambda msg: config.log(name, msg)

    # 1. 账号级反检测指纹加载/生成 (不同账号独立显卡/核心/内存/分辨率/时区)
    fp = fingerprint.load_or_create(name, country=pr.get('country'))
    logger('应用账号专属硬件指纹: %s (%s, %s, %s核%dG, %dx%d, %s)' % (
        name, fp.get('os_name'), fp.get('webgl_renderer', '').split('Direct3D')[0].strip(),
        fp.get('hardware_concurrency'), fp.get('device_memory'),
        fp.get('screen_width'), fp.get('screen_height'), fp.get('timezone')
    ))

    ch = browser.Chromium(binary=bc['binary'], user_data_dir=chrome_dir,
                          version=fp.get('edge_version', bc['version']), log=logger,
                          proxy=proxy, cdp_port=cdp_port, fingerprint=fp)
    tabs = ch.start()
    # 优先复用 start 创建好的首个标签，避免多余空白标签占用 ARM 内存
    tab = tabs[0] if tabs else ch.new_tab('about:blank')
    results = {}
    try:
        cdp = ch.attach(tab)
        # 清除其余多余标签，保证仅当前 1 个工作标签在运行
        ch.cleanup_extra_tabs(keep_tab_id=tab.get('id'))

        cookies = config.load_json(config.pfile(name, 'cookie.json'))
        if cookies:
            browser.inject_cookies(cdp, cookies, log=logger)

        for phase in phases:
            logger('>>> 开始浏览器阶段: %s' % phase)
            try:
                r = execute_single_browser_phase(phase, name, cdp, pr, ch, bc, config, logger)
                results[phase] = r
                logger('<<< 浏览器阶段 %s 完成: %s' % (phase, r))
            except Exception as ex:
                results[phase] = '失败(%s)' % str(ex)[:60]
                logger('!!! 浏览器阶段 %s 异常: %s' % (phase, str(ex)[:120]))
            finally:
                # 每个阶段完成后，立即关闭所有可能产生的子标签，及时释放 ARM 内存并保护当前活跃标签
                cur_tid = cdp.path.split('/')[-1].split('?')[0] if (hasattr(cdp, 'path') and cdp.path) else tab.get('id')
                ch.cleanup_extra_tabs(keep_tab_id=cur_tid)

        return results
    finally:
        try:
            cur_tid = cdp.path.split('/')[-1].split('?')[0] if (hasattr(cdp, 'path') and cdp.path) else tab.get('id')
            ch.close_tab(cur_tid)
        except Exception:
            pass
        # 彻底终结 Chromium 整个进程树并释放 profile 锁与系统内存
        ch.stop()
        logger('已完全关闭浏览器并释放当前账号 Profile 与系统内存')


def run_browser_phase(name, phase, token, pr, profile_path, config, proxy=None, cdp_port=None):
    """单阶段调用封装(兼容历史与独立调试)。"""
    res = run_browser_session(name, [phase], token, pr, profile_path, config, proxy=proxy, cdp_port=cdp_port)
    return res.get(phase, '未执行')
