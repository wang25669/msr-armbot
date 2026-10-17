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
    """检查 Cookie 是否包含 Bing/Microsoft Rewards 核心登录态。"""
    if not cookies:
        return {'valid': False, 'reason': 'Cookie 为空', 'count': 0}
    
    names = {c.get('name') for c in cookies}
    has_u = '_U' in names
    has_muid = 'MUID' in names
    has_srch = any('SRCH' in n for n in names)
    
    # _U 是 Bing 识别已登录微软账户的核心票据
    valid = has_u or has_muid
    reason = 'OK' if valid else '缺少关键登录凭据 (_U 或 MUID)'
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
    dest = config.pfile(name, 'cookie.json')
    config.save_json(dest, cookies)
    config.log(name, f'Cookie 导入成功: 共 {len(cookies)} 条 (含 _U={diag["has_auth_u"]})')
    return diag
