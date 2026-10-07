"""浏览器侧任务: 搜索 / 每日活动卡 / 日常任务卡。

从现网 bing_daily_v2.py phase2/phase1/phase5 移植, 关键差异:
- 无 X11/xdotool: 回车走 CDP Input.dispatchKeyEvent(现网主路径本就不依赖 X)
- headless 下无需抢前台(CDP 直连目标标签)
- 域名按 probe 结果决定(cn 账号 -> cn.bing.com)
"""
import random
import re
import time
import urllib.parse

from msr.browser import ev, navigate, StepError


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


def phase_cards(name, cdp, pr, source, budget_seconds=1500, log=print,
                human_visit=None, max_tries=2):
    """phase1(每日活动) / phase5(日常任务) 通用: 点击未完成卡片。

    human_visit(tab_cdp, card): 访问卡片后的停留逻辑(direct/sid 滚动+停留;
    quiz 由调用方注入答题逻辑)。
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
            log(f'{source}卡: {n} [{kind}] 第{tried[n]}次')
            try:
                navigate(cdp, card['destinationUrl'])
                if kind in ('direct', 'sid', 'urloffer'):
                    scroll_human(cdp)
                    time.sleep(random.uniform(8, 15))
                else:  # quiz: 交给注入的答题逻辑, 无则标记跳过
                    if human_visit is None:
                        log(f'{n} quiz 无答题逻辑, 跳过')
                    else:
                        human_visit(cdp, card)
                done_flag = _check_done(cdp, source, key, n, domain)
                if done_flag:
                    done.append(n)
                    log(f'{n} ✓')
                elif tried[n] >= max_tries:
                    log(f'{n} {tried[n]}次未落库, 放弃')
                else:
                    log(f'{n} 没落库, 稍后重试')
                    time.sleep(random.uniform(15, 30))
            except StepError as ex:
                log(f'{n} 异常: {str(ex)[:80]}')
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
