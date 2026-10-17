"""浏览器侧任务: 搜索 / 每日活动卡 / 日常任务卡。

从现网 bing_daily_v2.py phase2/phase1/phase5 移植, 关键差异:
- 无 X11/xdotool: 回车走 CDP Input.dispatchKeyEvent(现网主路径本就不依赖 X)
- headless 下无需抢前台(CDP 直连目标标签)
- 域名按 probe 结果决定(cn 账号 -> cn.bing.com)
"""
import json
import random
import re
import time
import urllib.parse

from msr.browser import ev, navigate, StepError, human_click, close_tab


def bing_domain(pr):
    c = (pr.get('country') or 'US').lower()
    return 'cn.bing.com' if c == 'cn' else 'www.bing.com'


def press_enter(cdp):
    """回车走 CDP(不依赖 X11)。"""
    for t in ('keyDown', 'keyUp'):
        cdp.call('Input.dispatchKeyEvent',
                 {'type': t, 'key': 'Enter', 'code': 'Enter',
                  'windowsVirtualKeyCode': 13, 'nativeVirtualKeyCode': 13})


def typed_search(cdp, query, domain):
    """真人式搜索: 首页输入框 -> 逐字插入 -> 随机延迟 -> 回车。"""
    navigate(cdp, f'https://{domain}/')
    focused = ev(cdp, "(function(){var el=document.querySelector("
                        "'textarea[name=q],input[name=q]');if(!el)return 'NO-INPUT';"
                        'el.focus();return "OK"})()')
    if focused != 'OK':
        raise StepError('搜索框失踪')
    cdp.call('Input.insertText', {'text': query})
    time.sleep(random.uniform(1.0, 2.5))
    press_enter(cdp)
    time.sleep(random.uniform(6, 9))
    return ev(cdp, 'location.href')


def scroll_human(cdp):
    try:
        ev(cdp, 'window.scrollTo(0, document.body.scrollHeight)')
        time.sleep(random.uniform(2, 4))
        ev(cdp, 'window.scrollTo(0, 0)')
    except StepError:
        pass


def search_counters(cdp):
    """读 dashboard 计数器(pcSearch/mobileSearch)。"""
    d = _dashboard(cdp)
    c = {}
    for item in (d.get('counters') or {}):
        if item.get('name') in ('pcSearch', 'mobileSearch'):
            c[item['name']] = (item.get('pointProgress'), item.get('pointProgressMax'))
    return c


def _dashboard(cdp):
    from msr.browser import StepError as SE
    r = ev(cdp, '''
    (function(){
      var out = {};
      try {
        var scr = document.getElementById("wa-embedded-search-header");
        if (scr) { out.counters = JSON.parse(scr.getAttribute("data-counters")); }
      } catch(e) {}
      return out;
    })()''')
    return r or {}


def phase_search(name, cdp, pr, budget_seconds=3600, target=None, log=print):
    """搜索计数到 target(缺省 dashboard max)。返回 'x/y'。"""
    domain = bing_domain(pr)
    t0 = time.time()
    queries = ['weather', 'news', 'python tutorial', 'best coffee', 'local events',
               'sports scores', 'recipes', 'travel tips', 'movies', 'music',
               'tech news', 'health tips', 'home decor', 'gardening', 'finance',
               'books', 'fitness', 'history', 'science', 'art',
               'cars', 'photography', 'fashion', 'games', 'food']
    random.shuffle(queries)
    i, last_c = 0, None
    while time.time() - t0 < budget_seconds:
        c = search_counters(cdp)
        last_c = c or last_c
        pc = c.get('pcSearch') if c else None
        if pc and (pc[1] is None or int(pc[1]) <= 0):
            log('今日无搜索任务(计数器 max=%s), 跳过' % (pc[1],))
            return '0/0 无任务'
        if pc and target is None:
            target = pc[1]
        if pc and pc[0] >= pc[1]:
            log(f'搜索已完成 {pc[0]}/{pc[1]}')
            break
        q = queries[i % len(queries)] + ('' if i < len(queries) else f' {i}')
        i += 1
        href = typed_search(cdp, q, domain)
        scroll_human(cdp)
        log(f'搜索[{i}] {q} -> {str(href)[:60]}')
        time.sleep(random.uniform(25, 50))
    c = last_c or {}
    pc = c.get('pcSearch') or ('?', '?')
    return f'{pc[0]}/{pc[1]}'


# ---------- 每日活动卡 / 日常任务卡 ----------

def dailyset_kind(p):
    """卡片类型(结构化字段判定, 零文案; 2026-10-02 教训)。

    quiz=页内答题(IsConversation/WQOskey/dsetqu); urloffer=BTEPOKey UrlOffer;
    sid=filters sid uuid; direct=其余(点击即计分)。
    """
    url = p.get('destinationUrl') or ''
    m = re.search(r'filters=([^&]+)', url)
    filters = urllib.parse.unquote(m.group(1)) if m else ''
    if 'BTEPOKey:"REWARDSQUIZ_DailySet_UrlOffer"' in filters:
        return 'urloffer'
    if 'IsConversation:"True"' in filters or 'WQOskey:' in filters:
        return 'quiz'
    if re.search(r'form=(tgrew4|dsetqu)', url):
        return 'quiz'
    if re.search(r'filters=sid:"[0-9a-f-]{36}"', filters, re.I):
        return 'sid'
    return 'direct'


def _dash_done(p):
    return str(p.get('complete', '')).lower() == 'true'


def _anchor_js(dest):
    """按 href 与 destinationUrl 的共享参数(form/filters sid/WQOskey/q)打分选锚点(零文案)。

    2026-10-15 从现网 phase1 移植: API 的 destinationUrl 与页面 href 常因本地化
    不同, 不能直接字符串相等; 用共享参数值打分取最高分锚点。
    """
    return ('''(() => {
      const dest = %s;
      const dp = (() => { try { const u = new URL(dest); const o = {};
        u.searchParams.forEach((v,k)=>o[k.toLowerCase()]=v); return o; } catch(e) { return {}; } })();
      const keys = [];
      if (dp.form) keys.push(dp.form);
      const filt = dp.filters || '';
      const sid = (filt.match(/sid:?\\s*"?([0-9a-f-]{8,})/i)||[])[1];
      if (sid) keys.push(sid);
      const wqo = (filt.match(/WQOskey:?\\s*"?([A-Za-z0-9_]+)/i)||[])[1];
      if (wqo) keys.push(wqo);
      if (dp.q) keys.push(dp.q);
      const anchors = [...document.querySelectorAll('a[href]')].filter(a => a.offsetParent !== null);
      const dec = (h) => { try { return decodeURIComponent(h); } catch(e) { return h; } };
      let best = null, bestScore = 0;
      for (const a of anchors) {
        const h = dec(a.href || '');
        let s = 0;
        for (const k of keys) { if (k && h.includes(k)) s += k.length; }
        if (s > bestScore) { bestScore = s; best = a; }
      }
      return best || null;
    })()''' % json.dumps(dest))


def phase_cards(name, cdp, pr, source, budget_seconds=1500, log=print,
                human_visit=None, max_tries=2):
    """phase1(每日活动) / phase5(日常任务) 通用: 点击未完成卡片。

    2026-10-15 与现网对齐两处关键行为:
    - **归因在"点击卡片"这个动作上, 直接 navigate 到 destinationUrl 不计分**,
      所以每张卡都回 dashboard, 按 dest 参数匹配真实锚点做真人点击;
    - 点击后停留 3-5 分钟(原实现只等 8-15 秒, 归因来不及落库)。
    quiz 卡由调用方注入 human_visit 答题。
    """
    domain = bing_domain(pr)
    key = time.strftime('%m/%d/%Y')
    done, tried = [], {}
    t0 = time.time()

    for attempt_round in range(max_tries * 4):
        if time.time() - t0 > budget_seconds:
            break
        d = _dash_from_page(cdp)
        cards = (d.get(source) or {}).get(key) if source == 'dailySetPromotions' \
            else (d.get(source) or [])
        if cards is None:
            break
        todo = [p for p in cards
                if not _dash_done(p) and tried.get(p.get('name'), 0) < max_tries]
        if not todo:
            break
        for card in todo:
            n = card.get('name', '?')
            tried[n] = tried.get(n, 0) + 1
            kind = dailyset_kind(card)
            dest = card.get('destinationUrl') or ''
            log('%s卡: %s [%s] 第%d次 -> %s' % (source, n, kind, tried[n], dest[:60]))
            new_tabs = []
            try:
                # 回 dashboard, 按参数匹配锚点, 真人点击(直接导航不计分)
                navigate(cdp, 'https://rewards.bing.com/')
                time.sleep(random.uniform(3, 6))
                new_tabs = human_click(cdp, _anchor_js(dest), label=n[:20], close_new=False)
                if kind == 'quiz' and human_visit is not None:
                    try:
                        human_visit(cdp, card)
                    except Exception as ex:
                        log('%s quiz交互容忍: %s' % (n, str(ex)[:60]))
                else:
                    wait = random.uniform(180, 300)     # 停留兼等归因落库
                    time.sleep(wait)
                    log('%s 停留%.1f分钟' % (n, wait / 60))
                for tid in new_tabs:
                    close_tab(cdp, tid)
                new_tabs = []
                # 回 dashboard 校验(读页面 wax 数据)
                navigate(cdp, 'https://rewards.bing.com/')
                time.sleep(random.uniform(2, 4))
                done_flag = _check_done(cdp, source, key, n, domain)
                if done_flag:
                    done.append(n)
                    log('%s ✓' % n)
                elif tried[n] >= max_tries:
                    log('%s %d次未落库, 放弃' % (n, tried[n]))
                else:
                    log('%s 没落库, 稍后重试' % n)
                    time.sleep(random.uniform(15, 30))
            except StepError as ex:
                log('%s 异常: %s' % (n, str(ex)[:80]))
                for tid in new_tabs:
                    close_tab(cdp, tid)
    if not tried:
        return '今日无%s任务(跳过)' % ('每日活动' if source == 'dailySetPromotions' else '日常任务')
    return '处理%d张' % len(done)


def _dash_from_page(cdp):
    """rewards dashboard 数据源(_ wax 数组)。"""
    r = ev(cdp, '''
    (function(){
      try {
        var el = document.querySelector("#ee#wax, body#wax, #wax");
        if (el && el.value) { return JSON.parse(el.value); }
        var scripts = document.querySelectorAll("script");
        for (var i = 0; i < scripts.length; i++) {
          var t = scripts[i].textContent || "";
          if (t.indexOf("dailySetPromotions") >= 0 && t.indexOf("morePromotions") >= 0) {
            var m = t.match(/\\("([a-zA-Z0-9_]+)"\\)/);
            if (m) {
              var data = window[m[1]];
              if (data) return JSON.parse(JSON.stringify(data));
            }
          }
        }
      } catch(e) {}
      return {};
    })()''')
    return r or {}


def _check_done(cdp, source, key, card_name, domain):
    try:
        d = _dash_from_page(cdp)
        cards = (d.get(source) or {}).get(key) if source == 'dailySetPromotions' \
            else (d.get(source) or [])
        for p in (cards or []):
            if p.get('name') == card_name:
                return _dash_done(p)
    except StepError:
        pass
    return False


def phase_quick(name, cdp, pr, log=print):
    return phase_cards(name, cdp, pr, 'dailySetPromotions', log=log)


def phase_earn(name, cdp, pr, log=print):
    return phase_cards(name, cdp, pr, 'morePromotions', log=log)
