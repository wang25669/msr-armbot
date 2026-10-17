"""执行计划: 探测结果 -> 当天要跑的阶段列表。"""
from msr import api

# 规范任务名 -> 执行器。声明式映射, planner 只做存在性判断, 不硬编码区域差异。
# needs: api = 纯 HTTP 即可; browser = 需要 headless chromium。
TASK_TABLE = {
    'pcSearch':                 {'phase': 'search',   'needs': 'browser'},
    'dailySetPromotions':       {'phase': 'quick',    'needs': 'browser'},
    'quests':                   {'phase': 'quests',   'needs': 'browser'},
    'morePromotions':           {'phase': 'earn',     'needs': 'browser'},
    'visualSearch':             {'phase': 'visual',   'needs': 'browser', 'optional': True},
    'edge_browsing_streak_flight':   {'phase': 'edge_api',  'needs': 'api'},
    'edge_browsing_streak_eligible': {'phase': 'edge_api',  'needs': 'api'},
    'app_checkin':              {'phase': 'app',      'needs': 'api'},
    'ENUS_readarticle3_30points': {'phase': 'read',   'needs': 'api'},
}


def classify(promo_name):
    """promotion name -> 规范任务名(无匹配返 None)。"""
    if 'pcSearch' in promo_name or 'search' in promo_name.lower() and 'WW_' in promo_name:
        return 'pcSearch'
    if 'DailySet' in promo_name:
        return 'dailySetPromotions'
    if 'punchcard' in promo_name.lower() or 'quest' in promo_name.lower():
        return 'quests'
    if 'morePromotions' in promo_name or promo_name.startswith('WW_Rewards'):
        return 'morePromotions'
    if 'visual' in promo_name.lower():
        return 'visualSearch'
    if 'edge_browsing_streak' in promo_name:
        return promo_name
    if 'readarticle' in promo_name:
        return 'ENUS_readarticle3_30points'
    return None


def plan(probe_result, force_phases=None):
    """probe 结果 -> 有序阶段列表 [{phase, needs}]。

    force_phases: 用户 --only 指定, 直接采用(调试用)。
    固定顺序: api 侧在前(便宜快速), 浏览器侧在后。
    """
    if force_phases:
        order = force_phases.split(',')
        out = []
        for ph in order:
            needs = next((t['needs'] for t in TASK_TABLE.values() if t['phase'] == ph), 'api')
            out.append({'phase': ph, 'needs': needs})
        return out

    # API 侧: 按 promotions 实际存在判断(与现网 edge_app_streak 同源, 实测可靠)
    have = set()
    for n in probe_result.get('task_names', []):
        c = classify(n)
        if c:
            have.add(c)

    # 浏览器侧: promotions 不含 dailySet/morePromotions/visual/quests(它们在 rewards
    # dashboard/earn 页面下发, 不在 /dapi/me)。这些阶段默认尝试, 由 executor 在浏览器里
    # 按当天实际卡片执行 —— 卡片不存在自然跳过, 天然区域自适应。
    BROWSER_ALWAYS = ['search', 'quick', 'quests', 'earn', 'visual']

    want_phases = [{'phase': 'app', 'needs': 'api'}]          # type103 总是有效
    if 'edge_browsing_streak_flight' in have or 'edge_browsing_streak_eligible' in have:
        want_phases.append({'phase': 'edge_api', 'needs': 'api'})
    if 'ENUS_readarticle3_30points' in have:
        want_phases.append({'phase': 'read', 'needs': 'api'})
    for ph in BROWSER_ALWAYS:
        want_phases.append({'phase': ph, 'needs': 'browser'})
    return want_phases

