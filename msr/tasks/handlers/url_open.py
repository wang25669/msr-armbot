"""P4 · S2: url_open 统一 handler。

把"打开(点击)卡片即归因"的四类任务合并到一处:
  1. moreactivities  —— #moreactivities 内的积分卡
  2. explore         —— #exploreonbing 探索卡 (每卡用自己的关键词, 红线)
  3. dailyset-urlopen—— #dailyset 中 kind=url_open 的卡 (与 quiz 卡混排, 本阶段仍走旧路径)
  4. urlreward       —— /dapi/me 下发 type=urlreward 且带 destination 的 promo (DOM 无锚点, 导航+停留)

硬约束 (来自 2026-11 实测, 不许改):
  - 卡片必须"真人点击真实锚点", 直接导航 dest 对 moreactivities/dailyset 不计分;
  - explore 必须一卡一词, 用 form=ML2PCR 那套参数打开;
  - 归因停留时间读 cards.attribution_wait_min/max;
  - 自己开的标签页在 finally 里关掉。
"""
import json
import random
import re
import time
import urllib.parse

from msr.browser import ev, navigate, human_click
from msr.tasks_browser import container_snapshot, extract_semantic_explore_query


def _dwell_window(log, tag=''):
    """归因停留窗口 (读 settings.cards.attribution_wait_min/max)。"""
    from msr import settings as S
    st = S.load_settings()
    wmin = int(st.get('cards', {}).get('attribution_wait_min', 60))
    wmax = int(st.get('cards', {}).get('attribution_wait_max', 120))
    return wmin, wmax


def _close_tabs(ch, tids, log):
    for tid in tids or []:
        try:
            ch.close_tab(tid)
        except Exception:
            pass


# ---------------- 1. moreactivities ----------------

MA_EXPAND_JS = """(() => {
  const ma = document.getElementById("moreactivities");
  if (!ma) return false;
  const trigger = ma.querySelector("button[slot='trigger'][aria-expanded='false'], [class*=Disclosure]:not([data-expanded='true']) button[aria-expanded='false']");
  if (trigger) trigger.click();
  return true;
})()"""

MA_CARDS_JS = """(() => {
  const ma = document.getElementById("moreactivities");
  if (!ma) return [];
  return Array.from(ma.querySelectorAll("a[href]")).map((a, idx) => {
    // 2026-11-10 某SG号2 实测: 网格外层被一个 <a> 包住, 它的 innerText 是全部卡片文本,
    // 里面含 "已完成" -> 所有卡都被判完成 -> 该号 20/65 却报"日常任务已完成"。必须剔掉包裹锚点。
    if (a.querySelector("a[href]")) return null;
    let card = a.closest("li, div[class*=rounded], div.flex.flex-col") || a;
    if ((card.innerText || "").length > 240) card = a;   // 容器过大 => 抓到了整块网格, 退回锚点本身
    const txt = (card.innerText || a.innerText || "").replace(/\\n/g, " ");
    const href = a.href || "";
    const badge = !!card.querySelector('[class*="bg-statusSuccessRewardsBg"]');
    return {
      idx: idx, href: href, text: txt, badge: badge,
      isRedeem: href.includes("/redeem/"),
      hasPoints: txt.includes("+") || txt.includes("分") || /\\b\\d+\\s*(?:points|pts)\\b/i.test(txt),
      doneText: /已完成|complete|completed/i.test(txt)
    };
  }).filter(Boolean);
})()"""


# 需要真实产品内动作才能计分的卡: 点开也不落库, 只能人工做。
# 实测 2026-11-10: 「让创意闪耀，赢取20积分」要求用 Bing Image Creator 出图并设为壁纸;
# 「Download Bing app to earn 500」要装应用 —— 这两类反复触发"部分完成"会让阶段10白回补 4 分钟。
MA_MANUAL_PAT = re.compile(
    r'image\s*creator|wallpaper|设为壁纸|下载.{0,12}(?:app|应用)|download.{0,12}app|'
    r'安装.{0,12}(?:app|应用)|install.{0,12}app', re.I)


def _ma_manual(card):
    return bool(MA_MANUAL_PAT.search(card.get('text') or ''))


def run_moreactivities(name, cdp, pr, ch=None, log=print, budget_seconds=1500):
    """#moreactivities: 逐卡真人点击 -> 新标签停留归因 -> 关标签 -> 回读徽章。"""
    navigate(cdp, 'https://rewards.bing.com/earn')
    time.sleep(random.uniform(2.5, 4.0))
    ev(cdp, 'window.scrollTo(0, document.body.scrollHeight/2);')
    time.sleep(1.0)
    ev(cdp, 'window.scrollTo(0, document.body.scrollHeight);')
    time.sleep(1.0)

    cards = []
    deadline = time.time() + 20
    while time.time() < deadline:
        ev(cdp, MA_EXPAND_JS)
        cards = ev(cdp, MA_CARDS_JS) or []
        if cards:
            break
        time.sleep(1.0)

    # 只保留"未完成 + 有分值 + 非兑换"的卡
    manual = [c for c in cards if _ma_manual(c) and not c.get('badge')]
    todo = [c for c in cards if not c.get('isRedeem') and not c.get('badge')
            and not c.get('doneText') and c.get('hasPoints') and not _ma_manual(c)]
    if manual:
        log('[P4] 需人工完成(点开也不计分) %d 张: %s' % (
            len(manual), ' | '.join((c.get('text') or '')[:24] for c in manual)))
    snapshot = container_snapshot(cdp, 'moreactivities', log=log, tag='[P4] ')
    if not cards:
        log('[P4] 今日 #moreactivities 无卡片')
        return '今日无日常任务(跳过)'
    # 诊断: 只在"一张都不点"时打印 #moreactivities 内部可点元素与解析字段, 定位漏点原因
    if not todo and not manual:
      try:
          _scan = ev(cdp, """(() => {
            const ma = document.getElementById('moreactivities');
            if (!ma) return { noMa: true };
            const all = Array.from(ma.querySelectorAll("a[href], button, [role='button']"));
            const tiles = all.map((el, i) => ({
              i: i, tag: el.tagName, cls: (el.className || '').toString().slice(0, 46),
              href: (el.getAttribute('href') || '').slice(0, 64),
              t: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 34)
            })).filter(x => x.t);
            return { anchors: ma.querySelectorAll('a[href]').length,
                     buttons: ma.querySelectorAll('button').length,
                     total: tiles.length, tiles: tiles.slice(0, 20) };
          })()""")
          log('[P4] moreactivities 结构诊断: %s' % json.dumps(_scan, ensure_ascii=False)[:1500])
          _rows = [{k: c.get(k) for k in ('idx', 'text', 'hasPoints', 'badge', 'isRedeem', 'doneText')} for c in cards]
          log('[P4] 卡片解析结果(共%d): %s' % (len(cards), json.dumps(_rows, ensure_ascii=False)[:1600]))
      except Exception as _ex_scan:
          log('[P4] moreactivities 结构诊断异常(容忍): %s' % str(_ex_scan)[:60])
    badged_n = (snapshot or {}).get('badged', 0)
    if not todo:
        # 注意: 这里不能出现"待做/未完成"字样 —— runner 的阶段10回补用这些子串判失败
        return '日常任务已完成(锚点%d, 带徽章%d%s)' % (
            len(cards), badged_n, (', 需人工%d张' % len(manual)) if manual else '')

    log('[P4] #moreactivities 待做 %d / 共 %d 张' % (len(todo), len(cards)))
    wmin, wmax = _dwell_window(log)
    done = 0
    t0 = time.time()
    for card in todo:
        if time.time() - t0 > budget_seconds:
            log('[P4] 超出卡片阶段预算, 停止推进')
            break
        idx = card['idx']
        text = (card.get('text') or '')[:30]
        ev(cdp, MA_EXPAND_JS)
        selector = 'document.querySelectorAll("#moreactivities a[href]")[%d]' % idx
        new_tabs = human_click(cdp, selector, label=text[:15], close_new=False) or []
        try:
            if new_tabs and ch:
                w = random.uniform(wmin, wmax)
                log('[P4] 卡片[%s] 已在新标签打开, 停留 %.1f 秒等待归因' % (text, w))
                time.sleep(w)
            else:
                w = random.uniform(min(wmin, 10), min(wmax, 20))
                log('[P4] 卡片[%s] 未捕获新标签, 原地停留 %.1f 秒' % (text, w))
                time.sleep(w)
        finally:
            _close_tabs(ch, new_tabs, log)
        done += 1

    def _reread(tag='[P4] 回读'):
        '''回 /earn 重新读卡片状态; 归因落库常滞后 1-3 分钟, 所以调用方可能读多轮。'''
        navigate(cdp, 'https://rewards.bing.com/earn')
        time.sleep(random.uniform(3.5, 5.0))
        for _ in range(8):
            if ev(cdp, 'document.getElementById("moreactivities") !== null'):
                break
            time.sleep(1.0)
        ev(cdp, MA_EXPAND_JS)
        time.sleep(1.0)
        cards_now = ev(cdp, MA_CARDS_JS) or []
        remain_now = [c for c in cards_now if not c.get('isRedeem') and not c.get('badge')
                      and not c.get('doneText') and c.get('hasPoints') and not _ma_manual(c)]
        snap = container_snapshot(cdp, 'moreactivities', log=log, tag=tag)
        return cards_now, remain_now, (snap or {}).get('badged', 0)

    after, remain, badged = _reread('[P4] 回读')
    # 2026-11-09 实测: 点开的卡当时徽章未落(43 秒后仍 0), 阶段10回补 4 分钟后同一张卡显示已完成 ——
    # 说明归因有 1-3 分钟延迟。这里补一轮等待+复读, 避免把"还在归因"误报成"部分完成"而触发多余回补。
    if remain and done > 0:
        settle = max(180.0, float(wmax) * 3)
        log('[P4] 仍有 %d 张未带徽章, 归因滞后则等 %.0f 秒后复读...' % (len(remain), settle))
        time.sleep(settle)
        after, remain, badged = _reread('[P4] 复读')
    if not remain:
        return '日常任务已全部完成(共%d张, 徽章%d%s)' % (
            len(after), badged, (', 需人工%d张' % len(manual)) if manual else '')
    return '部分完成:处理%d张(剩%d张待做, 徽章%d)' % (done, len(remain), badged)


# ---------------- 2. explore ----------------

EXPLORE_FIND_JS = """(() => {
  const headings = Array.from(document.querySelectorAll("h2, h3, button, span, div")).filter(el => {
    const t = (el.innerText || "").replace(/\\s+/g, " ").trim();
    return /^(?:Explore on Bing|在必应上探索|探索必应)/i.test(t) && t.length < 50;
  });
  if (!headings.length) return null;
  const heading = headings[0];
  const disc = heading.closest(".react-aria-Disclosure, [class*=Disclosure]") || heading.parentElement;
  const text = disc ? (disc.innerText || "").replace(/\\s+/g, " ").trim() : (heading.innerText || "");
  const m = text.match(/(\\d+)\\s*\\/\\s*(\\d+)/);
  const prog = m ? parseInt(m[1], 10) : 0;
  const max = m ? parseInt(m[2], 10) : 40;
  if (disc) {
    const btn = disc.querySelector("button[aria-expanded='false']");
    if (btn) btn.click();
  }
  return { found: true, prog: prog, max: max };
})()"""

EXPLORE_CARDS_JS = """(() => {
  const headings = Array.from(document.querySelectorAll("h2, h3, button, span, div")).filter(el => {
    const t = (el.innerText || "").replace(/\\s+/g, " ").trim();
    return /^(?:Explore on Bing|在必应上探索|探索必应)/i.test(t) && t.length < 50;
  });
  if (!headings.length) return [];
  const heading = headings[0];
  const disc = heading.closest(".react-aria-Disclosure, [class*=Disclosure]") || heading.parentElement;
  const panel = disc ? (disc.querySelector(".react-aria-DisclosurePanel, [class*=DisclosurePanel]") || disc) : null;
  if (!panel) return [];
  return Array.from(panel.querySelectorAll("a[href]")).map((a, idx) => {
    const text = (a.innerText || "").replace(/\\s+/g, " ").trim();
    const ps = Array.from(a.querySelectorAll("p")).map(p => (p.innerText || "").trim());
    return {
      idx: idx,
      text: text,
      title: ps[0] || text.split("\\n")[0] || "",
      desc: ps[1] || text || "",
      href: a.href || "",
      isDone: /completed|已完成/i.test(text) || !!a.querySelector('[class*="bg-statusSuccessRewardsBg"]'),
      isLocked: /unlocks tomorrow|明天解锁|locked/i.test(text)
    };
  }).filter(c => !c.isDone && !c.isLocked && c.text.length > 5);
})()"""


EXPLORE_STATS_JS = """(() => {
  const heads = Array.from(document.querySelectorAll("h2,h3,button,span,div")).filter(el => {
    const t = (el.innerText || "").replace(/\\s+/g, " ").trim();
    return /^(?:Explore on Bing|在必应上探索|探索必应)/i.test(t) && t.length < 50;
  });
  if (!heads.length) return null;
  const disc = heads[0].closest(".react-aria-Disclosure,[class*=Disclosure]") || heads[0].parentElement;
  const panel = disc ? (disc.querySelector(".react-aria-DisclosurePanel,[class*=DisclosurePanel]") || disc) : null;
  if (!panel) return null;
  const links = Array.from(panel.querySelectorAll("a[href]")).map(a => {
    const t = (a.innerText || "").replace(/\\s+/g, " ").trim();
    return { t: t.slice(0, 36),
             done: /completed|已完成/i.test(t) || !!a.querySelector('[class*="bg-statusSuccessRewardsBg"]'),
             locked: /unlocks tomorrow|明天解锁|locked/i.test(t) };
  });
  return { total: links.length, done: links.filter(x => x.done).length,
           locked: links.filter(x => x.locked).length, pending: links.filter(x => !x.done && !x.locked).length,
           sample: links.slice(0, 12) };
})()"""


def run_explore(name, cdp, pr, ch=None, log=print):
    """#exploreonbing: 一卡一词, 新标签打开 form=ML2PCR 搜索页并停留归因。"""
    from msr import settings as S
    _cfg = S.load_settings()
    ex = _cfg.get('explore', {})
    dmin = float(ex.get('dwell_min', 16))
    dmax = float(ex.get('dwell_max', 20))
    retry_dwell = float(ex.get('retry_dwell_seconds', 30))
    budget_seconds = float(ex.get('budget_seconds', 1200))
    if dmax < dmin:
        dmin, dmax = dmax, dmin

    navigate(cdp, 'https://rewards.bing.com/earn')
    time.sleep(random.uniform(2.5, 4.0))
    info = ev(cdp, EXPLORE_FIND_JS)
    if not info or not info.get('found'):
        log('[P4] 未检测到 Explore on Bing 模块 (正常变体, 跳过)')
        return '无此模块(跳过)'
    if info.get('prog', 0) >= info.get('max', 40) > 0:
        return '已满额(%d/%d)' % (info.get('prog', 40), info.get('max', 40))

    try:
        _stats = ev(cdp, EXPLORE_STATS_JS)
        if _stats:
            log('[P4] explore 面板统计: 共%d 已完成%d 明天解锁%d 待打卡%d' % (
                _stats.get('total', 0), _stats.get('done', 0), _stats.get('locked', 0), _stats.get('pending', 0)))
            for _s in (_stats.get('sample') or []):
                log('[P4]   · %s%s%s' % (_s.get('t', '')[:36], ' [已完成]' if _s.get('done') else '',
                                        ' [明天解锁]' if _s.get('locked') else ''))
    except Exception as _ex_st:
        log('[P4] explore 面板统计异常(容忍): %s' % str(_ex_st)[:60])
    cards = ev(cdp, EXPLORE_CARDS_JS) or []
    if not cards:
        return '无可用卡片(跳过)'
    log('[P4] Explore 待打卡 %d 张 (当前 %d/%d)' % (len(cards), info.get('prog', 0), info.get('max', 40)))

    fixed_query = None
    done = 0
    _t0 = time.time()
    _budget = budget_seconds
    processed = set()

    def _fresh_cards():
        '''重读卡片列表 (面板懒渲染: 一次性快照只拿到已渲染的几张, 2026-11-10 实测只拿到 4/8 张, 少赚 20 分)'''
        return [x for x in (ev(cdp, EXPLORE_CARDS_JS) or [])
                if (x.get('title') or x.get('text') or '') not in processed]

    while True:
        pending = _fresh_cards()
        if not pending:
            ev(cdp, 'window.scrollBy(0, 700);')
            time.sleep(2.5)
            pending = _fresh_cards()
            if not pending:
                break
        if time.time() - _t0 > _budget:
            log('[P4] explore 超出 %.0f 分钟预算, 停止推进' % (_budget / 60))
            break
        c = pending[0]
        processed.add(c.get('title') or c.get('text') or '')
        if True or not fixed_query:
            fixed_query = extract_semantic_explore_query(c.get('title') or '', c.get('desc') or '')
        query = fixed_query
        log('[P4] 探索卡[%d]: %s -> 词:"%s" | 原文:"%s"' % (
            c['idx'], (c.get('title') or c.get('text'))[:32], query, (c.get('desc') or '')[:90]))

        before = set()
        try:
            from msr.browser import tab_ids as _tab_ids
            before = set(_tab_ids(cdp))
        except Exception:
            pass

        ev(cdp, """(() => {
          const headings = Array.from(document.querySelectorAll("h2, h3, button, span, div")).filter(el => {
            const t = (el.innerText || "").replace(/\\s+/g, " ").trim();
            return /^(?:Explore on Bing|在必应上探索|探索必应)/i.test(t) && t.length < 50;
          });
          if (!headings.length) return false;
          const disc = headings[0].closest(".react-aria-Disclosure, [class*=Disclosure]") || headings[0].parentElement;
          const panel = disc ? (disc.querySelector(".react-aria-DisclosurePanel, [class*=DisclosurePanel]") || disc) : null;
          if (!panel) return false;
          const links = panel.querySelectorAll("a[href]");
          if (links && links[%d]) { links[%d].target = "_blank"; links[%d].click(); return true; }
          return false;
        })()""" % (c['idx'], c['idx'], c['idx']))
        time.sleep(random.uniform(2.5, 3.5))

        new_tabs = []
        if hasattr(cdp, 'port'):
            try:
                from msr.browser import _http_json
                all_tabs = _http_json('http://127.0.0.1:%d/json/list' % cdp.port, timeout=3)
                new_tabs = [t for t in all_tabs if t.get('id') not in before and t.get('type') == 'page']
            except Exception:
                pass

        target = cdp
        tid = None
        if new_tabs and ch:
            tid = new_tabs[0].get('id')
            try:
                target = ch.attach(new_tabs[0])
            except Exception:
                target = cdp
                tid = None

        prog_before_card = (ev(cdp, EXPLORE_FIND_JS) or {}).get('prog') or 0
        # rwAutoFlyout=exb 是 API 下发 dest 里带的参数(见 discover 的 dest), 缺它不带出 Rewards 侧边栏,
        # 搜索可能不计入该 promo —— 2026-11-10 对照 dest 后补上
        url = ('https://www.bing.com/search?q=%s&form=ML2PCR&OCID=ML2PCR&PUBL=RewardsDO'
               '&CREA=ML2PCR&PC=ML2PCR&rwAutoFlyout=exb' % urllib.parse.quote_plus(query))
        dwell = random.uniform(dmin, dmax)
        navigate(target, url)
        log('[P4] 搜索结果页停留 %.1f 秒等待归因' % dwell)
        time.sleep(dwell)
        # 每张卡必须搜"对应关键词"才计分: 首搜没加分就补一次标题原词搜索(同标签 + 加长停留)
        try:
            prog_mid = (ev(cdp, EXPLORE_FIND_JS) or {}).get('prog') or 0
        except Exception:
            prog_mid = prog_before_card
        if prog_mid <= prog_before_card:
            alt_q = (c.get('title') or '').strip() or query
            if alt_q and alt_q != query:
                navigate(target, 'https://www.bing.com/search?q=%s&form=ML2PCR&OCID=ML2PCR&PUBL=RewardsDO'
                                 '&CREA=ML2PCR&PC=ML2PCR&rwAutoFlyout=exb' % urllib.parse.quote_plus(alt_q))
                log('[P4] 首搜未加分, 补搜标题原词: "%s" (停留 %.0f 秒)' % (alt_q[:40], retry_dwell))
                time.sleep(retry_dwell)
                try:
                    prog_mid = (ev(cdp, EXPLORE_FIND_JS) or {}).get('prog') or prog_mid
                except Exception:
                    pass
        try:
            ev(target, 'window.scrollBy(0, 400);')
            time.sleep(2.0)
        except Exception:
            pass
        if tid and ch:
            try:
                ch.close_tab(tid)
            except Exception:
                pass
            time.sleep(1.5)
        done += 1

    navigate(cdp, 'https://rewards.bing.com/earn')
    time.sleep(random.uniform(3.0, 5.0))
    fin = ev(cdp, EXPLORE_FIND_JS) or {}
    # 归因落库有 1-3 分钟延迟: 收尾再等一轮复读, 避免报告与阶段10判断被"还没到账"误导
    try:
        settle = max(90.0, dmax * 3)
        log('[P4] explore 收尾归因等待 %.0f 秒后复读进度...' % settle)
        time.sleep(settle)
        fin2 = ev(cdp, EXPLORE_FIND_JS) or {}
        if (fin2.get('prog') or 0) >= (fin.get('prog') or 0):
            fin = fin2
    except Exception as ex_s:
        log('[P4] explore 收尾复读异常(容忍): %s' % str(ex_s)[:60])
    return '完成%d个探索打卡(进度%s/%s)' % (done, fin.get('prog', done * 10), fin.get('max', 40))


# ---------------- 3. urlreward (API 下发, DOM 无锚点) ----------------

def run_urlrewards(name, cdp, pr, ch=None, log=print, token=None, api=None, config=None, proxy=None):
    """urlreward 族: /dapi/me 里 type=urlreward 且带 destination 的 promo, 导航 + 停留 + 回读。"""
    if not (api and token):
        return '无urlreward任务(跳过)'
    # 直接用起跑前 probe 已拿到的促销快照, 不再为了查 urlreward 额外请求一次 /dapi/me
    if isinstance(pr, dict) and pr.get('promotions') is not None:
        promos = pr.get('promotions') or []
    else:
        rg = {'country': pr.get('country', 'US'), 'language': pr.get('language', 'en')}
        try:
            resp = api.get_me(name, token, rg, proxy=proxy)
        except Exception as ex:
            return '无urlreward任务(跳过: %s)' % str(ex)[:40]
        promos = resp.get('promotions') or []
    todo = []
    for p in promos:
        attrs = p.get('attributes') or {}
        if str(attrs.get('type') or '').lower() != 'urlreward':
            continue
        dest = p.get('destination') or attrs.get('destination_url') or attrs.get('destination')
        if not dest:
            continue
        if str(attrs.get('complete', '')).lower() == 'true':
            continue
        todo.append((p.get('offerid') or attrs.get('offerid') or '?', dest))
    if not todo:
        log('[P4] 服务端无 urlreward 待做项')
        return '无urlreward任务(跳过)'

    log('[P4] 发现 %d 个 urlreward 任务' % len(todo))
    wmin, wmax = _dwell_window(log)
    done = 0
    for offer, dest in todo:
        log('[P4] urlreward[%s] -> %s' % (str(offer)[:24], str(dest)[:70]))
        try:
            navigate(cdp, dest)
            time.sleep(random.uniform(wmin, wmax))
            done += 1
        except Exception as ex:
            log('[P4] urlreward 打开失败: %s' % str(ex)[:60])
    return '处理%d个urlreward任务' % done
