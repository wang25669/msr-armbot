"""归一化层：RawTask（API/DOM 原始数据）→ 统一 Task 列表。

原则：
- 只按结构判 kind（容器 id / href / x-y 数字 / API type），不看文案；
- 同一个任务被多个来源发现时合并（API 优先，DOM 补进度/证据）；
- 识别不出来的东西进 unknowns，禁止静默丢弃（P3 的核心价值就在这份清单）。
"""
import re

from msr.tasks.model import make_task, STREAK_SLOTS, is_done

INTERNAL_HOSTS = ('rewards.bing.com', 'bing.com', 'microsoft.com')


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


PROG_RE = re.compile(r'(\d+)\s*/\s*(\d+)\s*(?:个任务|个活動|tasks|days|天)', re.I)
PROG_RE_PLAIN = re.compile(r'(\d+)\s*/\s*(\d+)')


def prog_from_text(text):
    """从卡片文本里取 x/y。PREF: 紧跟 个任务/tasks/days 的那组, 否则第一组 x/y。

    2026-11-11 实测(某新号 /earn 7 张打卡卡): 每张卡锚点文本都带 `x/y 个任务`,
    包括 WSB(4/4)、BingMonthly(2/5)、SeaOfThieves(1/4)。所以"进度不在文本里"的旧结论不成立,
    解析要优先咬住 `x/y 个任务|tasks|days`, 避免被描述里的其他数字(如 +1,320)带偏。
    """
    t = text or ''
    m = PROG_RE.search(t) or PROG_RE_PLAIN.search(t)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _prog(item):
    p = item.get('prog')
    if isinstance(p, (list, tuple)) and len(p) == 2:
        return int(p[0]), int(p[1])
    return prog_from_text(item.get('text') or '')


def _kind_by_href(href, default='quiz'):
    if not href:
        return default
    return 'url_open' if href.startswith('http') else default


def normalize(api_raw, dom_raw, account=''):
    """合并成 Task 列表，返回 (tasks, unknowns, notes)。"""
    tasks = []
    unknowns = []
    notes = []

    # ---------- 1) API 侧原样转 Task ----------
    for r in api_raw or []:
        # streak 类: 2026-11-08 实测更正 —— API 的 attributes.complete 是"当日任务是否达成"的权威信号,
        # 而 progress/max 是连续打卡窗口内的累计进度 (如 Edge 20/120, 120 = 4 天 x 30 分钟)。
        # 所以 complete = API complete OR progress>=max, 两个字段都保留, 别再单看 x/y 判当日完成。
        if r.get('kind') == 'streak' and _int(r.get('max')) > 0:
            api_complete = bool(r.get('complete'))
            progress_complete = _int(r.get('progress')) >= _int(r.get('max'))
            r = dict(r, complete=(api_complete or progress_complete))
            r['extra_flags'] = {'api_complete': api_complete,
                                'progress_complete': progress_complete}
        t = make_task(
            tid=r['id'], kind=r['kind'], source=r['source'], label=r.get('label', ''),
            points=r.get('points', 0), progress=r.get('progress', 0), max_=r.get('max', 0),
            complete=r.get('complete', False), dest=r.get('dest'),
            verify=r.get('verify') or {}, needs=r.get('needs', 'browser'),
            discovered=r.get('discovered', 'api'),
            extra={k: r[k] for k in ('slot', 'activated', 'needs_activation', 'multi_day', 'days',
                                     'extra_flags')
                   if k in r},
        )
        tasks.append(t)

    if not dom_raw:
        notes.append('DOM 未采集（浏览器失败或锁占用），仅 API 任务')
        return tasks, unknowns, notes

    for key in ('dailyset', 'streaks', 'quests', 'moreactivities', 'explore'):
        c = dom_raw.get(key) or {}
        if not c.get('present'):
            notes.append('容器缺失: %s' % key)

    # ---------- 2) dailyset：卡片数 3(老号) / 1(新号聚合卡) ----------
    ds = dom_raw.get('dailyset') or {}
    for it in ds.get('items') or []:
        prog, mx = _prog(it)
        href = it.get('href') or ''
        txt = it.get('text') or ''
        # 新号聚合卡：有 x/y、无锚点、卡片是"完成每日任务集"这种
        if not href and mx > 0:
            tasks.append(make_task(
                tid='dailyset:aggregate', kind='punchcard', source='dom',
                label=(txt[:40] or '每日任务集'), progress=prog, max_=mx,
                complete=bool(it.get('badge')), dest=None,
                verify={'dom_badge': True}, needs='browser', discovered='dom:dailyset',
                extra={'badge': it.get('badge')},
            ))
            continue
        kind = 'quiz' if 'quiz' in href.lower() else _kind_by_href(href, default='quiz')
        tasks.append(make_task(
            tid='dailyset:%d' % (it.get('idx', 0) + 1), kind=kind, source='dom',
            label=(txt[:40] or '每日活动'), progress=prog, max_=mx,
            complete=bool(it.get('badge')), dest=href or None,
            verify={'dom_badge': True}, needs='browser', discovered='dom:dailyset',
            extra={'badge': it.get('badge')},
        ))

    # ---------- 3) streaks 五槽位：按 DOM 顺序映射，文案只进 label ----------
    slots = dom_raw.get('streaks_slots') or []
    for i, s in enumerate(slots):
        prog, mx = _prog({'prog': s.get('prog')})
        slot = STREAK_SLOTS[i] if i < len(STREAK_SLOTS) else 'slot%d' % i
        tid = 'streak:%s' % slot
        if any(t['id'] == tid for t in tasks):
            # API 已有（例如 edge30m），DOM 只补进度
            for t in tasks:
                if t['id'] == tid:
                    if not t.get('max'):
                        t['progress'], t['max'] = prog, mx
                    t['badge'] = bool(s.get('badge'))
            continue
        needs = 'api' if slot in ('appcheckin', 'edge30m') else 'browser'
        tasks.append(make_task(
            tid=tid, kind='streak', source='dom', label=s.get('label', ''),
            progress=prog, max_=mx, complete=bool(s.get('badge')),
            dest=None, verify={'dom_badge': True}, needs=needs,
            discovered='dom:streaks', extra={'slot': slot, 'badge': s.get('badge')},
        ))
    if not slots:
        notes.append('#streaks 无可解析槽位（可能未渲染或语言变体）')

    # ---------- 4) quests：quest_<promoName> ----------
    seen_q = set()
    for q in dom_raw.get('quest_cards') or []:
        qid = q.get('id') or ''
        if not qid or qid in seen_q:
            continue
        seen_q.add(qid)
        prog, mx = _prog(q)
        if mx <= 0:
            # 2026-11-11 某新号 实测: #quests/更多活动 区域还有"推广位"锚点
            # (href 形如 .../punchcard?form=ML2Y1R, 文本是商品描述, 没有 x/y)。
            # 它不是进度卡, 记进 notes 而不是伪装成 0/0 的任务, 避免 dry-run 里出现 '-'。
            notes.append('quests 区域非进度卡(无 x/y, 视为推广/兑换位): %s'
                         % (q.get('text') or '')[:40])
            continue
        tasks.append(make_task(
            tid='quest:%s' % qid, kind='punchcard', source='dom',
            label=(q.get('text') or '')[:40], progress=prog, max_=mx,
            complete=bool(q.get('badge')), dest=q.get('href') or None,
            verify={'dom_badge': True}, needs='browser', discovered='dom:quests',
            extra={'badge': q.get('badge')},
        ))
    qbox = dom_raw.get('quests') or {}
    if qbox.get('present') and not (dom_raw.get('quest_cards') or []):
        notes.append('#quests 容器在但未解析出 quest_* 卡片（形状待确认）')

    # ---------- 5) moreactivities / explore：一卡一个 url_open ----------
    for key, tid_prefix in (('moreactivities', 'more'), ('explore', 'explore')):
        box = dom_raw.get(key) or {}
        for it in box.get('items') or []:
            prog, mx = _prog(it)
            href = it.get('href') or ''
            text = (it.get('text') or '').strip()
            if not text and not href:
                unknowns.append({'container': key, 'idx': it.get('idx'),
                                 'why': 'empty_card', 'text': ''})
                continue
            # 实测：这些卡片是 React 点击卡（无 <a href>），点开后才知道去向
            tasks.append(make_task(
                tid='%s:%d' % (tid_prefix, (it.get('idx') or 0) + 1), kind='url_open',
                source='dom', label=text[:40], progress=prog, max_=mx,
                complete=bool(it.get('badge')), dest=href or None,
                verify={'dom_badge': True}, needs='browser', discovered='dom:%s' % key,
                extra={'badge': it.get('badge'), 'click_only': not bool(href),
                       'requires_own_query': (key == 'explore')},
            ))

    # ---------- 6) 识别不出来的容器 ----------
    # levelup 是多日进度追踪，不是当日任务 -> 只作信息记录
    lv = dom_raw.get('levelup_items') or []
    if lv:
        notes.append('levelup(信息): %s' % ' | '.join(lv[:3]))

    for key in ('dailyset', 'streaks', 'quests', 'moreactivities', 'explore'):
        c = dom_raw.get(key) or {}
        if c.get('present') and c.get('count') and not any(
                t['discovered'].endswith(key) for t in tasks):
            unknowns.append({
                'container': key, 'count': c.get('count'),
                'why': 'container_present_but_unclassified',
                'sample': ((c.get('items') or [{}])[0].get('text') or '')[:80],
            })

    if dom_raw.get('needs_expand'):
        notes.append('#quests 需要展开（dashboard/earn 都未渲染），本轮标记 needs_expand')

    return _dedupe(tasks), unknowns, notes


def _dedupe(tasks):
    """同一 id 保留 API 版本，DOM 只补 badge/progress。"""
    out = {}
    for t in tasks:
        cur = out.get(t['id'])
        if cur is None:
            out[t['id']] = t
            continue
        if cur['source'] != 'api' and t['source'] == 'api':
            t['badge'] = cur.get('badge')
            out[t['id']] = t
        else:
            if not cur.get('max') and t.get('max'):
                cur['progress'], cur['max'] = t['progress'], t['max']
            cur['badge'] = cur.get('badge') or t.get('badge')
    return list(out.values())


def summarize(tasks, unknowns):
    by_kind = {}
    for t in tasks:
        by_kind[t['kind']] = by_kind.get(t['kind'], 0) + 1
    return {
        'total': len(tasks),
        'done': sum(1 for t in tasks if is_done(t)),
        'todo': sum(1 for t in tasks if not is_done(t)),
        'by_kind': by_kind,
        'need_activation': [t['id'] for t in tasks if t.get('needs_activation')],
        'unknown': len(unknowns),
    }
