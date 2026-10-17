"""planner 单测: sg(全任务区) 与 cn(极简区) 的计划生成，以及阶段名称与别名解析。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from msr import planner

SG_PROMOS = ['edge_entertainment_spotify', 'level_info', 'Rewards_EdgeUsage_progress_info',
             'user_info', 'waitlist_info', 'edge_browsing_streak_eligible',
             'edge_browsing_streak_flight', 'WW_NewLevel3_search_PC']
CN_PROMOS = ['edge_browsing_streak_eligible', 'edge_browsing_streak_flight',
             'WW_NewLevel1_search_PC']

expected_phases = ['appcheckin', 'edge30m', 'dailyset', 'quests', 'moreactivities', 'search', 'visual']

sg_plan = planner.plan({'task_names': SG_PROMOS})
phases = [w['phase'] for w in sg_plan]
print('sg 计划:', phases)
assert phases == expected_phases, phases
assert all(w['needs'] == 'browser' for w in sg_plan[2:])
print('  [OK] API侧在前, 浏览器侧在后, 阶段命名已规范对齐')

cn_plan = planner.plan({'task_names': CN_PROMOS})
phases_cn = [w['phase'] for w in cn_plan]
print('cn 计划:', phases_cn)
assert phases_cn == expected_phases, phases_cn
print('  [OK] cn 与 sg 同构(浏览器内自适配, 无需区域硬编码)')

# force_phases 与别名解析测试
f = planner.plan({'task_names': []}, force_phases='appchekin,read,quick,earn')
phases_f = [w['phase'] for w in f]
print('force 别名解析计划:', phases_f)
assert phases_f == ['appcheckin', 'appread', 'dailyset', 'moreactivities']
print('  [OK] --only 别名自适应映射 (appchekin->appcheckin, read->appread, quick->dailyset, earn->moreactivities)')
print()
print('ALL_OK')
