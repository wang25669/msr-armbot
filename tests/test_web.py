"""web 控制台 RESTful API 完整单测。"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msr import config, web

TMP = tempfile.mkdtemp()
config.init(TMP)

# 寻找空闲端口
s = socket.socket()
s.bind(('127.0.0.1', 0))
port = s.getsockname()[1]
s.close()

# 启动 Web Server 线程
server = web.HTTPServer(('127.0.0.1', port), web.WebHandler)
t = threading.Thread(target=server.serve_forever)
t.daemon = True
t.start()
time.sleep(0.5)

base_url = f'http://127.0.0.1:{port}'

try:
    # 1. 测试 HTML 根路由
    req = urllib.request.Request(f'{base_url}/')
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        html = resp.read().decode()
        assert 'MSR-ArmBot' in html
    print('[OK] Web 首页渲染正常')

    # 2. 测试 GET /api/status
    req = urllib.request.Request(f'{base_url}/api/status')
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert 'accounts' in data
        assert 'scheduler' in data
        assert data['scheduler']['daily_time'] == '01:30'
    print('[OK] /api/status 正常')

    # 3. 测试 POST /api/accounts 添加账号
    req = urllib.request.Request(f'{base_url}/api/accounts',
                                 data=json.dumps({'name': 'web_test_acc', 'proxy': 'http://127.0.0.1:1080', 'enabled': True}).encode(),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        d = json.loads(resp.read().decode())
        assert d.get('status') == 'ok'

    accs = config.accounts()
    assert any(a['name'] == 'web_test_acc' for a in accs)
    print('[OK] /api/accounts 添加账号正常')

    # 3.1 测试 POST /api/auth/device-code 获取设备码
    from msr import api
    orig_dc = api.device_code_login
    orig_poll = api.poll_device_code_once
    api.device_code_login = lambda name, proxy=None: {
        'user_code': 'MOCK1234',
        'device_code': 'mock_dev_code_xyz',
        'verification_uri': 'https://www.microsoft.com/link',
        'interval': 5,
        'expires_in': 900
    }
    api.poll_device_code_once = lambda name, dev_code, proxy=None: None

    try:
        req = urllib.request.Request(f'{base_url}/api/auth/device-code',
                                     data=json.dumps({'name': 'web_test_acc'}).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
            dc_data = json.loads(resp.read().decode())
            assert dc_data['status'] == 'ok'
            assert dc_data['data']['user_code'] == 'MOCK1234'
            assert dc_data['data']['verification_uri'] == 'https://www.microsoft.com/link'
        print('[OK] /api/auth/device-code 获取设备码正常')

        # 3.2 测试 POST /api/auth/device-code/poll 轮询检测 (未授权时返回 complete: False)
        req = urllib.request.Request(f'{base_url}/api/auth/device-code/poll',
                                     data=json.dumps({'name': 'web_test_acc', 'device_code': dc_data['data']['device_code']}).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
            poll_data = json.loads(resp.read().decode())
            assert poll_data['status'] == 'ok'
            assert poll_data['complete'] is False
        print('[OK] /api/auth/device-code/poll 轮询状态正常')
    finally:
        api.device_code_login = orig_dc
        api.poll_device_code_once = orig_poll

    # 4. 测试 POST /api/auth/cookies 导入 Cookie
    cookie_payload = json.dumps({
        'name': 'web_test_acc',
        'cookies_raw': '[{"name": "_U", "value": "val123"}, {"name": "MUID", "value": "muid456"}]'
    }).encode()
    req = urllib.request.Request(f'{base_url}/api/auth/cookies', data=cookie_payload,
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        d = json.loads(resp.read().decode())
        assert d['status'] == 'ok'
        assert d['has_u'] is True
        assert d['count'] == 2
    print('[OK] /api/auth/cookies 导入与校验正常')

    # 4.1 测试 GET /api/auth/cookies 导出 Cookie
    req = urllib.request.Request(f'{base_url}/api/auth/cookies?name=web_test_acc')
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        d = json.loads(resp.read().decode())
        assert d['status'] == 'ok'
        assert d['count'] == 2
        assert d['has_u'] is True
        assert 'json_str' in d
    print('[OK] /api/auth/cookies 导出正常')

    # 4.2 测试 GET /api/status 包含 auth 与 history_7d
    req = urllib.request.Request(f'{base_url}/api/status')
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        d = json.loads(resp.read().decode())
        target = next(a for a in d['accounts'] if a['name'] == 'web_test_acc')
        assert target['auth']['valid'] is True
        assert 'history_7d' in target
        assert len(target['history_7d']['days']) == 7
        assert 'fingerprint' in target
        assert 'webgl_renderer' in target['fingerprint']
    print('[OK] /api/status 返回自动凭据检测与 7 天得分概况正常')

    # 4.3 测试 GET /api/accounts/fingerprint
    req = urllib.request.Request(f'{base_url}/api/accounts/fingerprint?name=web_test_acc')
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        fp_resp = json.loads(resp.read().decode())
        assert fp_resp['status'] == 'ok'
        fp_data = fp_resp['data']
        assert fp_data['name'] == 'web_test_acc'
        assert fp_data['country'] in ('SG', 'US')
        assert 'hardware_concurrency' in fp_data

    # 4.4 测试 POST /api/accounts/fingerprint/generate (重新随机生成)
    req = urllib.request.Request(f'{base_url}/api/accounts/fingerprint/generate',
                                 data=json.dumps({'name': 'web_test_acc', 'country': 'JP'}).encode(),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        gen_resp = json.loads(resp.read().decode())
        assert gen_resp['status'] == 'ok'
        new_fp = gen_resp['data']
        assert 'webgl_renderer' in new_fp
    print('[OK] /api/accounts/fingerprint 端点正常')

    # 4.5 测试 POST /api/accounts/proxy-test 与 POST /api/accounts/fingerprint/align-proxy
    from msr import geo
    orig_probe = geo.probe_proxy_geo
    geo.probe_proxy_geo = lambda proxy=None, timeout=7: {
        'status': 'ok',
        'ip': '123.45.67.89',
        'country': 'CN',
        'country_name': '中国',
        'region': 'Shanghai',
        'city': 'Shanghai',
        'timezone': 'Asia/Shanghai',
        'latency_ms': 42
    }
    try:
        req = urllib.request.Request(f'{base_url}/api/accounts/proxy-test',
                                     data=json.dumps({'proxy': 'http://127.0.0.1:1080', 'name': 'web_test_acc'}).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            pt_data = json.loads(resp.read().decode())
            assert pt_data['status'] == 'ok'
            assert pt_data['data']['country'] == 'CN'
            assert pt_data['data']['timezone'] == 'Asia/Shanghai'

        req = urllib.request.Request(f'{base_url}/api/accounts/fingerprint/align-proxy',
                                     data=json.dumps({'name': 'web_test_acc'}).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            ap_data = json.loads(resp.read().decode())
            assert ap_data['status'] == 'ok'
            assert ap_data['timezone'] == 'Asia/Shanghai'
            assert 'zh-CN' in ap_data['data']['locale']
        print('[OK] /api/accounts/proxy-test 与 align-proxy 正常')
    finally:
        geo.probe_proxy_geo = orig_probe

    # 5. 测试 POST /api/settings 配置修改
    req = urllib.request.Request(f'{base_url}/api/settings',
                                 data=json.dumps({'search': {'dwell_min_seconds': 135}}).encode(),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
    from msr import settings
    val = settings.get('search.dwell_min_seconds', home=TMP)
    assert val == 135
    print('[OK] /api/settings 更新全局参数正常')

    # 5.1 测试 POST /api/settings/reset 恢复出厂设置
    req = urllib.request.Request(f'{base_url}/api/settings/reset', data=b'{}',
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
    val_reset = settings.get('search.dwell_min_seconds', home=TMP)
    assert val_reset == 180
    print('[OK] /api/settings/reset 恢复默认值正常')

    # 6. 测试 GET /api/logs
    req = urllib.request.Request(f'{base_url}/api/logs')
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        text = resp.read().decode()
        assert len(text) > 0
    print('[OK] /api/logs 正常')

    # 7. 测试 DELETE /api/accounts 删除账号
    req = urllib.request.Request(f'{base_url}/api/accounts?name=web_test_acc', method='DELETE')
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
    accs_after = config.accounts()
    assert not any(a['name'] == 'web_test_acc' for a in accs_after)
    print('[OK] /api/accounts 删除账号正常')

finally:
    server.shutdown()
    shutil.rmtree(TMP)

print('ALL_OK')
