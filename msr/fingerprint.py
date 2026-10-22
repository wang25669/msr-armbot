"""账号级浏览器硬件指纹与反检测伪装系统。

核心设计准则:
1. 账号间强隔离: 严禁所有账号共享同一套硬件特征。每个账号拥有独一无二的操作系统、显卡(WebGL)、核心数、内存、分辨率与语言时区。
2. 跨会话一致性: 单账号的硬件指纹在多次运行间保持稳定 (持久化于 profile(name)/fingerprint.json)，避免因单日内频繁变更加剧风控。
3. 真实物理对齐: 时区与语言自动匹配账号所在区域 (如 SG 账号对应 Asia/Singapore 及 zh-SG/en-US)，消除代理出口与浏览器本地时区的冲突。
"""
import hashlib
import json
import os
import random

OS_PRESETS = [
    {
        "os_name": "Windows 11",
        "platform": "Win32",
        "platform_version": "15.0.0",
        "ua_os": "Windows NT 10.0; Win64; x64"
    },
    {
        "os_name": "Windows 10",
        "platform": "Win32",
        "platform_version": "10.0.0",
        "ua_os": "Windows NT 10.0; Win64; x64"
    }
]

GPU_PRESETS = [
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0)"},
    {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0)"},
    {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0)"},
    {"vendor": "Google Inc. (AMD)", "renderer": "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0)"},
    {"vendor": "Google Inc. (AMD)", "renderer": "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0)"}
]

SCREEN_PRESETS = [
    (1920, 1080),
    (1600, 900),
    (1536, 864),
    (1440, 900),
    (1366, 768)
]

HARDWARE_PRESETS = [
    {"cores": 8, "memory": 16},
    {"cores": 8, "memory": 8},
    {"cores": 12, "memory": 16},
    {"cores": 16, "memory": 32},
    {"cores": 6, "memory": 16},
    {"cores": 4, "memory": 8}
]

TIMEZONE_MAP = {
    "SG": ("Asia/Singapore", "zh-SG,zh;q=0.9,en-US;q=0.8,en;q=0.7"),
    "CN": ("Asia/Shanghai", "zh-CN,zh;q=0.9,en;q=0.8"),
    "US": ("America/New_York", "en-US,en;q=0.9"),
    "GB": ("Europe/London", "en-GB,en;q=0.9,en-US;q=0.8"),
    "JP": ("Asia/Tokyo", "ja-JP,ja;q=0.9,en;q=0.8"),
    "DE": ("Europe/Berlin", "de-DE,de;q=0.9,en;q=0.8"),
    "HK": ("Asia/Hong_Kong", "zh-HK,zh;q=0.9,en;q=0.8"),
    "TW": ("Asia/Taipei", "zh-TW,zh;q=0.9,en;q=0.8"),
    "AU": ("Australia/Sydney", "en-AU,en;q=0.9,en-US;q=0.8"),
    "CA": ("America/Toronto", "en-CA,en;q=0.9,en-US;q=0.8"),
    "FR": ("Europe/Paris", "fr-FR,fr;q=0.9,en;q=0.8")
}


def _deterministic_pick(seed_str, items):
    h = int(hashlib.md5(seed_str.encode('utf-8')).hexdigest(), 16)
    return items[h % len(items)]


def generate_fingerprint(name, country='SG', randomize=False):
    """为指定账号生成一套逼真的硬件指纹，支持确定性派生或随机生成。"""
    c = (country or 'SG').upper()
    tz, loc = TIMEZONE_MAP.get(c, ("Asia/Shanghai", "zh-CN,zh;q=0.9,en;q=0.8"))

    if randomize:
        os_item = random.choice(OS_PRESETS)
        gpu = random.choice(GPU_PRESETS)
        res = random.choice(SCREEN_PRESETS)
        hw = random.choice(HARDWARE_PRESETS)
        edge_sub = random.randint(3100, 3350)
        edge_build = random.randint(40, 95)
    else:
        seed = name or 'default'
        os_item = _deterministic_pick(f'{seed}_os', OS_PRESETS)
        gpu = _deterministic_pick(f'{seed}_gpu', GPU_PRESETS)
        res = _deterministic_pick(f'{seed}_res', SCREEN_PRESETS)
        hw = _deterministic_pick(f'{seed}_hw', HARDWARE_PRESETS)
        h = int(hashlib.md5(f'{seed}_ver'.encode('utf-8')).hexdigest(), 16)
        edge_sub = 3100 + (h % 250)
        edge_build = 40 + ((h >> 8) % 55)

    edge_major = "141"
    full_ver = f"{edge_major}.0.{edge_sub}.{edge_build}"
    ua = (f"Mozilla/5.0 ({os_item['ua_os']}) AppleWebKit/537.36 (KHTML, like Gecko) "
          f"Chrome/{edge_major}.0.0.0 Safari/537.36 Edg/{edge_major}.0.0.0 MSRewards/Desktop/1.9.0")

    return {
        "name": name,
        "os_name": os_item["os_name"],
        "platform": os_item["platform"],
        "platform_version": os_item["platform_version"],
        "edge_version": edge_major,
        "full_version": full_ver,
        "user_agent": ua,
        "hardware_concurrency": hw["cores"],
        "device_memory": hw["memory"],
        "screen_width": res[0],
        "screen_height": res[1],
        "webgl_vendor": gpu["vendor"],
        "webgl_renderer": gpu["renderer"],
        "timezone": tz,
        "locale": loc,
        "country": c
    }


def get_fingerprint_file(name):
    from msr import config
    return config.pfile(name, 'fingerprint.json')


def load_or_create(name, country=None, proxy=None):
    """读取账号的专属指纹文件；若不存在则自动生成并持久化。
    优先根据代理出口真实的物理地理位置派生匹配的时区与语言；若无代理则根据账号所属区域匹配。
    """
    from msr import config, geo
    f = get_fingerprint_file(name)
    if os.path.isfile(f):
        data = config.load_json(f)
        if data and data.get('webgl_renderer') and data.get('hardware_concurrency'):
            return data

    acc_cfg = config.get_account(name) or {}
    px = proxy if proxy is not None else acc_cfg.get('proxy')

    detected_country = None
    detected_tz = None
    if px:
        try:
            p_geo = geo.get_cached_proxy_geo(name, px)
            if p_geo.get('status') == 'ok' and p_geo.get('country'):
                detected_country = p_geo.get('country')
                detected_tz = p_geo.get('timezone')
        except Exception:
            pass

    rg = config.load_json(config.pfile(name, 'region.json'), {}) or {}
    c = detected_country or country or rg.get('country') or 'SG'
    fp = generate_fingerprint(name, country=c, randomize=False)
    if detected_tz:
        fp['timezone'] = detected_tz
    save_fingerprint(name, fp)
    return fp


def align_with_proxy(name, proxy=None):
    """强制根据代理出口重新对齐账号指纹的时区、语言与国家，返回更新后的指纹。"""
    from msr import geo
    p_geo = geo.probe_proxy_geo(proxy)
    if p_geo.get('status') != 'ok':
        raise RuntimeError(p_geo.get('message', '代理不可达'))

    c = p_geo.get('country', 'CN')
    tz = p_geo.get('timezone', 'Asia/Shanghai')
    fp = load_or_create(name, country=c)
    fp['country'] = c
    fp['timezone'] = tz
    if c in TIMEZONE_MAP:
        fp['locale'] = TIMEZONE_MAP[c][1]
    save_fingerprint(name, fp)
    return fp, p_geo


def save_fingerprint(name, fp):
    """保存或更新账号指纹。"""
    from msr import config
    f = get_fingerprint_file(name)
    config.save_json(f, fp)


def apply_to_cdp(cdp, fp, log=print):
    """在现有 CDP 会话中注入专属硬件伪装、覆盖 User-Agent、时区与语言。"""
    version = fp.get('edge_version', '141')
    fullver_str = fp.get('full_version', f'{version}.0.4283.60')

    brands = [
        {'brand': 'Microsoft Edge WebView2', 'version': version},
        {'brand': 'Microsoft Edge', 'version': version},
        {'brand': 'Chromium', 'version': version},
        {'brand': 'Not(A:Brand', 'version': '24'}
    ]
    fullver = [
        {'brand': 'Microsoft Edge WebView2', 'version': fullver_str},
        {'brand': 'Microsoft Edge', 'version': fullver_str},
        {'brand': 'Chromium', 'version': f'{version}.0.8059.80'},
        {'brand': 'Not(A:Brand', 'version': '24.0.0.0'}
    ]

    # 1. 设置时区与地理区域，确保与出口代理/账号归属一致
    tz = fp.get('timezone', 'Asia/Singapore')
    loc = fp.get('locale', 'zh-CN')
    try:
        cdp.call('Emulation.setTimezoneOverride', {'timezoneId': tz})
    except Exception as ex:
        log(f'时区覆盖容忍 ({tz}): {ex}')
    try:
        cdp.call('Emulation.setLocaleOverride', {'locale': loc.split(',')[0]})
    except Exception as ex:
        log(f'语言覆盖容忍 ({loc}): {ex}')

    # 2. 设置 User-Agent 与 Client Hints 高熵元数据
    ua = fp.get('user_agent')
    try:
        cdp.call('Emulation.setUserAgentOverride', {
            'userAgent': ua,
            'acceptLanguage': loc,
            'userAgentMetadata': {
                'brands': brands,
                'fullVersion': fullver_str,
                'platform': 'Windows',
                'platformVersion': fp.get('platform_version', '15.0.0'),
                'architecture': 'x86',
                'model': '',
                'mobile': False
            }
        })
    except Exception as ex:
        log(f'UA覆盖容忍: {ex}')

    # 3. 构造注入脚本：硬件核心数、内存、WebGL 显卡渲染器与 Client Hints 深度拦截
    js_patch = f"""(() => {{
      // 1. 高熵 userAgentData 深度拦截
      try {{
        if (navigator.userAgentData) {{
          const P = Object.getPrototypeOf(navigator.userAgentData);
          const orig = P.getHighEntropyValues;
          Object.defineProperty(P, "getHighEntropyValues", {{
            value: function(h) {{
              return orig.call(this, h).then(r => {{
                r.fullVersionList = {json.dumps(fullver)};
                r.platform = "Windows";
                r.platformVersion = "{fp.get('platform_version', '15.0.0')}";
                r.architecture = "x86";
                r.model = "";
                r.bitness = "64";
                return r;
              }});
            }},
            configurable: true,
            writable: true
          }});
        }}
      }} catch(e) {{}}

      // 2. 覆盖 navigator.platform
      try {{
        Object.defineProperty(navigator, 'platform', {{
          get: () => "{fp.get('platform', 'Win32')}",
          configurable: true
        }});
      }} catch(e) {{}}

      // 3. 消除 webdriver 自动化标记
      try {{
        Object.defineProperty(navigator, 'webdriver', {{
          get: () => false,
          configurable: true
        }});
      }} catch(e) {{}}

      // 4. 专属硬件核心数与内存
      try {{
        Object.defineProperty(navigator, 'hardwareConcurrency', {{
          get: () => {fp.get('hardware_concurrency', 8)},
          configurable: true
        }});
        Object.defineProperty(navigator, 'deviceMemory', {{
          get: () => {fp.get('device_memory', 8)},
          configurable: true
        }});
      }} catch(e) {{}}

      // 5. 抹平 WebGL 渲染器指纹 (消除 llvmpipe / SwiftShader 虚拟化特征)
      try {{
        const patchWebGL = (proto) => {{
          if (!proto) return;
          const orig = proto.getParameter;
          proto.getParameter = function(param) {{
            if (param === 37445) return "{fp.get('webgl_vendor', 'Google Inc. (NVIDIA)')}";
            if (param === 37446) return "{fp.get('webgl_renderer', 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)')}";
            return orig.apply(this, arguments);
          }};
        }};
        if (window.WebGLRenderingContext) patchWebGL(WebGLRenderingContext.prototype);
        if (window.WebGL2RenderingContext) patchWebGL(WebGL2RenderingContext.prototype);
      }} catch(e) {{}}
    }})();"""

    try:
        cdp.call('Page.addScriptToEvaluateOnNewDocument', {'source': js_patch})
    except Exception as ex:
        log(f'反检测注入容忍: {ex}')

    try:
        cdp.call('Network.enable')
        cdp.call('Network.setExtraHTTPHeaders', {'headers': {
            'Accept-Language': loc
        }})
    except Exception as ex:
        log(f'专属语言头容忍: {ex}')
