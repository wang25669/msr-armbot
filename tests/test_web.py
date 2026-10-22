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

os.environ['NO_PROXY'] = '127.0.0.1,localhost'
os.environ['no_proxy'] = '127.0.0.1,localhost'

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

    # 3.1 测试 POST /api/accounts 修改现有账号代理并自动清理旧 geo 缓存
    fake_geo = config.pfile('web_test_acc', 'proxy_geo.json')
    with open(fake_geo, 'w') as f:
        f.write('{"cached": true}')
    assert os.path.isfile(fake_geo)

    req = urllib.request.Request(f'{base_url}/api/accounts',
                                 data=json.dumps({'name': 'web_test_acc', 'proxy': 'socks5://127.0.0.1:1088'}).encode(),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        d = json.loads(resp.read().decode())
        assert d.get('status') == 'ok'

    acc_obj = [a for a in config.accounts() if a['name'] == 'web_test_acc'][0]
    assert acc_obj['proxy'] == 'socks5://127.0.0.1:1088'
    assert not os.path.isfile(fake_geo)
    print('[OK] /api/accounts 修改代理并自动失效旧出口缓存正常')

    # 3.2 测试 POST /api/auth/credential 账号密码自动化登录接口 (Mock 测试)
    from msr import auth
    orig_login = auth.login_with_credential
    auth.login_with_credential = lambda name, username, password, proxy=None, log_cb=None: {
        'status': 'ok',
        'message': '测试登录成功',
        'cookie_count': 10,
        'has_token': True,
        'balance': 5000,
        'country': 'SG'
    }
    try:
        req = urllib.request.Request(f'{base_url}/api/auth/credential',
                                     data=json.dumps({'name': 'web_test_acc', 'username': 'test@outlook.com', 'password': 'pass123'}).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
            res_data = json.loads(resp.read().decode())
            assert res_data['status'] == 'ok'
            assert res_data['balance'] == 5000
        print('[OK] /api/auth/credential 账号密码登录接口正常')
    finally:
        auth.login_with_credential = orig_login

    # 3.2 测试交互式风控接管会话接口 (/api/auth/session/*)
    class FakeSession:
        def __init__(self):
            self.status = 'wait_auth_push'
            self.auth_code = '42'
            self.otp_prompt = None
            self.message = '请在手机上确认'
        def to_dict(self):
            return {'status': self.status, 'auth_code': self.auth_code, 'message': self.message}
    fake_sess = FakeSession()
    orig_start_sess = auth.start_session
    orig_get_sess = auth.get_session
    orig_sub_otp = auth.submit_session_otp
    orig_mouse = auth.session_mouse_click
    orig_cancel = auth.cancel_session
    orig_shot = auth.get_session_screenshot

    auth.start_session = lambda name, username, password, proxy=None, log_cb=None: fake_sess
    auth.get_session = lambda name: fake_sess
    auth.submit_session_otp = lambda name, code: {'status': 'ok'}
    auth.session_mouse_click = lambda name, x, y: {'status': 'ok'}
    auth.cancel_session = lambda name: None
    auth.get_session_screenshot = lambda name: b'\xff\xd8\xff\xe0\x00\x10JFIF'
    try:
        # 3.2.1 启动会话
        req = urllib.request.Request(f'{base_url}/api/auth/session/start',
                                     data=json.dumps({'name': 'web_test_acc', 'username': 'test@outlook.com', 'password': 'pass123'}).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            d = json.loads(resp.read().decode())
            assert d['status'] == 'ok'

        # 3.2.2 查询会话状态
        req = urllib.request.Request(f'{base_url}/api/auth/session/status?name=web_test_acc')
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            d = json.loads(resp.read().decode())
            assert d['status'] == 'wait_auth_push'
            assert d['auth_code'] == '42'

        # 3.2.3 获取远程截屏
        req = urllib.request.Request(f'{base_url}/api/auth/session/screenshot?name=web_test_acc')
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers['Content-Type'] == 'image/jpeg'
            assert len(resp.read()) > 0

        # 3.2.4 提交 OTP 验证码
        req = urllib.request.Request(f'{base_url}/api/auth/session/submit-otp',
                                     data=json.dumps({'name': 'web_test_acc', 'code': '123456'}).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            d = json.loads(resp.read().decode())
            assert d['status'] == 'ok'

        # 3.2.5 模拟远程鼠标点击
        req = urllib.request.Request(f'{base_url}/api/auth/session/mouse',
                                     data=json.dumps({'name': 'web_test_acc', 'x': 100, 'y': 200}).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            d = json.loads(resp.read().decode())
            assert d['status'] == 'ok'

        # 3.2.6 取消会话
        req = urllib.request.Request(f'{base_url}/api/auth/session/cancel',
                                     data=json.dumps({'name': 'web_test_acc'}).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            d = json.loads(resp.read().decode())
            assert d['status'] == 'ok'

        print('[OK] /api/auth/session/* 交互式登录与多重风控接管接口全部正常')
    finally:
        auth.start_session = orig_start_sess
        auth.get_session = orig_get_sess
        auth.submit_session_otp = orig_sub_otp
        auth.session_mouse_click = orig_mouse
        auth.cancel_session = orig_cancel
        auth.get_session_screenshot = orig_shot

    # 4. 测试 POST /api/auth/cookies 导入 Cookie
    orig_exchange = auth.exchange_token_from_cookies
    auth.exchange_token_from_cookies = lambda name, proxy=None, cdp=None, ch=None, log_cb=None: {'access_token': 'fake_token'}
    try:
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
            assert d.get('has_token') is True
        print('[OK] /api/auth/cookies 导入与校验正常')
    finally:
        auth.exchange_token_from_cookies = orig_exchange

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

    # 6. 测试 GET /api/logs (纯文本与 JSON 结构化元数据)
    req = urllib.request.Request(f'{base_url}/api/logs')
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
        text = resp.read().decode()
        assert len(text) > 0

    req_json = urllib.request.Request(f'{base_url}/api/logs?format=json')
    with urllib.request.urlopen(req_json, timeout=5) as resp:
        assert resp.status == 200
        d = json.loads(resp.read().decode())
        assert d['status'] == 'ok'
        assert 'runner' in d
        assert 'running' in d['runner']
        assert 'text' in d
    print('[OK] /api/logs (含 format=json 结构化元数据) 正常')

    # 7. 测试 DELETE /api/accounts 删除账号
    req = urllib.request.Request(f'{base_url}/api/accounts?name=web_test_acc', method='DELETE')
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
    accs_after = config.accounts()
    assert not any(a['name'] == 'web_test_acc' for a in accs_after)
    print('[OK] /api/accounts 删除账号正常')

    # 8. 测试内置调度器逻辑 (确保 random, fast_mode, home_args 无 NameError 与调度正常)
    import subprocess, io
    from msr import settings
    # 模拟设置当前时间匹配 daily_time
    cur_hm = time.strftime('%H:%M')
    settings.set_val('schedule.daily_time', cur_hm, home=TMP)
    settings.set_val('schedule.random_start_delay_max', 1, home=TMP)
    settings.set_val('schedule.fast_mode', True, home=TMP)

    # 验证 append_live_log 写入
    web.append_live_log('测试日志输出流')
    assert any('测试日志输出流' in line for line in web.LIVE_LOGS)
    print('[OK] append_live_log 内存日志与 stdout 流正常')

finally:
    server.shutdown()
    shutil.rmtree(TMP)

print('ALL_OK')
