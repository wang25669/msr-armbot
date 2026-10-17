"""网络出口与代理地理位置探测模块。

用于精确侦测代理出口的公网 IP、物理国家、城市与真实时区，
为 Chromium 浏览器指纹的时区/语言注入及微软漫游风险诊断提供客观依据。
"""
import json
import os
import time
import urllib.parse
import urllib.request

COUNTRY_NAMES = {
    'CN': '中国',
    'SG': '新加坡',
    'US': '美国',
    'JP': '日本',
    'GB': '英国',
    'DE': '德国',
    'HK': '中国香港',
    'TW': '中国台湾',
    'AU': '澳大利亚',
    'CA': '加拿大',
    'FR': '法国',
    'KR': '韩国'
}


def _build_opener(proxy=None):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
    handlers.append(urllib.request.HTTPSHandler())
    return urllib.request.build_opener(*handlers)


def probe_proxy_geo(proxy=None, timeout=5):
    """通过指定代理(若 None 则直连)探测公网出口 IP 及其地理归属信息。
    采用多层高可用国内/海外双冗余通道 (HTTPS 优先，防中国境内 HTTP 阻断与路由异常)。
    """
    opener = _build_opener(proxy)
    t0 = time.time()
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    # 1. 首选 api.ip.sb (全球 Anycast CDN, 极速 HTTPS, 国内移动/电信/联通及海外均直达)
    try:
        url = 'https://api.ip.sb/geoip'
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=timeout) as r:
            d = json.load(r)
            c_code = (d.get('country_code') or 'CN').upper()
            latency = round((time.time() - t0) * 1000, 1)
            return {
                'status': 'ok',
                'provider': 'ip.sb',
                'ip': d.get('ip'),
                'country': c_code,
                'country_name': COUNTRY_NAMES.get(c_code, d.get('country') or c_code),
                'region': d.get('region', ''),
                'city': d.get('city', ''),
                'timezone': d.get('timezone', 'Asia/Shanghai'),
                'latency_ms': latency
            }
    except Exception:
        pass

    # 2. 备选 ipwho.is (高精度 HTTPS, 完美兼容中国大陆与海外节点)
    try:
        url = 'https://ipwho.is/'
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=timeout) as r:
            d = json.load(r)
            if d.get('success'):
                c_code = (d.get('country_code') or 'CN').upper()
                tz = d.get('timezone', {}).get('id') if isinstance(d.get('timezone'), dict) else 'Asia/Shanghai'
                latency = round((time.time() - t0) * 1000, 1)
                return {
                    'status': 'ok',
                    'provider': 'ipwho.is',
                    'ip': d.get('ip'),
                    'country': c_code,
                    'country_name': COUNTRY_NAMES.get(c_code, d.get('country') or c_code),
                    'region': d.get('region', ''),
                    'city': d.get('city', ''),
                    'timezone': tz or 'Asia/Shanghai',
                    'latency_ms': latency
                }
    except Exception:
        pass

    # 3. 备选 myip.la (HTTPS，国内及海外均支持，中文省市名称)
    try:
        url = 'https://api.myip.la/cn?json'
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=timeout) as r:
            d = json.load(r)
            loc = d.get('location', {})
            c_code = (loc.get('country_code') or 'CN').upper()
            latency = round((time.time() - t0) * 1000, 1)
            return {
                'status': 'ok',
                'provider': 'myip.la',
                'ip': d.get('ip'),
                'country': c_code,
                'country_name': loc.get('country_name') or COUNTRY_NAMES.get(c_code, c_code),
                'region': loc.get('province', ''),
                'city': loc.get('city', ''),
                'timezone': 'Asia/Shanghai' if c_code == 'CN' else 'Asia/Singapore',
                'latency_ms': latency
            }
    except Exception:
        pass

    # 4. 备选 ip-api.com (HTTP 端口备选)
    try:
        url = 'http://ip-api.com/json/?fields=status,country,countryCode,regionName,city,timezone,query'
        req = urllib.request.Request(url, headers={'User-Agent': 'curl/7.88.1'})
        with opener.open(req, timeout=timeout) as r:
            d = json.load(r)
            if d.get('status') == 'success':
                latency = round((time.time() - t0) * 1000, 1)
                c_code = (d.get('countryCode') or 'US').upper()
                return {
                    'status': 'ok',
                    'provider': 'ip-api.com',
                    'ip': d.get('query'),
                    'country': c_code,
                    'country_name': COUNTRY_NAMES.get(c_code, d.get('country') or c_code),
                    'region': d.get('regionName', ''),
                    'city': d.get('city', ''),
                    'timezone': d.get('timezone', 'Asia/Shanghai'),
                    'latency_ms': latency
                }
    except Exception:
        pass

    return {
        'status': 'error',
        'message': '出口探测超时或代理服务器不可达'
    }


def get_cached_proxy_geo(name, proxy=None, max_age_hours=24):
    """读取或更新账号绑定的代理出口探测缓存，避免频繁请求被三方限流。"""
    from msr import config
    fpath = config.pfile(name, 'proxy_geo.json')
    if os.path.isfile(fpath):
        try:
            cached = config.load_json(fpath)
            # 若代理地址没有变化且未过期
            if cached and cached.get('proxy') == proxy and time.time() - cached.get('probed_time', 0) < max_age_hours * 3600:
                return cached.get('geo', {})
        except Exception:
            pass

    geo = probe_proxy_geo(proxy)
    if geo.get('status') == 'ok':
        config.save_json(fpath, {
            'proxy': proxy,
            'probed_time': time.time(),
            'geo': geo
        })
    return geo
