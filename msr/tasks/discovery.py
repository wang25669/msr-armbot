"""发现层：从权威数据源读取"今天有哪些任务"（只读，不做任何动作）。

两个来源：
1. discover_api —— GET /dapi/me（Bearer + x-rewards-*），拿上限/额度/倍率/激活信号；
2. discover_dom —— 用账号自己的 profile 起 headless Chromium，只读 DOM 容器。

铁律（文档 §3.2）：判定只看 API 字段、容器 id + x/y 数字、URL 结构；不碰文案。
"""
import json
import os
import time

from msr.tasks.model import CONTAINERS, BADGE_CLASS, STREAK_SLOTS

# 不作为任务下发的促销（纯展示/广告/兑换入口）
IGNORE_PROMO_TYPES = {'edge_flyout_nurf', 'edge_flyout_redeem', 'highvaluesweepstakes'}
IGNORE_PROMO_NAMES = {'user_info', 'waitlist_info', 'Fosbury_info', 'HVA_1M_Sweeps_info',
                      'HVA_1M_Sweeps_info_anon', 'Rewards_EdgeUsage_progress_info'}


def _attrs(promo):
    return promo.get('attributes') or {}


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _bool(v):
    return str(v).strip().lower() in ('true', '1', 'yes')


def discover_api(me, log=None):
    """把 /dapi/me 的 promotions 拆成 RawTask 列表（纯数据，不联网）。"""
    raw = []
    promos = (me or {}).get('promotions') or []
    names = {p.get('name') for p in promos}

    for p in promos:
        name = p.get('name') or ''
        a = _attrs(p)
        ptype = str(a.get('type') or '')
        if ptype in IGNORE_PROMO_TYPES or name in IGNORE_PROMO_NAMES:
            continue

        # 0) "资格/激活"类促销不是任务本身，而是激活闸门（实测 offerid 含 eligibility/activation）
        offerid = str(a.get('offerid') or '')
        if ('eligibility' in offerid.lower() or 'activation' in offerid.lower()) and _int(a.get('max')) <= 1:
            raw.append({
                'id': 'activation:%s' % (offerid or name), 'signal': True,
                'reason': 'eligibility', 'label': a.get('title') or name,
                'offerid': offerid, 'type': ptype,
            })
            continue

        # 1) PC 搜索：上限/进度/倍率全部由服务端下发
        if ptype == 'search' or 'search_PC' in name:
            raw.append({
                'id': 'search:pc', 'source': 'api', 'kind': 'search',
                'label': a.get('title') or 'PC 搜索',
                'progress': _int(a.get('progress')), 'max': _int(a.get('max')),
                'complete': _bool(a.get('complete')), 'dest': a.get('destination') or None,
                'verify': {'api': name}, 'needs': 'browser', 'discovered': 'api:promo',
            })
            continue

        # 2) urlreward 族（Shop & earn 等）
        if ptype == 'urlreward':
            raw.append({
                'id': 'url:%s' % (a.get('offerid') or name),
                'source': 'api', 'kind': 'url_open',
                'label': a.get('title') or name,
                'progress': _int(a.get('progress')), 'max': _int(a.get('max')),
                'complete': _bool(a.get('complete')), 'dest': a.get('destination') or None,
                'verify': {'api': name}, 'needs': 'browser', 'discovered': 'api:promo',
            })
            continue

        # 3) Edge 30 分钟：flight = 已激活；只有 eligible = 未激活，需先激活
        if name == 'edge_browsing_streak_flight':
            raw.append({
                'id': 'streak:edge30m', 'source': 'api', 'kind': 'streak',
                'label': a.get('title') or 'Edge 浏览',
                'progress': _int(a.get('progress')), 'max': _int(a.get('max')),
                'complete': _bool(a.get('complete')), 'dest': None,
                'verify': {'api_flight': name}, 'needs': 'api', 'discovered': 'api:promo',
                'slot': 'edge30m', 'activated': True,
            })
            continue
        if name == 'edge_browsing_streak_eligible' and 'edge_browsing_streak_flight' not in names:
            raw.append({
                'id': 'streak:edge30m', 'source': 'api', 'kind': 'streak',
                'label': a.get('title') or 'Edge 浏览(待激活)',
                'progress': _int(a.get('progress')), 'max': _int(a.get('max')),
                'complete': False, 'dest': None,
                'verify': {'api_flight': 'edge_browsing_streak_flight'},
                'needs': 'browser', 'discovered': 'api:eligible',
                'slot': 'edge30m', 'activated': False, 'needs_activation': 'edge',
            })
            continue

        # 4) 7 天冲刺（hva_dailystreaks_*）：多日任务，一天只推进一步
        if any(k.startswith('hva_dailystreaks_') for k in a):
            for key, dest_key in (('bing', 'hva_dailystreaks_bing_destination'),
                                  ('mobile', 'hva_dailystreaks_mobile_destination')):
                done = _int(a.get('hva_dailystreaks_%s_completed_amount' % key), -1)
                mx = _int(a.get('hva_dailystreaks_%s_completed_max' % key), 0)
                days = _int(a.get('hva_dailystreaks_%s_days' % key), 0)
                if mx <= 0:
                    continue
                raw.append({
                    'id': 'streak7:%s' % key, 'source': 'api', 'kind': 'punchcard',
                    'label': '%s 7天冲刺' % ('必应' if key == 'bing' else '移动应用'),
                    'progress': max(0, done), 'max': mx, 'complete': done >= mx,
                    'dest': a.get(dest_key) or None,
                    'verify': {'api': 'hva_dailystreaks_%s_completed_amount' % key},
                    'needs': 'api' if key == 'mobile' else 'browser',
                    'discovered': 'api:promo', 'multi_day': True, 'days': days,
                })
            continue

        # 5) 其它 edge_flyout_hva（punchcard 本体在 DOM 里，这里只登记激活信号）
        if ptype == 'edge_flyout_hva' and 'streak' not in name:
            raw.append({
                'id': 'activation:%s' % name, 'signal': True, 'reason': 'hva_promo',
                'label': a.get('title') or name, 'offerid': a.get('offerid') or '',
                'type': ptype,
            })
        # 其余促销不构成"每日必做任务"，忽略
    return raw


# ---------------- DOM 只读采集 ----------------

_DOM_JS = r"""
(() => {
  const BADGE = '%(badge)s';
  const sels = %(sels)s;
  const hasBadge = el => !!el && !!el.querySelector && !!el.querySelector('[class*="' + BADGE + '"]');
  // 徽章归属：只看卡片自身；父层只有在"仅含一张卡"时才算（否则会把兄弟卡的徽章借过来）
  const badgeOf = (card) => {
    if (hasBadge(card)) return true;
    const p = card.parentElement;
    if (p && p.querySelectorAll('a[href]').length <= 1 && hasBadge(p)) return true;
    return false;
  };
  const progOf = t => {
    const s = t || '';
    // 优先咬住紧跟"个任务/tasks/days"的 x/y, 避免描述里的其他数字(如 +1,320)干扰
    const m = s.match(/(\d+)\s*\/\s*(\d+)\s*(?:个任务|个活動|tasks|days|天)/i)
           || s.match(/(\d+)\s*\/\s*(\d+)/);
    return m ? [parseInt(m[1]), parseInt(m[2])] : null;
  };
  const climb = (a, root, maxLvl) => {
    let el = a;
    const out = [a];
    for (let i = 0; i < maxLvl; i++) {
      const p = el.parentElement;
      if (!p || p === root || !root.contains(p)) break;
      el = p;
      out.push(el);
      if (progOf(el.innerText) || hasBadge(el)) break;
    }
    return out;
  };
  // 有界上溯：一旦祖先包含"第二个同类链接"就停，避免把整张列表当成一张卡
  const cardOf = (a, root, linkSel, maxLvl) => {
    let el = a;
    let pickEl = a;
    for (let i = 0; i < maxLvl; i++) {
      const p = el.parentElement;
      if (!p || p === root || !root.contains(p)) break;
      if (linkSel && p.querySelectorAll(linkSel).length > 1) break;
      const t = p.innerText || '';
      if (t.length > 450) break;
      el = p;
      pickEl = el;
    }
    return pickEl;
  };

  const scan = (root) => {
    const seen = new Set();
    const items = [];
    root.querySelectorAll('a[href]').forEach(a => {
      const txt0 = (a.innerText || '').replace(/\s+/g, ' ').trim();
      const key = a.href + '|' + txt0.slice(0, 40);
      if (seen.has(key)) return;
      seen.add(key);
      const card = txt0.length >= 12 ? a : cardOf(a, root, 'a[href]', 4);
      const txt = ((card.innerText || txt0) || '').replace(/\s+/g, ' ').trim();
      items.push({
        idx: items.length, id: card.id || '', tag: card.tagName,
        cls: String(card.className || '').slice(0, 80),
        text: txt.slice(0, 220),
        prog: progOf(txt), badge: badgeOf(card),
        href: a.href || '', anchors: card.querySelectorAll('a[href]').length,
      });
    });
    if (!items.length) {
      Array.from(root.children || []).forEach((el, idx) => {
        if (['STYLE','SCRIPT','TEMPLATE'].includes(el.tagName)) return;
        const txt = (el.innerText || '').replace(/\s+/g, ' ').trim();
        if (!txt) return;
        items.push({idx, id: el.id || '', tag: el.tagName,
                    cls: String(el.className || '').slice(0, 80), text: txt.slice(0, 220),
                    prog: progOf(txt), badge: badgeOf(el), href: '', anchors: 0});
      });
    }
    return {present: true, count: items.length, items};
  };

  const out = {};
  for (const [key, sel] of Object.entries(sels)) {
    const box = document.querySelector(sel);
    out[key] = box ? scan(box) : {present: false, count: 0, items: []};
  }

  const st = document.querySelector(sels.streaks);
  if (st) {
    const slots = [];
    st.querySelectorAll('*').forEach(el => {
      const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
      const m = t.match(/^([^:：]{1,20})[:：]\s*(\d+)\s*\/\s*(\d+)$/);
      if (m && !slots.some(x => x.label === m[1].trim())) {
        slots.push({label: m[1].trim(), prog: [parseInt(m[2]), parseInt(m[3])],
                    badge: hasBadge(el.parentElement)});
      }
    });
    out.streaks_slots = slots.slice(0, 8);
  }

  const groups = {};
  document.querySelectorAll('#quests [id^="quest_"], #quests a[href*="/earn/quest/"], a[href*="/earn/quest/"]').forEach(el => {
    const a = el.tagName === 'A' ? el : el.querySelector('a[href]');
    const href = (a && a.href) || '';
    const m = href.match(/\/earn\/quest\/([^\/?#]+)/) || (el.id || '').match(/^quest_(.+)$/);
    if (!m) return;
    const name = m[1];
    const root = document.querySelector('#quests') || document.body;
    const card = a ? cardOf(a, root, 'a[href*="/earn/quest/"]', 5) : el;
    const t = ((card.innerText || '') || '').replace(/\s+/g, ' ').trim();
    const cur = groups[name];
    if (!cur || t.length > cur.text.length) {
      groups[name] = {id: name, text: t.slice(0, 220), prog: progOf(t),
                      badge: badgeOf(card), href: href || el.id};
    }
  });
  out.quest_cards = Object.values(groups);

  const lv = document.querySelector(sels.levelup);
  out.levelup_items = [];
  if (lv) {
    const seenLv = new Set();
    lv.querySelectorAll('*').forEach(el => {
      const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
      if (t && t.length < 60 && !seenLv.has(t)) { seenLv.add(t); out.levelup_items.push(t); }
    });
    out.levelup_items = out.levelup_items.slice(0, 10);
  }

  out.page = {url: location.href, title: document.title};
  return JSON.stringify(out);
})()
"""



def discover_dom(name, config, proxy=None, log=None, extra_wait=0):
    """起浏览器只读采集 DOM 容器（dashboard + earn 两页合并）。返回 (raw_dict, meta)。

    实测：`#dailyset`/`#streaks` 在 dashboard；`#quests`/`#moreactivities`/`#exploreonbing`
    在 /earn。两页都要读，缺一页就会漏任务。全程只导航 + 读取，不点击。
    """
    from msr import browser, runner

    log = log or (lambda m: None)
    prof = config.profile(name)
    cookies = config.load_json(config.pfile(name, 'cookie.json'), []) or []

    lock = runner.acquire_runner_lock(config, name=name)
    if not lock:
        return None, {'error': 'runner.lock 被占用（该账号正在执行任务），已跳过 DOM 采集'}

    ch = None
    meta = {'cookies_injected': len(cookies)}
    try:
        ch = browser.Chromium(binary='chromium', user_data_dir=os.path.join(prof, 'chrome'),
                              headless=True, log=lambda m: None, proxy=proxy)
        tabs = ch.start()
        pages = [t for t in tabs if t.get('type') == 'page']
        cdp = ch.attach(pages[0] if pages else ch.new_tab('about:blank'))
        if cookies:
            browser.inject_cookies(cdp, cookies, log=lambda m: None)

        js = _DOM_JS % {'badge': BADGE_CLASS,
                        'sels': json.dumps(CONTAINERS, ensure_ascii=False)}

        def read_page(url, wait):
            browser.navigate(cdp, url)
            time.sleep(wait + extra_wait)
            raw = browser.ev(cdp, js, timeout=30)
            try:
                return json.loads(raw) if isinstance(raw, str) else {}
            except Exception:
                return {}

        dash = read_page('https://rewards.bing.com/dashboard', 10)
        earn = read_page('https://rewards.bing.com/earn', 9)

        merged = {}
        avail = {}
        for key in CONTAINERS:
            a = dash.get(key) or {}
            b = earn.get(key) or {}
            avail[key] = {'dashboard': bool(a.get('present')), 'earn': bool(b.get('present'))}
            merged[key] = a if (a.get('count') or 0) >= (b.get('count') or 0) else b

        merged['streaks_slots'] = (dash.get('streaks_slots') or earn.get('streaks_slots') or [])
        qmap = {}
        for q in (dash.get('quest_cards') or []) + (earn.get('quest_cards') or []):
            qid = q.get('id')
            if not qid:
                continue
            cur = qmap.get(qid)
            if not cur or len(q.get('text') or '') > len(cur.get('text') or ''):
                qmap[qid] = q
        merged['quest_cards'] = list(qmap.values())
        merged['levelup_items'] = (dash.get('levelup_items') or earn.get('levelup_items') or [])
        merged['available'] = avail
        merged['pages'] = {'dashboard': (dash.get('page') or {}).get('url'),
                           'earn': (earn.get('page') or {}).get('url')}
        meta['available'] = avail
        meta['page'] = merged['pages'].get('earn')
        return merged, meta
    except Exception as ex:
        meta['error'] = str(ex)[:200]
        return None, meta
    finally:
        if ch:
            try:
                ch.stop()
            except Exception:
                pass
        runner.release_runner_lock(lock)
