"""执行计划: 探测结果 -> 当天要跑的阶段列表。

阶段名称规范对齐:
  appcheckin:     必应移动 App 签到 (API type103)
  appread:        资讯文章阅读 10 篇 (API type101)
  edge30m:        Edge 浏览打卡 30 分钟 (API type714/29 心跳)
  dailyset:       每日活动 Daily Set 3 张卡 (浏览器 #dailyset)
  quests:         任务打卡 Quests / Punchcards (浏览器 #quests)
  moreactivities: 日常任务 (浏览器 #moreactivities)
  search:         必应 PC 搜索 (浏览器补满上限)
  visual:         视觉搜索连续打卡 (浏览器壁纸搜图)
  claim:          检查并领取待领取积分 (浏览器 dashboard 结算领取)
"""
from msr import api

TASK_TABLE = {
    'pcSearch':                      {'phase': 'search',         'needs': 'browser'},
    'dailySetPromotions':            {'phase': 'dailyset',       'needs': 'browser'},
    'quests':                        {'phase': 'quests',         'needs': 'browser'},
    'morePromotions':                {'phase': 'moreactivities', 'needs': 'browser'},
    'visualSearch':                  {'phase': 'visual',         'needs': 'browser', 'optional': True},
    'pointClaim':                    {'phase': 'claim',          'needs': 'browser', 'optional': True},
    'edge_browsing_streak_flight':   {'phase': 'edge30m',        'needs': 'api'},
    'edge_browsing_streak_eligible': {'phase': 'edge30m',        'needs': 'api'},
    'app_checkin':                   {'phase': 'appcheckin',     'needs': 'api'},
    'ENUS_readarticle3_30points':    {'phase': 'appread',        'needs': 'api'},
}

# 别名映射(兼容输入拼写与历史名称)
ALIAS_MAP = {
    'app': 'appcheckin',
    'appchekin': 'appcheckin',
    'appcheckin': 'appcheckin',
    'checkin': 'appcheckin',
    'read': 'appread',
    'appread': 'appread',
    'edge': 'edge30m',
    'edge_api': 'edge30m',
    'edge30m': 'edge30m',
    'quick': 'dailyset',
    'dailyset': 'dailyset',
    'earn': 'moreactivities',
    'moreactivities': 'moreactivities',
    'more': 'moreactivities',
    'quest': 'quests',
    'quests': 'quests',
    'punchcard': 'quests',
    'search': 'search',
    'visual': 'visual',
    'claim': 'claim',
    'pointclaim': 'claim',
    'claimpoints': 'claim'
}


def canonical_phase(ph):
    """将阶段名或别名转为标准名称。"""
    p = ph.strip().lower()
    return ALIAS_MAP.get(p, p)


def classify(promo_name):
    """promotion name -> 规范任务名(无匹配返 None)。"""
    if 'pcSearch' in promo_name or ('search' in promo_name.lower() and 'WW_' in promo_name):
        return 'pcSearch'
    if 'DailySet' in promo_name:
        return 'dailySetPromotions'
    if 'punchcard' in promo_name.lower() or 'quest' in promo_name.lower():
        return 'quests'
    if 'morePromotions' in promo_name or promo_name.startswith('WW_Rewards'):
        return 'morePromotions'
    if 'visual' in promo_name.lower():
        return 'visualSearch'
    if 'pointclaim' in promo_name.lower() or 'claim' in promo_name.lower():
        return 'pointClaim'
    if 'edge_browsing_streak' in promo_name:
        return promo_name
    if 'readarticle' in promo_name:
        return 'ENUS_readarticle3_30points'
    return None


def plan(probe_result, force_phases=None):
    """probe 结果 -> 有序阶段列表 [{phase, needs}]。

    force_phases: 用户 --only 指定, 直接采用(调试用)。
    """
    if force_phases:
        order = [canonical_phase(x) for x in force_phases.split(',')]
        out = []
        for ph in order:
            needs = next((t['needs'] for t in TASK_TABLE.values() if t['phase'] == ph), 'api')
            out.append({'phase': ph, 'needs': needs})
        return out

    # API 侧: 按 promotions 实际存在判断
    have = set()
    for n in probe_result.get('task_names', []):
        c = classify(n)
        if c:
            have.add(c)

    # 浏览器侧阶段: 默认按顺序尝试, 由 executor 在浏览器里按当天实际卡片执行
    BROWSER_ALWAYS = ['dailyset', 'quests', 'moreactivities', 'search', 'visual', 'claim']

    want_phases = [
        {'phase': 'appcheckin', 'needs': 'api'},          # type103 签到总是有效
        {'phase': 'appread', 'needs': 'api'},             # 移动端10篇阅读(+30分)默认纳入
        {'phase': 'edge30m', 'needs': 'api'},             # Edge 30分钟打卡心跳 (内置自动资格激活与飞跃检测，无资格/已完成则秒级跳过)
    ]
    for ph in BROWSER_ALWAYS:
        want_phases.append({'phase': ph, 'needs': 'browser'})
    return want_phases
