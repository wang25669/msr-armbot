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

# 凭据存储与独立清理测试
config.save_credential('a', 'user_a@outlook.com', 'Pass123!')
cred_a = config.get_credential('a')
assert cred_a is not None
assert cred_a['username'] == 'user_a@outlook.com'
assert cred_a['password'] == 'Pass123!'
# 验证 accounts.json 同步保存了 username
assert any(acc.get('name') == 'a' and acc.get('username') == 'user_a@outlook.com' for acc in config.accounts())
# 验证账号 b 的凭据为空 (严格隔离)
assert config.get_credential('b') is None

config.delete_credential('a')
assert config.get_credential('a') is None
assert not any(acc.get('name') == 'a' and 'username' in acc for acc in config.accounts())

# 账号彻底清理测试
config.delete_profile('a')
assert not os.path.exists(pa)
assert os.path.exists(pb)

shutil.rmtree(TMP)
print('ALL_OK')
