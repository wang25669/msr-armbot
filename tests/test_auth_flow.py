import unittest
from msr import auth

class MockCDP:
    def __init__(self, return_val):
        self.return_val = return_val
        self.last_eval = None

    def call(self, method, params=None, timeout=60):
        if method == 'Runtime.evaluate':
            self.last_eval = params.get('expression')
            return {'result': {'value': self.return_val}}
        return {}

class TestAuthRedesign(unittest.TestCase):

    def test_inspect_kmsi_modern(self):
        cdp = MockCDP({'type': 'kmsi'})
        state = auth.inspect_login_page(cdp)
        self.assertEqual(state.get('type'), 'kmsi')

    def test_inspect_dismissible_passkey(self):
        cdp = MockCDP({'type': 'dismissible_prompt', 'has_passkey': True})
        state = auth.inspect_login_page(cdp)
        self.assertEqual(state.get('type'), 'dismissible_prompt')

    def test_inspect_auth_push(self):
        cdp = MockCDP({'type': 'auth_push', 'code': '42'})
        state = auth.inspect_login_page(cdp)
        self.assertEqual(state.get('type'), 'auth_push')
        self.assertEqual(state.get('code'), '42')

    def test_inspect_otp(self):
        cdp = MockCDP({'type': 'otp_input', 'prompt': '请输入验证码'})
        state = auth.inspect_login_page(cdp)
        self.assertEqual(state.get('type'), 'otp_input')

    def test_session_to_dict_includes_viewport(self):
        sess = auth.LoginSession('test_acc', 'user@live.com', 'pwd')
        d = sess.to_dict()
        self.assertIn('viewport', d)
        self.assertEqual(d['viewport']['w'], 1280)
        self.assertEqual(d['viewport']['h'], 757)

if __name__ == '__main__':
    unittest.main()
