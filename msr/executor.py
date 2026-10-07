"""阶段执行器。API 侧先落地(签到/阅读/Edge打卡), 浏览器侧后续迁移。"""
import json
import random
import secrets
import time
import uuid


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


def run_browser_phase(name, phase, token, pr, profile_path, config):
    """浏览器阶段: 搜索/每日活动卡/视觉 —— Task 4-5 迁移, 先占位。"""
    raise NotImplementedError('浏览器阶段 %s 待 Task 4-5 迁移' % phase)
