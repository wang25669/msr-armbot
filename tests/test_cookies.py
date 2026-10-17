"""cookies 单测: 支持 JSON 数组、Raw header 与校验逻辑。"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msr import config, cookies

TMP = tempfile.mkdtemp()
config.init(TMP)

# 1. 验证 JSON 数组导入与清洗
json_str = '''[
  {"name": "_U", "value": "auth_token_value_123", "domain": ".bing.com", "path": "/"},
  {"name": "MUID", "value": "muid_abc_456"}
]'''
parsed = cookies.import_content(json_str)
assert len(parsed) == 2
diag = cookies.validate_cookies(parsed)
assert diag['valid'] is True
assert diag['has_auth_u'] is True
assert diag['has_muid'] is True
print('[OK] JSON 数组解析与凭据校验正常')

# 2. 验证 Raw Header 字符串导入
raw_header = "_U=token_raw_789; MUID=muid_raw; SRCHD=AF=NOFORM"
parsed_raw = cookies.import_content(raw_header)
assert len(parsed_raw) == 3
assert any(c['name'] == '_U' and c['value'] == 'token_raw_789' for c in parsed_raw)
print('[OK] Raw Cookie 请求头格式解析正常')

# 3. 验证文件保存到 profile
f_path = os.path.join(TMP, 'test_ck.json')
with open(f_path, 'w') as f:
    f.write(json_str)

res = cookies.import_file('test_acc', f_path)
assert res['valid'] is True
loaded = config.load_json(config.pfile('test_acc', 'cookie.json'))
assert len(loaded) == 2
print('[OK] Cookie 文件持久化导入正常')

shutil.rmtree(TMP)
print('ALL_OK')
