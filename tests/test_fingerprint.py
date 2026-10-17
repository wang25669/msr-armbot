import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msr import fingerprint, config

class MockCDP:
    def __init__(self):
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        return {}

def test_fingerprint():
    # 1. 基础生成测试
    fp1 = fingerprint.generate_fingerprint('acc_alice', country='SG')
    assert fp1['name'] == 'acc_alice'
    assert fp1['country'] == 'SG'
    assert fp1['timezone'] == 'Asia/Singapore'
    assert 'RTX' in fp1['webgl_renderer'] or 'Radeon' in fp1['webgl_renderer'] or 'Iris' in fp1['webgl_renderer'] or 'UHD' in fp1['webgl_renderer']
    assert fp1['hardware_concurrency'] in (4, 6, 8, 12, 16)
    assert fp1['device_memory'] in (8, 16, 32)
    assert 'Edg/141' in fp1['user_agent']

    # 2. 账号确定性测试 (相同账号名称派生完全一致的指纹，保持跨会话稳定性)
    fp1_again = fingerprint.generate_fingerprint('acc_alice', country='SG')
    assert fp1 == fp1_again

    # 3. 多账号差异性测试 (不同账号使用不同指纹)
    fp2 = fingerprint.generate_fingerprint('acc_bob', country='US')
    assert fp2['name'] == 'acc_bob'
    assert fp2['timezone'] == 'America/New_York'
    # 至少核心数、内存或GPU等属性存在差异 (散列输入不同)
    assert (fp1['webgl_renderer'] != fp2['webgl_renderer'] or
            fp1['hardware_concurrency'] != fp2['hardware_concurrency'] or
            fp1['screen_width'] != fp2['screen_width'])

    # 4. 持久化存储测试
    tmpdir = tempfile.mkdtemp(prefix='msr_fp_test_')
    orig_home = os.environ.get('MSR_HOME')
    try:
        os.environ['MSR_HOME'] = tmpdir
        config.init()

        saved_fp = fingerprint.load_or_create('test_user', country='JP')
        assert saved_fp['timezone'] == 'Asia/Tokyo'
        fpath = fingerprint.get_fingerprint_file('test_user')
        assert os.path.isfile(fpath)

        # 二次加载验证
        reloaded = fingerprint.load_or_create('test_user')
        assert reloaded == saved_fp

        # 修改并保存
        saved_fp['screen_width'] = 2560
        fingerprint.save_fingerprint('test_user', saved_fp)
        reloaded2 = fingerprint.load_or_create('test_user')
        assert reloaded2['screen_width'] == 2560
    finally:
        if orig_home is not None:
            os.environ['MSR_HOME'] = orig_home
        else:
            os.environ.pop('MSR_HOME', None)
        shutil.rmtree(tmpdir, ignore_errors=True)

    # 5. CDP 注入调用测试
    mock_cdp = MockCDP()
    fingerprint.apply_to_cdp(mock_cdp, fp1, log=lambda *args: None)
    called_methods = [m[0] for m in mock_cdp.calls]
    assert 'Emulation.setTimezoneOverride' in called_methods
    assert 'Emulation.setLocaleOverride' in called_methods
    assert 'Emulation.setUserAgentOverride' in called_methods
    assert 'Page.addScriptToEvaluateOnNewDocument' in called_methods
    assert 'Network.enable' in called_methods
    assert 'Network.setExtraHTTPHeaders' in called_methods

    print('[OK] 账号独立指纹生成、确定性派生、持久化及CDP注入测试通过')
    print('ALL_OK')

if __name__ == '__main__':
    test_fingerprint()
