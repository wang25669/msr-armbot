"""浏览器与 CDP 自愈/标签生命周期单测。"""
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msr import browser


class TestBrowserCDP(unittest.TestCase):

    def test_tab_ids_filters_only_pages(self):
        """测试 tab_ids 严格过滤普通页面，忽略 iframe 与 browser_ui。"""
        mock_targets = [
            {'id': 'page1', 'type': 'page'},
            {'id': 'iframe1', 'type': 'iframe', 'parentId': 'page1'},
            {'id': 'ui1', 'type': 'browser_ui'},
            {'id': 'page2', 'type': 'page'},
        ]
        cdp = browser.CDP('ws://127.0.0.1:9222/devtools/page/page1')
        with patch('msr.browser._http_json', return_value=mock_targets):
            ids = browser.tab_ids(cdp)
            self.assertEqual(ids, ['page1', 'page2'])
            self.assertNotIn('iframe1', ids)
            self.assertNotIn('ui1', ids)
        print('[OK] tab_ids 准确过滤 page 类型，避免误杀 iframe')

    def test_close_tab_protects_non_page_targets(self):
        """测试 close_tab 拒绝关闭非 page 类型目标，保护父级宿主页面。"""
        mock_targets = [
            {'id': 'page1', 'type': 'page'},
            {'id': 'iframe1', 'type': 'iframe'},
        ]
        called_urls = []

        def fake_http_json(url, **kw):
            called_urls.append(url)
            if 'close' in url:
                return {}
            return mock_targets

        with patch('msr.browser._http_json', side_effect=fake_http_json):
            cdp = browser.CDP('ws://127.0.0.1:9222/devtools/page/page1')
            # 尝试关闭 iframe，应被拦截
            browser.close_tab(cdp, 'iframe1')
            self.assertFalse(any('close/iframe1' in u for u in called_urls))

            # 尝试关闭 page1，应允许
            browser.close_tab(cdp, 'page1')
            self.assertTrue(any('close/page1' in u for u in called_urls))
        print('[OK] close_tab 安全保护非 page 目标')

    def test_cdp_reconnect_discovers_or_creates_page(self):
        """测试当原 tab 失效时，CDP._reconnect 能自动探测或创建新 tab 恢复连接。"""
        cdp = browser.CDP('ws://127.0.0.1:9222/devtools/page/old_dead_tab')

        # 模拟原 tab 已被关闭，/json/list 返回新 page
        mock_list = [{'id': 'new_live_tab', 'type': 'page', 'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/page/new_live_tab'}]

        def fake_create_conn(*args, **kw):
            sock = MagicMock()
            if 'old_dead_tab' in cdp.path:
                # 旧 tab 握手返回 404
                sock.recv.return_value = b'HTTP/1.1 404 Not Found\r\n\r\n'
            else:
                # 新 tab 握手成功
                sock.recv.return_value = b'HTTP/1.1 101 Switching Protocols\r\n\r\n'
            return sock

        with patch('socket.create_connection', side_effect=fake_create_conn), \
             patch('msr.browser._http_json', return_value=mock_list):
            success = cdp._reconnect()
            self.assertTrue(success)
            self.assertEqual(cdp.ws_url, 'ws://127.0.0.1:9222/devtools/page/new_live_tab')
            self.assertIsNotNone(cdp.sock)
        print('[OK] CDP._reconnect 自动寻回活跃 page 会话')

    def test_cdp_call_never_raises_none_attribute_error(self):
        """测试即使 sock 为 None，call() 也能自动触发自愈重连而不是抛出 NoneType has no attribute sendall。"""
        cdp = browser.CDP('ws://127.0.0.1:9222/devtools/page/tab1')
        cdp.sock = None

        with patch.object(cdp, '_reconnect', return_value=True), \
             patch.object(cdp, '_call_once', return_value={'result': 'ok'}):
            res = cdp.call('Page.navigate', {'url': 'about:blank'})
            self.assertEqual(res, {'result': 'ok'})
        print('[OK] cdp.call() 杜绝 NoneType.sendall 崩溃并自动触发自愈')

    def test_inject_cookies_batch(self):
        """测试 inject_cookies 优先采用 Network.setCookies 批量写入。"""
        cdp = MagicMock()
        cdp.call.return_value = {}
        cookies = [
            {'name': 'c1', 'value': 'v1', 'domain': '.bing.com'},
            {'name': 'c2', 'value': 'v2', 'domain': '.bing.com'}
        ]
        ok = browser.inject_cookies(cdp, cookies, log=lambda *a: None)
        self.assertEqual(ok, 2)
        # 验证 Network.setCookies 被单次调用且包含两枚 cookie
        set_cookies_calls = [c for c in cdp.call.call_args_list if c[0][0] == 'Network.setCookies']
        self.assertEqual(len(set_cookies_calls), 1)
        self.assertEqual(len(set_cookies_calls[0][0][1]['cookies']), 2)
        print('[OK] inject_cookies 批量 Network.setCookies 调用正常')

    def test_inject_cookies_fallback(self):
        """测试 inject_cookies 在 Network.setCookies 失败时自动降级逐条单设。"""
        cdp = MagicMock()
        def fake_call(method, params=None, **kw):
            if method == 'Network.setCookies':
                raise Exception('CDP method not found')
            if method == 'Network.setCookie':
                return {'success': True}
            return {}
        cdp.call.side_effect = fake_call
        cookies = [
            {'name': 'c1', 'value': 'v1', 'domain': '.bing.com'},
            {'name': 'c2', 'value': 'v2', 'domain': '.bing.com'}
        ]
        ok = browser.inject_cookies(cdp, cookies, log=lambda *a: None)
        self.assertEqual(ok, 2)
        print('[OK] inject_cookies 降级逐条写入自愈正常')

    def test_cdp_pump_events_dispatches(self):
        """测试 CDP.pump_events 能主动解析缓冲区或接收的 WebSocket 事件帧并触发 event_handler。"""
        import struct
        cdp = browser.CDP('ws://127.0.0.1:9222/devtools/page/tab1')
        cdp.sock = MagicMock()
        # 构造符合 RFC 6455 标准的 unmasked WebSocket 文本帧
        event_obj = {'method': 'Network.requestWillBeSent', 'params': {'request': {'url': 'https://login.live.com/oauth20_desktop.srf?code=TEST_OAUTH_CODE'}}}
        payload = json.dumps(event_obj).encode()
        n = len(payload)
        hdr = b'\x81'
        if n < 126:
            hdr += bytes([n])
        elif n < (1 << 16):
            hdr += bytes([126]) + struct.pack('>H', n)
        else:
            hdr += bytes([127]) + struct.pack('>Q', n)
        frame_bytes = hdr + payload
        cdp._rest = frame_bytes

        dispatched = []
        cdp.event_handler = lambda ev: dispatched.append(ev)

        count = cdp.pump_events(timeout=0.05)
        self.assertEqual(count, 1)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]['method'], 'Network.requestWillBeSent')
        print('[OK] CDP.pump_events 异步帧主动排空与事件分发正常')


if __name__ == '__main__':
    unittest.main()
