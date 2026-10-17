"""settings 单测: 默认配置继承、动态修改、持久化合并与多级键读写。"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msr import config, settings

TMP = tempfile.mkdtemp()
config.init(TMP)

# 1. 验证默认配置完整性
st = settings.load_settings(TMP)
assert st['search']['dwell_min_seconds'] == 180
assert st['search']['dwell_max_seconds'] == 300
assert st['search']['stall_max_retries'] == 12
assert st['cards']['attribution_wait_min'] == 60
assert 'serverchan' in st['notify']
print('[OK] 默认配置结构完整')

# 2. 验证动态 set_val 与 get
settings.set_val('search.dwell_min_seconds', 150, home=TMP)
val = settings.get('search.dwell_min_seconds', home=TMP)
assert val == 150

settings.set_val('notify.serverchan.sendkey', 'SCT_TEST_KEY', home=TMP)
key = settings.get('notify.serverchan.sendkey', home=TMP)
assert key == 'SCT_TEST_KEY'
print('[OK] 多级参数动态读写与类型自适应正常')

# 3. 验证持久化合并 (部分修改不覆盖其他默认项)
st2 = settings.load_settings(TMP)
assert st2['search']['dwell_min_seconds'] == 150
assert st2['search']['dwell_max_seconds'] == 300  # 默认值保留
assert st2['notify']['serverchan']['sendkey'] == 'SCT_TEST_KEY'
print('[OK] 持久化深度合并正常')

shutil.rmtree(TMP)
print('ALL_OK')
