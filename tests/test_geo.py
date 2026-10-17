"""geo 地理位置探测与缓存模块单测。"""
import os
import shutil
import sys
import tempfile
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msr import config, geo

TMP = tempfile.mkdtemp()
config.init(TMP)

try:
    # 1. 模拟 ip.sb 成功返回
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"ip":"123.45.67.89","country":"China","country_code":"CN","region":"Shanghai","city":"Shanghai","timezone":"Asia/Shanghai"}'
    mock_resp.__enter__.return_value = mock_resp

    with patch('urllib.request.build_opener') as mock_opener:
        mock_instance = MagicMock()
        mock_instance.open.return_value = mock_resp
        mock_opener.return_value = mock_instance

        res = geo.probe_proxy_geo('http://127.0.0.1:8888')
        assert res['status'] == 'ok'
        assert res['country'] == 'CN'
        assert res['country_name'] == '中国'
        assert res['timezone'] == 'Asia/Shanghai'
        assert res['ip'] == '123.45.67.89'
    print('[OK] probe_proxy_geo 基础解析正常')

    # 2. 测试缓存读取与更新机制
    with patch('msr.geo.probe_proxy_geo') as mock_probe:
        mock_probe.return_value = {
            'status': 'ok',
            'ip': '1.2.3.4',
            'country': 'SG',
            'country_name': '新加坡',
            'timezone': 'Asia/Singapore'
        }
        # 首次调用探测并落盘
        g1 = geo.get_cached_proxy_geo('acc_sg', proxy='http://sg-proxy:8080')
        assert g1['country'] == 'SG'
        assert mock_probe.call_count == 1

        # 第二次调用命中缓存，不发起网络探测
        g2 = geo.get_cached_proxy_geo('acc_sg', proxy='http://sg-proxy:8080')
        assert g2['country'] == 'SG'
        assert mock_probe.call_count == 1  # 依然是 1

        # 若代理地址变更，应重新探测
        mock_probe.return_value = {
            'status': 'ok',
            'ip': '5.6.7.8',
            'country': 'US',
            'country_name': '美国',
            'timezone': 'America/New_York'
        }
        g3 = geo.get_cached_proxy_geo('acc_sg', proxy='http://us-proxy:8080')
        assert g3['country'] == 'US'
        assert mock_probe.call_count == 2
    print('[OK] get_cached_proxy_geo 缓存与失效刷新正常')

finally:
    shutil.rmtree(TMP)

print('ALL_OK')
