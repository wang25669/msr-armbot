"""planner 单测: sg(全任务区) 与 cn(极简区) 的计划生成。"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from msr import planner

SG_PROMOS = ['edge_entertainment_spotify', 'level_info', 'Rewards_EdgeUsage_progress_info',
             'user_info', 'waitlist_info', 'edge_browsing_streak_eligible',
             'edge_browsing_streak_flight', 'WW_NewLevel3_search_PC']
CN_PROMOS = ['edge_browsing_streak_eligible', 'edge_browsing_streak_flight',
             'WW_NewLevel1_search_PC']

sg_plan = planner.plan({'task_names': SG_PROMOS})
phases = [w['phase'] for w in sg_plan]
print('sg 计划:', phases)
assert phases == ['app', 'edge_api', 'search', 'quick', 'earn'], phases
assert all(w['needs'] == 'browser' for w in sg_plan[2:])
print('  [OK] API侧在前, 浏览器侧在后, 无视觉')

cn_plan = planner.plan({'task_names': CN_PROMOS})
phases_cn = [w['phase'] for w in cn_plan]
print('cn 计划:', phases_cn)
assert phases_cn == ['app', 'edge_api', 'search', 'quick', 'earn'], phases_cn
print('  [OK] cn 与 sg 同构(浏览器内自适配, 无需区域硬编码)')

# force_phases: 调试用直通
f = planner.plan({'task_names': []}, force_phases='app,read')
print('force 计划:', [w['phase'] for w in f])
assert [w['phase'] for w in f] == ['app', 'read']
print('  [OK] --only 直通')
print()
print('ALL_OK')
