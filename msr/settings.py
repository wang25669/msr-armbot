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
        "read_interval_min": 60,        # 每篇阅读最小间隔秒数
        "read_interval_max": 120,       # 每篇阅读最大间隔秒数
        "edge30m_interval": 305,        # 两次心跳上报间隔秒数
    },

    # 4. 多账号编排与调度参数 (各账号独立配置专属每日定时，此处管理全局调度与并发安全)
    "schedule": {
        "daily_enabled": True,          # 每日自动循环定时总开关
        "max_concurrency": 2,           # 最大任务并发上限 (同时拉起运行的最大浏览器实例数，默认 2，防爆内存)
        # 阶段间过渡等待已硬编码为随机 30~60 秒(2026-11-11 取消可调参数)
        "random_start_delay_max": 25,   # 定时任务到达时的随机抖动延迟上限 (分钟，防整点固定特征)
        "retry_wait_minutes": 4,        # 失败任务自动修复重试前等待时间 (分钟)
        "catchup_grace_minutes": 45,    # 漏跑自愈: 定时点过后多少分钟仍无今日成功记录就补发 (容器重启/槽位冲突兜底)
        "catchup_latest_hour": 23,      # 漏跑自愈的最晚小时 (到点后仍可补发, 超过该小时不再补, 避免撞账号本地日边界)
    },

    # 5. 浏览器与反指纹参数
    # 注: spoof_platform / window_width / window_height 已于 P1 删除 —— 三个参数历史上有声明但无任何读取点,
    #     窗口尺寸与 platform 实际取账号指纹 (fingerprint.json: screen_width/height、platform), 见设计文档 §7.1。
    "browser": {
        "binary": None,                 # 指定 Chromium/Chrome 路径 (null 为自动探测)
        "version": "141",               # 伪装 Edge 主版本号 (账号指纹页 fp-edge-ver 可覆盖)
        "headless": True,               # 是否使用无头模式
        "hardware_concurrency": 8,      # 伪装 CPU 核心数
        "device_memory": 8,             # 伪装内存容量 (GB)
        "disable_webrtc_leak": True,    # 开启 WebRTC UDP 防代理泄漏策略
    },

    # 7. 新引擎任务参数 (P1 新增, 每组均有真实读取点, 见 tasks_browser/tasks_visual)
    #    默认值刻意与"当前现网实测可用的行为"对齐, 参数生效不改变既有节奏 (设计文档 §7.4 的建议值是
    #    新引擎目标值, 收紧需先有真机验证, 不在 P1 一次性改掉已跑通的手法)。
    "quests": {
        "activate_wait_seconds": 15,    # 打开打卡卡后等待步骤组件水合渲染的上限秒数
        "step_dwell_min": 60,           # 单个打卡步骤停留最小秒数 (等待服务端归因)
        "step_dwell_max": 120,          # 单个打卡步骤停留最大秒数
    },
    "explore": {
        "dwell_min": 16,                # 探索搜索结果页停留最小秒数
        "dwell_max": 20,                # 探索搜索结果页停留最大秒数
        "retry_dwell_seconds": 30,      # 首搜未计分时, 用卡片标题原词补搜的停留秒数
        "budget_seconds": 1200,         # 单轮 explore 总预算秒数 (懒渲染滚动补拉也在此预算内)
    },
    "streak": {
    },
    "visual": {
        "submit_rounds": 3,             # 提交后回读未完成时最多尝试轮数
        "landing_wait_seconds": 18,     # 落页后基础停留秒数 (实际再加 0~7 秒抖动)
    },
    "engine": {
        # P4(S2) 路由保险丝: legacy = 旧 tasks_browser 路径; new = url_open 合并 handler。
        # 验证通过后一次性全量切 new (不做灰度), 回滚只需切回 legacy。
                                       # legacy 保留给阶段10回补做安全网(route_override), 不再是默认路径
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


# P1: 已删除的死参数 —— 用户 settings.json 里若残留, 加载时剥离, 避免"看着能配其实无效"
LEGACY_KEYS = {
    'browser': ('spoof_platform', 'window_width', 'window_height'),
    # 2026-11-11: 阶段间过渡等待改为代码内固定随机 30~60 秒, 两个可调参数作废
    'schedule': ('inter_stage_wait_min', 'inter_stage_wait_max', 'fast_mode'),
    # 2026-11-11: 以下参数按用户要求从控制台移除, 代码内固化为常量
    'mobile': ('read_article_count', 'edge30m_reports'),
    'quests': ('max_steps_per_run', 'multi_day_guard_hours', 'retry_no_credit_once'),
    'streak': ('activation_required_check', 'activate_timeout_seconds'),
    'engine': ('url_open_route',),
    'explore': ('per_card_query',),
    # schedule.retry_wait_minutes 保留并接上真实读取点 (runner 阶段10回补等待), 不在删除之列
}


def _strip_legacy(cfg):
    for group, keys in LEGACY_KEYS.items():
        node = cfg.get(group)
        if isinstance(node, dict):
            for k in keys:
                node.pop(k, None)
    return cfg


def validate_settings(cfg):
    """保存前校验 (§7.3 契约3: 非法值不落盘)。返回错误描述字符串, 合法返回 None。"""
    def num(group, key):
        try:
            return float(cfg.get(group, {}).get(key))
        except (TypeError, ValueError):
            return None

    pairs = [
        ('search', 'dwell_min_seconds', 'dwell_max_seconds'),
        ('cards', 'attribution_wait_min', 'attribution_wait_max'),
        ('cards', 'quiz_step_delay_min', 'quiz_step_delay_max'),
        ('mobile', 'read_interval_min', 'read_interval_max'),
        ('quests', 'step_dwell_min', 'step_dwell_max'),
        ('explore', 'dwell_min', 'dwell_max'),
    ]
    for group, kmin, kmax in pairs:
        a, b = num(group, kmin), num(group, kmax)
        if a is None or b is None:
            return f'{group}.{kmin}/{kmax} 必须为数字'
        if a < 0 or b < 0:
            return f'{group}.{kmin}/{kmax} 不许为负数'
        if a > b:
            return f'{group}.{kmin}({a:g}) 必须 <= {kmax}({b:g})'

    for group, key in [('search', 'budget_minutes'), ('cards', 'budget_minutes'),
                       ('schedule', 'retry_wait_minutes'),
                       ('quests', 'activate_wait_seconds'),
                       ('explore', 'retry_dwell_seconds'), ('explore', 'budget_seconds'),
                       ('visual', 'submit_rounds'), ('visual', 'landing_wait_seconds')]:
        v = num(group, key)
        if v is None:
            return f'{group}.{key} 必须为数字'
        if v < 0:
            return f'{group}.{key} 不许为负数'
        if v == 0:
            return f'{group}.{key} 必须 > 0'

    if num('visual', 'submit_rounds') < 1:
        return 'visual.submit_rounds 至少为 1'

    prob = num('search', 'click_result_probability')
    if prob is None or not (0.0 <= prob <= 1.0):
        return 'search.click_result_probability 必须在 0~1 之间'

    ver = str(cfg.get('browser', {}).get('version') or '').strip()
    if not ver.isdigit():
        return 'browser.version 必须为数字串 (如 141)'

    mode = cfg.get('search', {}).get('query_language')
    if mode not in ('auto', 'zh', 'en'):
        return 'search.query_language 只能是 auto/zh/en'

    return None


def effective_banner(cfg=None):
    """本次运行生效参数 (设计文档 §7.3 契约5: 每轮把生效参数打到日志头)。"""
    c = cfg or load_settings()
    s, cd, m = c.get('search', {}), c.get('cards', {}), c.get('mobile', {})
    sch, br = c.get('schedule', {}), c.get('browser', {})
    q, e, st, v = c.get('quests', {}), c.get('explore', {}), c.get('streak', {}), c.get('visual', {})
    tag = '真人节奏'
    return [
        f'节奏={tag} 并发={sch.get("max_concurrency")} 回补等待={sch.get("retry_wait_minutes")}min',
        f'search 停留={s.get("dwell_min_seconds")}~{s.get("dwell_max_seconds")}s 熔断={s.get("stall_max_retries")} 硬顶={s.get("hard_cap_attempts")} 预算={s.get("budget_minutes")}min 点击概率={s.get("click_result_probability")} 语言={s.get("query_language")}',
        f'cards 停留={cd.get("attribution_wait_min")}~{cd.get("attribution_wait_max")}s 预算={cd.get("budget_minutes")}min 答题间隔={cd.get("quiz_step_delay_min")}~{cd.get("quiz_step_delay_max")}s',
        f'quests 激活等待={q.get("activate_wait_seconds")}s 步骤停留={q.get("step_dwell_min")}~{q.get("step_dwell_max")}s 跨日保护=20h',
        f'explore 停留={e.get("dwell_min")}~{e.get("dwell_max")}s 预算={e.get("budget_seconds")}s',
        f'visual 轮数={v.get("submit_rounds")} 落页停留={v.get("landing_wait_seconds")}s(+抖动) | mobile Edge={m.get("edge30m_interval")}s/次',
        f'browser headless={br.get("headless")} webrtc防泄漏={br.get("disable_webrtc_leak")} UA版本={br.get("version")}',
        f'固定值: 阅读目标=10篇 单卡步数=8 Edge上报=6次 激活超时=30s(3次轮询) url_open=new',
    ]


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
            return _strip_legacy(_deep_merge(DEFAULT_SETTINGS, user_cfg))
        except Exception:
            pass
    return copy.deepcopy(DEFAULT_SETTINGS)


def save_settings(settings, home=None):
    """保存配置到 settings.json (保存即校验: 非法值抛 ValueError, 不落盘)。"""
    err = validate_settings(settings)
    if err:
        raise ValueError(err)
    _strip_legacy(settings)          # 已作废的参数不落盘
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
