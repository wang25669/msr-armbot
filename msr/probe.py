"""区域与任务集探测 —— 不硬编码任何任务集, 以 /dapi/me 实测为准。"""
import json


def probe_account(name, token, proxy=None):
    """GET /dapi/me -> {country, language, levelInfo, promotions, task_names}。

    task_names 是从 promotions 提取的"该账号实际可做任务"的规范名列表,
    planner 据此生成执行计划。region 是账号属性(与出口 IP 无关),
    在任何网络环境下探测结果一致(2026-10-07 与用户确认)。支持代理。
    """
    from msr import api

    # country 真实位置: /me -> profile.attributes.country (账号属性, 与 IP 无关)
    # 第一次用中性 region 头, 读到真实值后再用真实值请求一次(promotions 按区域下发)
    neutral = {'country': 'US', 'language': 'en'}
    resp = api.get_me(name, token, neutral, proxy=proxy)
    attrs = ((resp.get('profile') or {}).get('attributes') or {})
    country = (attrs.get('country') or 'US').upper()
    language = attrs.get('language') or 'en'
    rg = {'country': country, 'language': language}

    resp = api.get_me(name, token, rg, proxy=proxy)


    # 不按 complete 过滤: planner 需要的是"账号拥有哪些任务类型",
    # 今日是否已完成由执行器运行时判断(每天会重置)
    names = [p.get('name', '') for p in resp.get('promotions', [])]

    return {
        'country': country,
        'language': language,
        'levelInfo': resp.get('levelInfo') or {},
        'balance': (resp.get('balance') or ''),
        'promotions': resp.get('promotions') or [],
        'task_names': names,
        'probed_at': '',
    }
