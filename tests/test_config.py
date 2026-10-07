"""config 单测: 多账号 profile 隔离。"""
import json
import os
import shutil
import sys
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp()
from msr import config
config.init(TMP)

config.save_accounts([{'name': 'a', 'enabled': True}, {'name': 'b', 'enabled': True}])
assert [a['name'] for a in config.accounts()] == ['a', 'b']

pa = config.profile('a')
pb = config.profile('b')
assert pa != pb and os.path.isdir(pa) and os.path.isdir(pb)
assert config.pfile('a', 'token.json') == os.path.join(pa, 'token.json')

config.save_json(config.pfile('a', 'state.json'), {'balance': 100})
assert config.load_json(config.pfile('a', 'state.json'))['balance'] == 100
assert config.load_json(config.pfile('b', 'state.json'), {}) == {}

shutil.rmtree(TMP)
print('ALL_OK')
