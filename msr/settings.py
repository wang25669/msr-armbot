"""集中参数配置管理系统。

将搜索节奏、卡片停留、心跳周期、调度间隔、防检测指纹及推送通知的所有参数全部提取为声明式配置。
存储于 $MSR_HOME/settings.json，支持默认值继承、深度合并与动态热修改。
"""
import copy
import json
import os

DEFAULT_SETTINGS = {
    # 1. 搜索任务节奏参数 (对齐实测 2 小时风控冷却周期)
    "search": {
        "dwell_min_seconds": 180,       # 每次搜索后随机停留最小秒数 (默认3分钟防风控冷板)
        "dwell_max_seconds": 300,       # 每次搜索后随机停留最大秒数 (默认5分钟)
        "stall_max_retries": 12,        # 连续搜索未推进分数时允许的最大尝试次数
        "hard_cap_attempts": 60,        # 单日搜索最大尝试次数绝对熔断上限
        "budget_minutes": 120,          # 搜索阶段总超时预算 (分钟)
        "click_result_probability": 0.85, # 搜索后随机点击结果链接模拟真实访问的概率
        "query_language": "auto",       # 搜索词语言: auto | zh | en
    },

    # 2. 卡片与打卡任务节奏参数 (Daily Set / More Activities / Quests)
    "cards": {
        "attribution_wait_min": 60,     # 卡片点击后停留最小秒数 (等待归因落库)
        "attribution_wait_max": 120,    # 卡片点击后停留最大秒数
        "budget_minutes": 35,           # 卡片阶段总超时预算 (分钟)
        "quiz_step_delay_min": 3,       # 自动答题每题间隔最小秒数
        "quiz_step_delay_max": 6,       # 自动答题每题间隔最大秒数
    },

    # 3. 移动端与 Edge 浏览 API 参数
    "mobile": {
        "read_article_count": 10,       # 移动端资讯阅读文章目标篇数
        "read_interval_min": 60,        # 每篇阅读最小间隔秒数
        "read_interval_max": 120,       # 每篇阅读最大间隔秒数
        "edge30m_reports": 6,           # Edge 30分钟打卡上报次数 (每次约5分钟)
        "edge30m_interval": 305,        # 两次心跳上报间隔秒数
    },

    # 4. 多账号编排与调度参数 (各账号独立配置专属每日定时，此处管理全局调度与并发安全)
    "schedule": {
        "daily_enabled": True,          # 每日自动循环定时总开关
        "max_concurrency": 2,           # 最大任务并发上限 (同时拉起运行的最大浏览器实例数，默认 2，防爆内存)
        "inter_stage_wait_min": 3,      # 阶段间随机过渡等待最小时间 (分钟)
        "inter_stage_wait_max": 5,      # 阶段间随机过渡等待最大时间 (分钟)
        "random_start_delay_max": 25,   # 定时任务到达时的随机抖动延迟上限 (分钟，防整点固定特征)
        "retry_wait_minutes": 4,        # 失败任务自动修复重试前等待时间 (分钟)
        "fast_mode": False,             # 极速测试模式 (开启后所有等待压缩至 3-6 秒)
    },

    # 5. 浏览器与反指纹参数
    "browser": {
        "binary": None,                 # 指定 Chromium/Chrome 路径 (null 为自动探测)
        "version": "141",               # 伪装 Edge 主版本号
        "headless": True,               # 是否使用无头模式
        "spoof_platform": "Win32",      # 强制覆盖 navigator.platform
        "hardware_concurrency": 8,      # 伪装 CPU 核心数
        "device_memory": 8,             # 伪装内存容量 (GB)
        "disable_webrtc_leak": True,    # 开启 WebRTC UDP 防代理泄漏策略
        "window_width": 1280,
        "window_height": 900
    },

    # 6. 通知渠道 (Server酱 / Telegram / 自定义 Webhook)
    "notify": {
        "channels": ["stdout"],         # 可组合: stdout | serverchan | telegram | webhook
        "on": ["error", "summary"],     # 触发条件: error (失败) | summary (每日汇总) | both
        "serverchan": {
            "sendkey": "",              # Server酱 SendKey (支持 Turbo 版 SCTxxx 或旧版 SCUxxx)
            "url": "https://sctapi.ftqq.com/{sendkey}.send",
            "channel": ""               # 可选推送通道 (如 9 为钉钉机器人, 18 为企业微信等)
        },
        "telegram": {
            "bot_token": "",
            "chat_id": ""
        },
        "webhook": {
            "url": "",
            "method": "POST"
        }
    }
}


def _deep_merge(base, override):
    """递归合并字典配置。"""
    res = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if k in res and isinstance(res[k], dict) and isinstance(v, dict):
            res[k] = _deep_merge(res[k], v)
        else:
            res[k] = copy.deepcopy(v)
    return res


def get_settings_file(home=None):
    from msr import config
    h = home or config.HOME
    return os.path.join(h, 'settings.json')


def load_settings(home=None):
    """加载配置：以 DEFAULT_SETTINGS 为底本，合并用户 settings.json。"""
    path = get_settings_file(home)
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                user_cfg = json.load(f)
            return _deep_merge(DEFAULT_SETTINGS, user_cfg)
        except Exception:
            pass
    return copy.deepcopy(DEFAULT_SETTINGS)


def save_settings(settings, home=None):
    """保存配置到 settings.json。"""
    path = get_settings_file(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)


def get(path_key, default=None, home=None):
    """通过类似 'search.dwell_min_seconds' 读取配置项。"""
    cfg = load_settings(home)
    keys = path_key.split('.')
    cur = cfg
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur


def set_val(path_key, value, home=None):
    """通过类似 'search.dwell_min_seconds' 更新并保存配置项。"""
    cfg = load_settings(home)
    keys = path_key.split('.')
    cur = cfg
    for k in keys[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    # 尝试转换基础类型
    if isinstance(value, str):
        if value.lower() == 'true':
            value = True
        elif value.lower() == 'false':
            value = False
        elif value.isdigit():
            value = int(value)
        else:
            try:
                value = float(value)
            except ValueError:
                pass
    cur[keys[-1]] = value
    save_settings(cfg, home)
    return cfg
