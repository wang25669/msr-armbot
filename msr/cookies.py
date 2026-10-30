"""Cookie 解析、导入、清洗与校验模块。

兼容主流导出格式:
1. Cookie-Editor 导出的标准 JSON 数组
2. EditThisCookie 导出的 JSON 数组
3. 请求头格式的 Raw Cookie 字符串 ("name1=val1; name2=val2")
4. Netscape / curl 格式文本
"""
import json
import os
import re
import time


def _normalize_cookie(c):
    """规范化单个 cookie 结构，补齐 CDP 所需默认属性。"""
    name = str(c.get('name') or c.get('Name') or '').strip()
    if not name:
        return None
    val = str(c.get('value') or c.get('Value') or '')
    domain = c.get('domain') or c.get('Domain') or '.bing.com'
    path = c.get('path') or c.get('Path') or '/'
    secure = bool(c.get('secure', True))
    http_only = bool(c.get('httpOnly', False) or c.get('hostOnly', False))
    exp = c.get('expirationDate') or c.get('expires')
    
    item = {
        'name': name,
        'value': val,
        'domain': domain,
        'path': path,
        'secure': secure,
        'httpOnly': http_only
    }
    if exp:
        try:
            item['expirationDate'] = float(exp)
        except (ValueError, TypeError):
            pass
    return item


def parse_raw_header(raw_str):
    """解析 HTTP 请求头格式的 Cookie 字符串。"""
    cookies = []
    pairs = raw_str.split(';')
    for p in pairs:
        if '=' not in p:
            continue
        k, v = p.split('=', 1)
        k = k.strip()
        v = v.strip()
        if k:
            cookies.append({
                'name': k,
                'value': v,
                'domain': '.bing.com',
                'path': '/',
                'secure': True,
                'httpOnly': False
            })
    return cookies


def parse_netscape(text):
    """解析 Netscape / curl cookies.txt 格式。"""
    cookies = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) >= 7:
            cookies.append({
                'domain': parts[0],
                'path': parts[2],
                'secure': parts[3].lower() == 'true',
                'expirationDate': float(parts[4]) if parts[4].isdigit() else None,
                'name': parts[5],
                'value': parts[6],
                'httpOnly': False
            })
    return cookies


def validate_cookies(cookies):
    """检查 Cookie 是否包含 Bing/MSR 核心登录态。"""
    if not cookies:
        return {'valid': False, 'reason': 'Cookie 为空', 'count': 0}
    
    names = {c.get('name') for c in cookies}
    has_u = '_U' in names
    has_muid = 'MUID' in names
    has_srch = any('SRCH' in n for n in names)
    
    # _U 是 Bing 识别已登录 MSR 账户的核心票据，绝对不可仅凭匿名追踪票据 MUID 误判为已登录
    valid = bool(has_u)
    reason = 'OK' if valid else '缺少核心登录凭据 (_U)'
    return {
        'valid': valid,
        'has_auth_u': has_u,
        'has_muid': has_muid,
        'has_srch': has_srch,
        'count': len(cookies),
        'reason': reason
    }


def import_content(content_str):
    """自动推断格式并解析为规范化 Cookie 列表。"""
    content_str = content_str.strip()
    if not content_str:
        return []
    
    # 尝试 JSON
    if content_str.startswith('[') and content_str.endswith(']'):
        try:
            arr = json.loads(content_str)
            if isinstance(arr, list):
                res = []
                for x in arr:
                    n = _normalize_cookie(x)
                    if n:
                        res.append(n)
                return res
        except Exception:
            pass
    
    # 尝试 Netscape
    if '\t' in content_str and ('bing.com' in content_str or 'microsoft.com' in content_str):
        ns = parse_netscape(content_str)
        if ns:
            return ns

    # 尝试 Raw Header
    if '=' in content_str:
        return parse_raw_header(content_str)
        
    return []


# 微软生态体系白名单后缀（包含其所有子域名）
MICROSOFT_DOMAIN_SUFFIXES = (
    'bing.com',
    'live.com',
    'microsoft.com',
    'microsoftonline.com',
    'windows.net',
    'msn.com',
    'xbox.com',
    'xboxlive.com',
)

# 降级备用 URL 列表（覆盖所有核心服务）
FALLBACK_MS_URLS = [
    'https://login.live.com',
    'https://account.live.com',
    'https://www.bing.com',
    'https://rewards.bing.com',
    'https://cn.bing.com',
    'https://account.microsoft.com',
    'https://rewards.microsoft.com',
    'https://login.microsoft.com',
    'https://login.microsoftonline.com',
    'https://www.microsoft.com',
    'https://www.msn.com',
]


def filter_microsoft_cookies(cookies):
    """过滤保留微软生态体系相关的 Cookie，剔除第三方广告、跟踪器及外站网页 Cookie。"""
    res = []
    seen = set()
    for c in cookies:
        domain = (c.get('domain') or '').lower().strip('.')
        if any(domain == s or domain.endswith('.' + s) for s in MICROSOFT_DOMAIN_SUFFIXES):
            key = (c.get('name'), c.get('domain'), c.get('path'))
            if key not in seen:
                seen.add(key)
                res.append(c)
    return res


def extract_cookies_from_cdp(cdp, log=None):
    """从 CDP 中提取微软生态全量纯净 Cookie。

    优先使用 Storage.getCookies 获取整个浏览器存储分区的 Cookie，并通过微软生态域名白名单过滤；
    若 Storage 域不可用，降级使用 Network.getCookies 传入 11+ 个核心服务 URL。
    """
    cookies = []
    try:
        res = cdp.call('Storage.getCookies')
        raw_cookies = res.get('cookies', [])
        if raw_cookies:
            cookies = filter_microsoft_cookies(raw_cookies)
    except Exception as ex:
        if log:
            log(f'Storage.getCookies 提取异常，尝试降级: {ex}')

    if not cookies:
        try:
            res = cdp.call('Network.getCookies', {'urls': FALLBACK_MS_URLS})
            cookies = filter_microsoft_cookies(res.get('cookies', []))
        except Exception as ex:
            if log:
                log(f'Network.getCookies 提取异常: {ex}')

    return cookies


def save_cookies_safely(name, cookies, config=None, log=print):
    """安全持久化 Cookie：先备份为 cookie.json.bak，再原子写入 cookie.json 并设 0600 权限。"""
    if config is None:
        from msr import config as cfg_mod
        config = cfg_mod
    import shutil
    dest = config.pfile(name, 'cookie.json')
    bak = config.pfile(name, 'cookie.json.bak')
    if os.path.isfile(dest):
        try:
            shutil.copy2(dest, bak)
        except Exception as e:
            if log:
                log(f'备份 cookie.json.bak 异常: {e}')
    config.save_json(dest, cookies)
    try:
        os.chmod(dest, 0o600)
    except Exception:
        pass
    if log:
        log(f'最新会话 Cookie 已安全回写 ({len(cookies)} 枚，已备份并设 0600 权限)')
    return True


def import_file(name, file_path):
    """从本地文件导入 Cookie 到指定账号 profile。"""
    from msr import config
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f'Cookie 文件不存在: {file_path}')
    
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        raw = f.read()
    
    cookies = import_content(raw)
    if not cookies:
        raise ValueError(f'无法解析 Cookie 文件内容，支持 JSON 数组或 raw 字符串: {file_path}')
    
    diag = validate_cookies(cookies)
    save_cookies_safely(name, cookies, config=config, log=lambda m: config.log(name, m))
    config.log(name, f'Cookie 导入成功: 共 {len(cookies)} 条 (含 _U={diag["has_auth_u"]})')
    return diag
