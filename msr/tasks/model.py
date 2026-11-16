"""任务模型：新引擎的唯一真相对象。

设计约束（见 docs/task-engine-redesign.md §3.2）：
- 判定只认三种来源：/dapi/me 字段、DOM 容器 id + x/y 数字、URL 结构；
- label 只用于日志/通知，禁止参与判定；
- 每个 Task 必须自带 verify，执行后回读不成立就不许报完成。
"""

# ---- DOM 稳定锚点（跨账号/跨语言稳定，实测见文档 §1.2）----
CONTAINERS = {
    'dailyset': '#dailyset',
    'streaks': '#streaks',
    'quests': '#quests',
    'moreactivities': '#moreactivities',
    'explore': '#exploreonbing',
    'levelup': '#levelup',
}

# 已完成卡片的对号徽章（Fluent token，中英文页面一致）
# 实测：某SG号2 3/3 三枚，某US号 0/3 零枚
BADGE_CLASS = 'bg-statusSuccessRewardsBg'

# 五种连续打卡槽位在 #streaks 内的固定顺序（文案随语言变，序号不变）
STREAK_SLOTS = ['search', 'dailyset', 'edge30m', 'appcheckin', 'visual']

# kind 取值：见文档 §3.3
KINDS = ('search', 'url_open', 'quiz', 'punchcard', 'streak', 'claim', 'api_attempt')


def make_task(tid, kind, source, label='', points=0, progress=0, max_=0,
              complete=False, dest=None, verify=None, needs='browser',
              deadline=None, discovered='api', extra=None):
    """构造一条 Task。字段固定，避免各 handler 各自加字段造成漂移。"""
    assert kind in KINDS, '未知 kind: %s' % kind
    t = {
        'id': tid,
        'kind': kind,
        'source': source,           # 'api' | 'dom' | 'static'
        'label': label,             # 仅日志
        'points': points,
        'progress': progress,
        'max': max_,
        'complete': bool(complete),
        'dest': dest,
        'verify': verify or {},
        'needs': needs,             # 'browser' | 'api'
        'deadline': deadline,
        'discovered': discovered,   # 发现方式，排查用
    }
    if extra:
        t.update(extra)
    return t


def is_done(task):
    """统一的完成判定：徽章（dom_badge）或 x/y 达标或 API complete。"""
    if task.get('complete'):
        return True
    v = task.get('verify') or {}
    if v.get('dom_badge') and task.get('badge'):
        return True
    mx = task.get('max') or 0
    if mx > 0 and (task.get('progress') or 0) >= mx:
        return True
    return False


def task_line(t):
    """单行可读输出，供 dry-run 打印。"""
    prog = ''
    if (t.get('max') or 0) > 0:
        prog = '%s/%s' % (t.get('progress', 0), t.get('max'))
    return '%-28s kind=%-11s %-9s %-22s %s' % (
        t['id'][:28], t['kind'], prog or '-', (t.get('dest') or '-')[:22],
        'DONE' if is_done(t) else 'todo')
