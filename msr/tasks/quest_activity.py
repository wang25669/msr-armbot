# -*- coding: utf-8 -*-
"""打卡卡(quest / punchcard)的**步骤级**状态读取 —— 判"这一步今天到底计没计分"。

## 为什么需要

外层卡片的 `x/y tasks` 是"已完成活动数"，服务端刷新滞后很久：
2026-11-12 实测 某新号 —— 12:14 回读 Sea of Thieves 还是 `1/4`，当晚 22:20 才变 `2/4`；
Duolingo 21:01 回读 `1/6`，22:20 已是 `2/6`。阶段末只看外层 `x/y` 判"推进"，
就会把**已经计分**的步骤报成 `进度推进0/K张`（当晚就是这样误报的）。

## 判据：执行前后各取一次卡详情页快照，比对"已完成的步骤数"

每个步骤是容器 `{"className":"flex flex-row gap-2 sm:gap-4","children":[...]}`：

    已完成：左侧徽章 div class 含 `bg-statusSuccessRewardsBg` + `svg viewBox="0 0 7 6"`
            —— **没有 anchor、没有 isCompleted、也没有 linkText**（2026-11-12 实测，
            所以按按钮文案匹配已完成的步骤必然落空）
    未完成：左侧空圈，末尾挂 anchor：`{"isCompleted":false,"isLocked":false,"linkText":"..."}`
    h3 是标题，p 是要求文案（`5/5 days`、`at least 24 hours apart`、`once a day` 在这里）

所以判"这一步今天计没计分"最稳的做法是**比对前后快照的已完成集合**（按标题多重集计），
既不依赖文案匹配，也能正确处理"两张同名步骤"（Sea of Thieves 实测有两条
`Search to complete`：desktop 5/5 与 mobile 0/7）。

一次 HTTPS GET（0.5~2 秒），**不需要浏览器**。cookie 只用 bing.com 域
（整份塞进 Cookie 头会到 42KB，Bing 直接回 HTTP 400）。
"""
import re
import urllib.request
from collections import Counter

from msr import config as _cfg
from msr.tasks.streaks import UA

QUEST_TPL = 'https://rewards.bing.com/earn/quest/%s'
EARN = 'https://rewards.bing.com/earn'

_BLOCK_RE = re.compile(r'\{"className":"flex flex-row gap-2 sm:gap-4","children":\[')
_H3_RE = re.compile(r'"h3",null,\{"className":"[^"]*","children":"((?:[^"\\]|\\.){1,90})"')
_P_RE = re.compile(r'"p",null,\{"className":"[^"]*","children":"((?:[^"\\]|\\.){0,400})"')
_LINK_RE = re.compile(r'"linkText":"((?:[^"\\]|\\.){0,90})"')
_BADGE = 'bg-statusSuccessRewardsBg'


def _cookie_header(name):
    cookies = _cfg.load_json(_cfg.pfile(name, 'cookie.json'), []) or []
    bing = [c for c in cookies if 'bing.com' in str(c.get('domain') or '')]
    return '; '.join('%s=%s' % (c.get('name'), c.get('value'))
                     for c in (bing or cookies) if c.get('name') and c.get('value'))


def parse_activities(html):
    """payload -> [{title, link, desc, completed, locked}]；空/坏输入返回 None。"""
    if not html:
        return None
    h = html.replace('\\"', '"')
    spans = [m.start() for m in _BLOCK_RE.finditer(h)]
    if not spans:
        return []
    out = []
    for i, s in enumerate(spans):
        e = spans[i + 1] if i + 1 < len(spans) else min(len(h), s + 4000)
        blk = h[s:e]
        t = _H3_RE.search(blk)
        if not t:
            continue                      # 没有标题的块不是步骤（页头账号/邮箱之类）
        d = _P_RE.search(blk)
        link = _LINK_RE.search(blk)
        out.append({
            'title': re.sub(r'\s+', ' ', t.group(1)).strip(),
            'link': re.sub(r'\s+', ' ', link.group(1)).strip() if link else None,
            'desc': re.sub(r'\s+', ' ', d.group(1))[:300] if d else '',
            # 完成时没有 isCompleted 字段，只有徽章；未完成时 isCompleted 明确为 false
            'completed': (_BADGE in blk) or bool(re.search(r'"isCompleted":true', blk)),
            'locked': bool(re.search(r'"isLocked":true', blk)),
        })
    return out


def fetch_activities(name, promo_id, proxy=None, timeout=25):
    """GET 卡详情页并解析；网络/解析失败返回 None（调用方按"未知"处理，不擅自判定）。"""
    ck = _cookie_header(name)
    if not ck or not promo_id:
        return None
    req = urllib.request.Request(QUEST_TPL % promo_id, headers={
        'Cookie': ck, 'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9'})
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
    try:
        html = urllib.request.build_opener(*handlers).open(req, timeout=timeout).read() \
            .decode('utf-8', 'ignore')
    except Exception:
        return None
    return parse_activities(html)


def _quest_ids_from_html(html):
    """/earn 页面(payload/HTML) -> 打卡卡 promo id 列表(去重保序)。纯函数, 无 IO。"""
    h = (html or '').replace('\\"', '"').replace('\\/', '/')   # RSC 里斜杠有时被转义成 \/
    ids = []
    for m in re.finditer(r'/earn/quest/([A-Za-z0-9_\-]{6,})', h):
        qid = m.group(1)
        if qid not in ids:
            ids.append(qid)
    return ids


def list_quest_ids(name, proxy=None, timeout=25):
    """GET /earn 里出现的打卡卡 promo id 列表（去重、保序）。

    用途：页面 `#quests` 没渲染出来时做交叉验证 —— 服务端 payload 里有卡就说明"没渲染"，
    不能当成"今天没有打卡任务"（2026-11-16 事故：某 SG 号 11-13 空跑一天，周卡最终 7/8 领不到 100 分）。
    返回 None = 读取失败（未知），[] = 页面确实没有打卡卡。
    """
    ck = _cookie_header(name)
    if not ck:
        return None
    req = urllib.request.Request(EARN, headers={
        'Cookie': ck, 'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9'})
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
    try:
        html = urllib.request.build_opener(*handlers).open(req, timeout=timeout).read() \
            .decode('utf-8', 'ignore')
    except Exception:
        return None
    return _quest_ids_from_html(html)


def snapshot(activities):
    """-> {标题: 已完成条数}。取不到时返回 None。"""
    if activities is None:
        return None
    return dict(Counter(a['title'] for a in activities if a.get('completed')))


def newly_completed(before, after):
    """before -> after 新增完成的步骤数（按标题多重集计）；任一侧缺失返回 None。"""
    if before is None or after is None:
        return None
    added = Counter(after) - Counter(before)
    return sum(added.values())
