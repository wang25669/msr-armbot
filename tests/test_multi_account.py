"""多账号与独立代理配置单测。"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msr import config, planner

TMP = tempfile.mkdtemp()
config.init(TMP)

# 1. 创建两组账号: cn 直连, sg 走独立代理
accs = [
    {'name': 'cn_user', 'enabled': True, 'proxy': None, 'offset_minutes': 0},
    {'name': 'sg_user', 'enabled': True, 'proxy': 'http://127.0.0.1:7890', 'cdp_port': 9222, 'offset_minutes': 10}
]
config.save_accounts(accs)

# 2. 读取并验证账号独立配置
loaded = config.accounts()
assert len(loaded) == 2
assert config.get_account('cn_user')['proxy'] is None
assert config.get_account('sg_user')['proxy'] == 'http://127.0.0.1:7890'
assert config.get_account('sg_user')['cdp_port'] == 9222
print('[OK] 多账号独立代理与端口配置正常')

# 3. 验证 profile 目录隔离
p_cn = config.profile('cn_user')
p_sg = config.profile('sg_user')
assert p_cn != p_sg
assert os.path.isdir(p_cn) and os.path.isdir(p_sg)

# 4. 验证计划生成包含打卡任务与浏览器自适配
cn_pr = {'country': 'CN', 'task_names': ['edge_browsing_streak_flight', 'WW_NewLevel1_search_PC']}
sg_pr = {'country': 'SG', 'task_names': ['edge_entertainment_spotify', 'edge_browsing_streak_flight', 'WW_NewLevel3_search_PC']}

plan_cn = planner.plan(cn_pr)
plan_sg = planner.plan(sg_pr)

phases_cn = [w['phase'] for w in plan_cn]
phases_sg = [w['phase'] for w in plan_sg]

assert 'quests' in phases_cn and 'quests' in phases_sg
assert 'dailyset' in phases_cn and 'dailyset' in phases_sg
assert 'search' in phases_cn and 'search' in phases_sg
print('[OK] 区域任务计划生成包含打卡任务 (quests) 与每日活动 (dailyset)')

# 5. 验证账号停用 (enabled: False) 过滤与生效机制
accs[0]['enabled'] = False
config.save_accounts(accs)
active_accs = [a for a in config.accounts() if a.get('enabled', True)]
assert len(active_accs) == 1
assert active_accs[0]['name'] == 'sg_user'
print('[OK] 账号停用过滤机制生效正常')

shutil.rmtree(TMP)
print('ALL_OK')
