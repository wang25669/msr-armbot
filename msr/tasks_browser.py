"""浏览器侧任务: 搜索 / 每日活动卡 / 任务打卡 (含桌面专属) / 日常任务卡。

完全适配 Next.js App Router 现代 rewards.bing.com:
- 无 X11/xdotool: 回车走 CDP Input.dispatchKeyEvent
- 现代 DOM: 结构化解析 #dailyset, #quests, #moreactivities, #streaks
- 任务打卡 (Quests/Punchcards): 支持桌面应用专属打卡与多步骤解锁
- 智能答题 (Quiz Solver): 自动识别 .btq_opt, role=radio 交互答题
- 域名自适配: cn 账号走 cn.bing.com, 全球账号走 www.bing.com
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
                  'windowsVirtualKeyCode': 13, 'nativeVirtualKeyCode': 13,
                  'text': '\r' if t == 'keyDown' else ''})


def typed_search(cdp, query, domain):
    """真人式搜索: 首页输入框 -> 逐字插入 -> 随机延迟 -> 回车 (多层防卡死与跳转保障)。"""
    navigate(cdp, f'https://{domain}/')
    # 智能轮询等待搜索框就绪 (最多等待 12 秒，杜绝跨国代理加载抖动误报“搜索框失踪”)
    deadline = time.time() + 12
    focused = False
    while time.time() < deadline:
        res = ev(cdp, """(function(){
          var el = document.querySelector('textarea[name=q], input[name=q], #sb_form_q');
          if (!el) return 'NO-INPUT';
          el.focus();
          return 'OK';
        })()""")
        if res == 'OK':
            focused = True
            break
        time.sleep(0.8)

    if not focused:
        # 如果首页实在加载不出输入框，直接导航至搜索结果页作为顶级兜底
        search_url = f'https://{domain}/search?q={urllib.parse.quote(query)}&form=QBLH'
        navigate(cdp, search_url)
        time.sleep(random.uniform(3, 5))
        return ev(cdp, 'location.href') or search_url

    cdp.call('Input.insertText', {'text': query})
    time.sleep(random.uniform(0.8, 1.5))
    press_enter(cdp)
    time.sleep(2.5)

    # 检查回车后是否成功跳转至搜索结果页
    cur_url = ev(cdp, 'location.href') or ''
    if '/search' not in cur_url:
        # 尝试点击搜索按钮或提交表单 (应对 textarea 默认不提交或输入法阻断)
        ev(cdp, """(() => {
          const btn = document.querySelector('#search_icon, #sb_form_go, button[type=submit], label[for=sb_form_go], [aria-label*="search" i]');
          if (btn) { btn.click(); return; }
          const f = document.querySelector('#sb_form, form');
          if (f) { f.submit(); return; }
        })()""")
        time.sleep(3.0)
        cur_url = ev(cdp, 'location.href') or ''

    # 终极兜底：如果依然停留在首页，直接导航到对应关键词的搜索结果页
    if '/search' not in cur_url:
        search_url = f'https://{domain}/search?q={urllib.parse.quote(query)}&form=QBLH'
        navigate(cdp, search_url)
        time.sleep(random.uniform(3, 5))
        cur_url = ev(cdp, 'location.href') or search_url

    return cur_url


def scroll_human(cdp):
    """真人滚动浏览。"""
    try:
        ev(cdp, 'window.scrollBy({top: 400, behavior: "smooth"})')
        time.sleep(random.uniform(1.5, 3.0))
        ev(cdp, 'window.scrollBy({top: 500, behavior: "smooth"})')
        time.sleep(random.uniform(1.5, 3.0))
        ev(cdp, 'window.scrollTo({top: 0, behavior: "smooth"})')
    except Exception:
        pass


def search_counters_api(pr):
    """通过 promo 属性读 PC 搜索进度。"""
    for p in (pr.get('promotions') or []):
        attrs = p.get('attributes') or {}
        tag = attrs.get('AnswerScenario.Tag') or attrs.get('Classification.Tag') or ''
        name = p.get('name') or ''
        if 'PCSearch' in tag or 'search_PC' in name or attrs.get('type') == 'search':
            try:
                prog = int(attrs.get('progress', 0))
                m = int(attrs.get('max', 0))
                complete = str(attrs.get('complete', '')).lower() == 'true'
                return {'progress': prog, 'max': m, 'complete': complete}
            except (ValueError, TypeError):
                pass
    return None


def search_counters_dom(cdp):
    """通过 dashboard #streaks 检查搜索打卡状态。"""
    try:
        res = ev(cdp, """(() => {
          const st = document.getElementById("streaks");
          if (!st) return null;
          const text = st.innerText;
          const m = text.match(/(?:搜索|search|pc\\s*search|pc)\\s*[:：]?\\s*(\\d+)\\s*\\/\\s*(\\d+)/i);
          if (m) {
            return { progress: parseInt(m[1]), max: parseInt(m[2]), complete: parseInt(m[1]) >= parseInt(m[2]) };
          }
          return null;
        })()""")
        return res
    except Exception:
        return None


def phase_search(name, cdp, pr, ch=None, budget_seconds=None, target=None, log=print, token=None, proxy=None):
    """必应 PC 搜索打卡 (按账户等级读动态上限，自适应冷板停留与防卡死熔断)。"""
    from msr import settings
    st = settings.load_settings()
    s_cfg = st.get('search', {})
    sch_cfg = st.get('schedule', {})
    fast = sch_cfg.get('fast_mode', False)

    dwell_min = 3 if fast else int(s_cfg.get('dwell_min_seconds', 180))
    dwell_max = 6 if fast else int(s_cfg.get('dwell_max_seconds', 300))
    stall_max = int(s_cfg.get('stall_max_retries', 12))
    hard_cap = int(s_cfg.get('hard_cap_attempts', 60))
    click_prob = float(s_cfg.get('click_result_probability', 0.85))
    if not budget_seconds:
        budget_seconds = int(s_cfg.get('budget_minutes', 120)) * 60

    domain = bing_domain(pr)

    # 优先检查是否已满额
    c_api = search_counters_api(pr)
    target_max = (c_api or {}).get('max', 60)
    current_prog = (c_api or {}).get('progress', 0)
    if c_api and (c_api.get('complete') or current_prog >= target_max):
        log('PC搜索已满额: %d/%d (根据促销属性)' % (current_prog, target_max))
        return '%d/%d' % (current_prog, target_max)

    log('PC搜索启动: 当前进度 %d/%d (模式=%s, 单次停留 %.1f-%.1f 秒/词)' % (
        current_prog, target_max, '极速测试[fast_mode]' if fast else '防风控真机', dwell_min, dwell_max))

    queries_en = [
        'weather forecast', 'world news today', 'python tutorial for beginners',
        'best coffee machine review', 'local events this weekend', 'sports live scores',
        'healthy dinner recipes', 'travel destinations 2026', 'top movies on netflix',
        'pop music billboard', 'artificial intelligence tech news', 'indoor gardening tips',
        'finance and investment strategies', 'best sci-fi books 2026', 'electric vehicles range',
        'space telescope images', 'nutrition facts tips', 'smart home automation',
        'architecture design trends', 'wildlife photography techniques'
    ]
    queries_zh = [
        '今日头条新闻', '天气预报查询', '最新电影票房榜', '健康养生小常识',
        '热门旅游景点推荐', '科技创新最新进展', '家常菜谱大全做法', '新能源汽车续航排名',
        '故宫博物院展览', '中国空间站最新动态', '世界名画高清赏析', '羽毛球世锦赛赛程',
        '流行音乐新歌榜', '居家收纳整理技巧', '秋季润燥养生汤', '人工智能最新技术'
    ]
    qlang = s_cfg.get('query_language', 'auto')
    if qlang == 'zh':
        queries = queries_zh
    elif qlang == 'en':
        queries = queries_en
    else:
        is_cn = (pr.get('country') or '').lower() == 'cn'
        queries = queries_zh + queries_en if is_cn else queries_en + queries_zh
    random.shuffle(queries)

    t0 = time.time()
    searches_done = 0
    stall_count = 0
    last_prog = current_prog
    consecutive_errors = 0

    while (time.time() - t0 < budget_seconds) and (searches_done < hard_cap):
        if current_prog >= target_max:
            log('PC搜索达成上限: %d/%d (尝试%d次)' % (current_prog, target_max, searches_done))
            break

        q = queries[searches_done % len(queries)] + ('' if searches_done < len(queries) else f' {searches_done}')
        searches_done += 1

        try:
            href = typed_search(cdp, q, domain)
            log('搜索[%d] 关键词: "%s" -> %s' % (searches_done, q, str(href)[:60]))
            consecutive_errors = 0

            # 初始 3-6 秒: 搜索结果页真人滚动与初读
            time.sleep(random.uniform(1.2, 2.5))
            scroll_human(cdp)

            # 模拟真人点击任意搜索结果并开启新标签阅读
            res_tab_id = None
            c_res = None
            if random.random() < click_prob and ch:
                try:
                    pick_js = """(() => {
                        const anchors = Array.from(document.querySelectorAll('#b_results .b_algo h2 a')).filter(a => a.href && a.offsetParent);
                        if (anchors.length === 0) return null;
                        const pick = anchors[Math.floor(Math.random() * Math.min(anchors.length, 6))];
                        return { href: pick.href, title: pick.innerText.replace(/\\n/g, ' ').trim() };
                    })()"""
                    pick = ev(cdp, pick_js)
                    if pick and pick.get('href'):
                        r_href = pick['href']
                        r_title = pick.get('title', '')[:25]
                        log('搜索[%d] 拟态翻阅: 开启新标签阅读搜索结果 [%s] -> %s' % (searches_done, r_title, r_href[:60]))
                        res_tab = ch.new_tab(r_href)
                        if res_tab and res_tab.get('id'):
                            res_tab_id = res_tab['id']
                            try:
                                c_res = ch.attach(res_tab)
                                time.sleep(random.uniform(2.0, 3.5))
                                scroll_human(c_res)
                            except Exception:
                                pass
                except Exception as ex:
                    log('搜索结果点击容忍: %s' % str(ex)[:60])

            # 核心节奏停留: 180-300 秒 (等待 MSR 风控冷却，fast_mode 下 3-6 秒)
            dwell = random.uniform(dwell_min, dwell_max)
            log('搜索[%d] 模拟真人深度阅读停留 %.1f 秒 (%s)...' % (
                searches_done, dwell, '在独立文章标签页' if res_tab_id else '在主搜索结果页'))
            t_start = time.time()
            while time.time() - t_start < dwell:
                chunk = min(25.0, dwell - (time.time() - t_start))
                if chunk > 0:
                    time.sleep(chunk)
                    if c_res and random.random() < 0.4:
                        try:
                            scroll_human(c_res)
                        except Exception:
                            pass
                    elapsed = time.time() - t_start
                    if dwell > 60 and elapsed < dwell - 10 and int(elapsed) % 60 < 25:
                        log('搜索[%d] 真人阅读中: 已停留 %ds/%ds...' % (searches_done, int(elapsed), int(dwell)))

            # 阅读结束，关闭结果标签页，返回 Bing 主搜索页
            if res_tab_id and ch:
                try:
                    ch.close_tab(res_tab_id)
                except Exception:
                    pass
                log('搜索[%d] 完成文章阅读，已关闭结果标签页并返回主搜索' % searches_done)

        except Exception as ex:
            consecutive_errors += 1
            log('搜索[%d] 异常: %s' % (searches_done, str(ex)[:80]))
            if consecutive_errors >= 3:
                log('搜索连续 %d 次异常，尝试重置浏览器至 Bing 首页...' % consecutive_errors)
                try:
                    navigate(cdp, f'https://{domain}')
                    time.sleep(3)
                except Exception:
                    pass
            if consecutive_errors >= 6:
                log('搜索连续 %d 次异常未恢复，提前终止搜索阶段' % consecutive_errors)
                break
            time.sleep(random.uniform(3, 6))
            continue

        # 回读积分进度
        if token:
            try:
                from msr import api
                new_pr = api.get_me(name, token, pr, proxy=proxy)
                new_c = search_counters_api(new_pr)
                if new_c:
                    current_prog = new_c.get('progress', current_prog)
                    target_max = new_c.get('max', target_max)
                    log('进度回读(API): %d/%d (已完成=%s)' % (current_prog, target_max, new_c.get('complete', False)))
                    if new_c.get('complete') or current_prog >= target_max:
                        log('PC搜索达成上限: %d/%d (尝试%d次)' % (current_prog, target_max, searches_done))
                        break
            except Exception as ex:
                log('API进度回读容忍: %s' % str(ex)[:60])
        else:
            current_prog = min(current_prog + 3, target_max)
            log('进度推算: %d/%d' % (current_prog, target_max))
            if current_prog >= target_max:
                log('PC搜索达成上限: %d/%d (尝试%d次)' % (current_prog, target_max, searches_done))
                break

        if current_prog > last_prog:
            stall_count = 0
            last_prog = current_prog
        else:
            stall_count += 1
            log('搜索计数未推进(%d/%d)，连续 %d/%d 次' % (current_prog, target_max, stall_count, stall_max))
            if stall_count >= stall_max:
                log('搜索计数连续 %d 次停滞，提前收尾(避免死磕风控)' % stall_max)
                break

    if current_prog >= target_max:
        return '%d/%d满额 (尝试%d次)' % (current_prog, target_max, searches_done)
    return '未满额:%d/%d (尝试%d次,缺%d分)' % (current_prog, target_max, searches_done, target_max - current_prog)


# ---------- 每日活动卡 (Daily Set) ----------

def dailyset_kind(card):
    """判定每日活动卡类型 (quiz / urloffer / sid / direct)。"""
    raw_url = card.get('destinationUrl') or card.get('href') or ''
    # 多层 URL 解码以解析 checkuser?ru= 包装的实际目标
    url = raw_url
    for _ in range(3):
        unquoted = urllib.parse.unquote(url)
        if unquoted == url:
            break
        url = unquoted
    lower = url.lower()
    if 'pollscenarioid' in lower or 'poll' in lower:
        return 'quiz'
    if any(k in lower for k in ('gamification_dailyset', 'rewardsquiz', 'btroid', 'dsetqu', 'form=tgrew4', 'wqoskey', 'quiz', 'trivia')):
        return 'quiz'
    m = re.search(r'filters=([^&]+)', raw_url)
    filters = urllib.parse.unquote(m.group(1)) if m else ''
    if 'BTEPOKey:"REWARDSQUIZ_DailySet_UrlOffer"' in filters:
        return 'urloffer'
    if 'IsConversation:"True"' in filters or 'WQOskey:' in filters:
        return 'quiz'
    if re.search(r'filters=sid:"[0-9a-f-]{36}"', filters, re.I):
        return 'sid'
    return 'direct'


def interact_quiz(ch, tab_id, label='', log=print):
    """在指定标签内自动完成页内交互答题与投票 (零文案, 按结构属性)。"""
    if not ch:
        return
    ws_url = f'ws://127.0.0.1:{ch.port}/devtools/page/{tab_id}'
    try:
        cdp_quiz = ch.attach({'id': tab_id, 'webSocketDebuggerUrl': ws_url})
    except Exception as ex:
        log('quiz attach失败: %s' % str(ex)[:60])
        return

    try:
        # 等待页面加载及 quiz/poll 组件异步渲染 (最多 30s)
        for _ in range(30):
            try:
                has_quiz = ev(cdp_quiz, "document.querySelector('.btq_opt, [class*=btq_opt], .btp_choice, [class*=btp_choice], [role=radio], [class*=quiz], [class*=poll], [class*=choice]') !== null")
                if has_quiz:
                    log('quiz/poll组件已渲染: %s' % label)
                    break
            except Exception:
                pass
            time.sleep(1)

        time.sleep(random.uniform(2, 4))
        # 平滑向下滚动以展现题目
        for _ in range(2):
            try:
                ev(cdp_quiz, "window.scrollBy({top: 350, behavior: 'smooth'})")
            except Exception:
                pass
            time.sleep(random.uniform(1.0, 2.0))

        find_js = """(() => {
          const sels = ['.btq_opt', '.btq_opts .acf-button-standard__link',
                        '[class*=btq_opt]', '.btp_choice', '[class*=btp_choice]',
                        '.btp_choice_wrapper', 'acf-button-standard.btp_choice',
                        '[role=radio]', '[class*=quiz] button', '[class*=option]', '[class*=choice]'];
          for (const s of sels) {
            const el = [...document.querySelectorAll(s)].find(e => e.offsetParent !== null
                        && e.getBoundingClientRect().width > 20);
            if (el) return el;
          }
          return null;
        })()"""

        is_poll = 'poll' in label.lower() or '投票' in label
        max_questions = 1 if is_poll else 6

        # 逐题点击 (通常 1-4 题，投票仅 1 题)
        for qi in range(max_questions):
            try:
                hit = ev(cdp_quiz, find_js + " !== null")
            except Exception:
                break
            if not hit:
                if qi > 0:
                    log('quiz交互: 已答%d题, 无更多选项 (%s)' % (qi, label))
                break
            try:
                human_click(cdp_quiz, find_js, label=f'quiz第{qi+1}题:{label}', close_new=True)
                log('quiz第%d题已点击 (%s)' % (qi + 1, label))
            except Exception as e:
                log('quiz交互点击容忍: %s' % str(e)[:60])
                break
            from msr import settings
            st_cfg = settings.load_settings()
            c_cfg = st_cfg.get('cards', {})
            q_min = int(c_cfg.get('quiz_step_delay_min', 3))
            q_max = int(c_cfg.get('quiz_step_delay_max', 6))
            time.sleep(random.uniform(q_min, q_max))
    finally:
        try:
            cdp_quiz.close()
        except Exception:
            pass


def phase_dailyset(name, cdp, pr, ch=None, budget_seconds=1500, log=print):
    """Daily Set 3张每日活动卡。"""
    navigate(cdp, 'https://rewards.bing.com/dashboard')
    time.sleep(random.uniform(2, 4))

    # 自动展开 #dailyset 折叠手风琴 (兼容成熟老号默认平铺展开与新手账号折叠汇总卡片)
    def _ensure_expanded():
        try:
            ev(cdp, """(() => {
              const ds = document.getElementById("dailyset");
              if (!ds) return;
              const trigger = ds.querySelector("button[slot='trigger'][aria-expanded='false'], [class*=Disclosure]:not([data-expanded='true']) button[aria-expanded='false']");
              if (trigger) trigger.click();
            })()""")
        except Exception:
            pass

    _ensure_expanded()

    # 智能轮询等待 #dailyset 容器及卡片渲染完成 (应对 Next.js SPA 水合与网络延迟，最多等待 25 秒)
    cards = []
    deadline = time.time() + 25
    while time.time() < deadline:
        _ensure_expanded()
        cards = ev(cdp, """(() => {
          const ds = document.getElementById("dailyset");
          if (!ds) return [];
          const links = Array.from(ds.querySelectorAll("a[href]"));
          if (links.length === 0) return [];
          return links.map((a, idx) => ({
            idx: idx,
            href: a.href,
            text: a.innerText.replace(/\\n/g, ' '),
            title: a.querySelector("h3")?.innerText || a.innerText.split("\\n")[0].slice(0, 30),
            isDone: /已完成|complete|completed/i.test(a.innerText) || 
                    /bg-statusSuccess|check/i.test(a.innerHTML) || 
                    /已完成|complete|completed/i.test(a.getAttribute("aria-label") || "")
          }));
        })()""") or []
        if cards:
            break
        time.sleep(1.0)

    if not cards:
        # 如果超时仍未出现，尝试重新导航刷新一次作为兜底
        log('未发现每日活动区域 (#dailyset)，尝试重新导航刷新二次探测...')
        navigate(cdp, 'https://rewards.bing.com/dashboard')
        time.sleep(random.uniform(3, 5))
        _ensure_expanded()
        cards = ev(cdp, """(() => {
          const ds = document.getElementById("dailyset");
          if (!ds) return [];
          return Array.from(ds.querySelectorAll("a[href]")).map((a, idx) => ({
            idx: idx,
            href: a.href,
            text: a.innerText.replace(/\\n/g, ' '),
            title: a.querySelector("h3")?.innerText || a.innerText.split("\\n")[0].slice(0, 30),
            isDone: /已完成|complete|completed/i.test(a.innerText) || 
                    /bg-statusSuccess|check/i.test(a.innerHTML) || 
                    /已完成|complete|completed/i.test(a.getAttribute("aria-label") || "")
          }));
        })()""") or []

    if not cards:
        log('未发现每日活动区域 (#dailyset)')
        return '失败:未发现每日活动区域'

    todo = [c for c in cards if not c.get('isDone')]
    if not todo:
        log('今日每日活动已全部完成 (3/3)')
        return '3/3已完成'

    done_count = 0
    for card in todo:
        idx = card['idx']
        title = card['title']
        href = card['href']
        kind = dailyset_kind({'destinationUrl': href})
        log('每日活动卡片: %s [%s] -> %s' % (title, kind, href[:60]))

        _ensure_expanded()
        selector = f'document.querySelectorAll("#dailyset a[href]")[{idx}]'
        new_tabs = human_click(cdp, selector, label=title[:15], close_new=False)

        if new_tabs and ch:
            target_tid = new_tabs[0]
            try:
                from msr import settings
                st_card = settings.load_settings()
                fast_mode = bool(st_card.get('schedule', {}).get('fast_mode'))
                if kind == 'quiz':
                    interact_quiz(ch, target_tid, label=title, log=log)
                    wait = random.uniform(3, 6) if fast_mode else random.uniform(25, 45)
                    log('%s 答题后停留 %.1f 秒等待结算' % (title, wait))
                    time.sleep(wait)
                else:
                    w_min = 3 if fast_mode else int(st_card.get('cards', {}).get('attribution_wait_min', 60))
                    w_max = 6 if fast_mode else int(st_card.get('cards', {}).get('attribution_wait_max', 120))
                    wait = random.uniform(w_min, w_max)
                    log('%s 停留 %.1f 秒等待归因' % (title, wait))
                    time.sleep(wait)
            finally:
                for tid in new_tabs:
                    try:
                        ch.close_tab(tid)
                    except Exception:
                        pass
        else:
            time.sleep(random.uniform(10, 20))

        done_count += 1

    navigate(cdp, 'https://rewards.bing.com/dashboard')
    time.sleep(random.uniform(3.5, 5.0))
    # 轮询等待 #dailyset 渲染就绪
    for _ in range(8):
        if ev(cdp, 'document.getElementById("dailyset") !== null'):
            break
        time.sleep(1.0)
    time.sleep(2.0)
    _ensure_expanded()

    remaining_todo = ev(cdp, """(() => {
      const ds = document.getElementById("dailyset");
      if (!ds) return 0;
      const links = Array.from(ds.querySelectorAll("a[href]"));
      return links.filter(a => {
        const isDone = /已完成|complete|completed/i.test(a.innerText) || 
                       /bg-statusSuccess|check/i.test(a.innerHTML) || 
                       /已完成|complete|completed/i.test(a.getAttribute("aria-label") || "");
        return !isDone;
      }).length;
    })()""") or 0

    if remaining_todo == 0 or (done_count >= len(cards) and remaining_todo <= 1):
        return '3/3已完成'
    return '部分完成:处理%d张(剩%d张待完成)' % (done_count, remaining_todo)


phase_quick = phase_dailyset


def resolve_quest_subtask_action(step_info, q_name):
    """根据任务卡片固有通用属性(URL结构、Query参数、卡片元数据)自适应解析动作类型。

    绝不针对任何特定节日、主题硬编码关键词。
    通用操作分流:
    1. 'quiz': 具有官方测验标识 (filters=BTBQ/BTB/REWARDSQUIZ 或路径含 quiz)
    2. 'search_direct': 具有搜索终结点且已有预设关键词 q
    3. 'search_semantic': 具有搜索终结点但无 q (开放式搜索任务)，从卡片 title 元数据提取核心题干搜索
    4. 'landing': 常规网页/壁纸/活动介绍落地页，模拟自然浏览与停留
    """
    href = step_info.get('href') or ''
    title = (step_info.get('title') or '').strip()
    card_text = (step_info.get('cardText') or '').strip()
    has_query = step_info.get('hasQuery', False)

    # 1. 测验/竞答类属性判定 (Quiz / Trivia)
    if any(k in href for k in ('REWARDSQUIZ', 'BTBQ', 'BTB')) or '/quiz' in href.lower():
        return {'action': 'quiz', 'search_term': None, 'reason': 'URL携带官方测验标识(REWARDSQUIZ/BTBQ/quiz)'}

    # 2. 搜索类属性判定 (Search，含新手打卡 Onboarding / Search now 引导)
    parsed = urllib.parse.urlparse(href)
    is_bing = 'bing.com' in parsed.netloc
    is_search_path = parsed.path.rstrip('/') == '/search'
    is_search_intent = any(k in title.lower() or k in card_text.lower() for k in (
        '搜索', 'search', 'search now', 'keep searching', '立即搜索', '开始搜索'
    ))

    if (is_bing and is_search_path) or (is_bing and is_search_intent):
        if has_query:
            return {'action': 'search_direct', 'search_term': None, 'reason': 'URL已携带官方预置搜索词'}
        # 开放式搜索任务: 从卡片元数据中的 title (标题) 净化提取主题词
        clean_term = title
        for prefix in (
            'Bing 有您的', 'Bing有您的', '通过', '使用', '在您的桌面上', '在桌面上', '探索', '查找', '关于',
            'Search with Bing on your desktop', 'Search with Bing', 'Bing has your', 'Use Bing',
            'Search now', 'Keep searching', '立即搜索', '开始搜索',
            'Explore', 'Find', 'Discover', 'About'
        ):
            clean_term = re.sub(re.escape(prefix), '', clean_term, flags=re.IGNORECASE)
        clean_term = re.sub(r'[^\w\s\u4e00-\u9fa5]', ' ', clean_term).strip()

        if 2 <= len(clean_term) <= 30:
            chosen_term = clean_term
        else:
            matches = re.findall(r'[\u4e00-\u9fa5a-zA-Z0-9]{2,12}', card_text)
            chosen_term = matches[0] if (matches and len(matches[0]) >= 2) else 'trending news'
        return {'action': 'search_semantic', 'search_term': chosen_term, 'reason': f'搜索打卡任务(含新手搜索)，从卡片提取核心词: "{chosen_term}"'}

    # 3. 常规落地页属性 (Landing Page / Wallpaper / Promotion)
    return {'action': 'landing', 'search_term': None, 'reason': '常规展示/体验类落地页'}


# ---------- 任务打卡 (Quests / Punchcards，含桌面专属打卡) ----------

def phase_quests(name, cdp, pr, ch=None, budget_seconds=1500, log=print):
    """任务打卡(Quests / Punchcards，含桌面专属打卡)。"""
    navigate(cdp, 'https://rewards.bing.com/earn')
    time.sleep(random.uniform(2, 4))

    # 智能轮询等待 #quests 容器及链接渲染完成 (最多等待 20 秒)
    quests = []
    deadline = time.time() + 20
    while time.time() < deadline:
        quests = ev(cdp, """(() => {
          const qs = document.getElementById("quests");
          if (!qs) return [];
          const links = Array.from(qs.querySelectorAll("a[href*='/earn/quest/']"));
          return links.map(a => {
            const text = a.innerText.replace(/\\n/g, ' ');
            return {
              href: a.href,
              text: text,
              isDone: /已完成|completed?/i.test(text) || /\\b(\\d+)\\s*\\/\\s*\\1\\s*(?:个任务|tasks?)/i.test(text) || /\\b(\\d+)\\s+of\\s+\\1\\s+tasks?\\s+completed/i.test(text)
            };
          });
        })()""") or []
        if quests:
            break
        time.sleep(1.0)

    if not quests:
        log('今日未发现打卡任务 (#quests)')
        return '无打卡任务'

    active_quests = [q for q in quests if not q.get('isDone')]
    log('发现 %d 个打卡任务 (待处理 %d 个)' % (len(quests), len(active_quests)))

    total_steps_done = 0
    t0 = time.time()

    for q in active_quests:
        if time.time() - t0 > budget_seconds:
            break
        q_url = q['href']
        q_name = q_url.split('/')[-1]
        log('进入打卡任务: %s' % q_name)
        seen_step_urls = set()

        # 循环完成该任务中今天可执行的所有子步骤
        for step_attempt in range(8):
            navigate(cdp, q_url)
            time.sleep(random.uniform(2, 4))

            # 检测可点击的活跃步骤
            step_info = ev(cdp, """(() => {
              const anchors = Array.from(document.querySelectorAll("main a[href], main button")).filter(a => {
                const row = a.closest("div.flex.flex-row, div.flex.flex-col.gap-3, div[class*=rounded-2xl]") || a.parentElement;
                if (row && (row.querySelector("div[class*=bg-statusSuccess], div[class*=statusSuccessRewardsBg], svg[class*=text-statusSuccess], svg[aria-label*=complete i], svg[aria-label*=已完成]") || /已完成|completed/i.test(row.innerText))) {
                  return false;
                }

                const t = a.innerText || "";
                const h = a.href || "";
                if (h.includes("/dashboard") || h.includes("/earn") || h.includes("/redeem") || h.includes("/pointsbreakdown") || h.includes("terms") || h.includes("privacy")) return false;
                return true;
              });
              if (anchors.length === 0) return null;
              const target = anchors[0];
              const p = target.closest("div.flex.flex-col.gap-3, div.flex.flex-row") || target.parentElement;
              const pTitle = p?.querySelector("h2, h3, h4, [class*=title]")?.innerText?.replace(/\\n/g, ' ')?.trim() || "";
              const pText = p?.innerText?.replace(/\\n/g, ' ')?.trim() || "";

              let href = target.href || "";
              if (href.startsWith("ms-search://")) {
                let q = "";
                try {
                  const u = new URL(href);
                  q = u.searchParams.get("q") || "";
                } catch(e) {
                  const m = href.match(/[?&]q=([^&]+)/);
                  if (m) q = decodeURIComponent(m[1]);
                }
                href = "https://www.bing.com/search?q=" + encodeURIComponent(q || "today news");
                target.href = href;
                target.target = "_blank";
              }
              let hasQ = false;
              try {
                const u = new URL(href, location.href);
                hasQ = !!u.searchParams.get("q");
              } catch(e) {}

              const multiDayMatch = pText.match(/(\\d+\\s*\\/\\s*\\d+\\s*(?:天已完成|days?(?:\\s+completed)?))/i) || pText.match(/(\\d+\\s*of\\s*\\d+\\s*days?(?:\\s+completed)?)/i);

              return {
                text: target.innerText.replace(/\\n/g, ' ').trim() || pTitle,
                title: pTitle,
                href: href,
                hasQuery: hasQ,
                cardText: pText,
                isMultiDay: !!multiDayMatch,
                multiDayProg: multiDayMatch ? multiDayMatch[1] : null
              };
            })()""")

            if not step_info:
                log('%s 今日无更多待做步骤' % q_name)
                break

            step_text = step_info['text']
            step_href = step_info['href']
            if step_info.get('isMultiDay'):
                prog_str = step_info.get('multiDayProg') or '跨日任务'
                if step_href in seen_step_urls:
                    log('%s 跨日累计步骤 (%s) 今日打卡搜索已完成，MSR 规则要求间隔24小时次日继续，转入下一打卡' % (q_name, prog_str))
                    break
                log('%s 发现跨日累计步骤: %s (需每日打卡推进)' % (q_name, prog_str))
            elif step_href in seen_step_urls:
                log('%s 步骤已处理过(需等待冷却或次日刷新)，进入下一打卡' % q_name)
                break
            seen_step_urls.add(step_href)

            log('执行步骤: %s -> %s' % (step_text[:30], step_href[:60]))

            click_selector = """(() => {
              const el = Array.from(document.querySelectorAll("main a[href], main button")).find(a => {
                const row = a.closest("div.flex.flex-row, div.flex.flex-col.gap-3, div[class*=rounded-2xl]") || a.parentElement;
                if (row && (row.querySelector("div[class*=bg-statusSuccess], div[class*=statusSuccessRewardsBg], svg[class*=text-statusSuccess], svg[aria-label*=complete i], svg[aria-label*=已完成]") || /已完成|completed/i.test(row.innerText))) {
                  return false;
                }
                const t = a.innerText || "";
                const h = a.href || "";
                if (h.includes("/dashboard") || h.includes("/earn") || h.includes("/redeem") || h.includes("/pointsbreakdown") || h.includes("terms") || h.includes("privacy")) return false;
                return true;
              });
              if (el && el.href && el.href.startsWith("ms-search://")) {
                let q = "";
                try {
                  const u = new URL(el.href);
                  q = u.searchParams.get("q") || "";
                } catch(e) {
                  const m = el.href.match(/[?&]q=([^&]+)/);
                  if (m) q = decodeURIComponent(m[1]);
                }
                el.href = "https://www.bing.com/search?q=" + encodeURIComponent(q || "today news");
                el.target = "_blank";
              }
              return el;
            })()"""

            new_tabs = human_click(cdp, click_selector, label=step_text[:15], close_new=False)

            if new_tabs and ch:
                target_tid = new_tabs[0]
                try:
                    action_info = resolve_quest_subtask_action(step_info, q_name)
                    act = action_info['action']
                    log('%s 步骤属性解析: [%s] -> %s (%s)' % (q_name, act, step_text[:25], action_info['reason']))

                    if act == 'quiz':
                        interact_quiz(ch, target_tid, label=step_text, log=log)
                    elif act == 'search_semantic':
                        term = action_info['search_term']
                        try:
                            c2 = ch.attach({'id': target_tid, 'webSocketDebuggerUrl': f'ws://127.0.0.1:{ch.port}/devtools/page/{target_tid}'})
                            time.sleep(2)
                            focused = ev(c2, """(() => {
                              const i = document.querySelector('#sb_form_q, textarea[name=q], input[name=q]');
                              if (i) { i.focus(); return 'OK'; }
                              return null;
                            })()""")
                            if focused:
                                log('%s 在新标签输入任务主题词搜索: "%s"' % (q_name, term))
                                c2.call('Input.insertText', {'text': term})
                                time.sleep(random.uniform(1.0, 1.8))
                                press_enter(c2)
                                time.sleep(2.5)
                                cur_h = ev(c2, 'location.href') or ''
                                if '/search' not in cur_h:
                                    ev(c2, """(() => {
                                      const btn = document.querySelector('#search_icon, #sb_form_go, button[type=submit], label[for=sb_form_go]');
                                      if (btn) btn.click();
                                    })()""")
                                    time.sleep(2.5)
                                    cur_h = ev(c2, 'location.href') or ''
                                if '/search' not in cur_h:
                                    navigate(c2, f'https://www.bing.com/search?q={urllib.parse.quote(term)}&form=QBLH')
                                    time.sleep(3)
                                scroll_human(c2)
                            c2.close()
                        except Exception as ex:
                            log('开放式搜索执行容忍: %s' % str(ex)[:60])
                    else:
                        # search_direct 或 landing: 模拟自然视线浏览与平滑滚动
                        try:
                            c2 = ch.attach({'id': target_tid, 'webSocketDebuggerUrl': f'ws://127.0.0.1:{ch.port}/devtools/page/{target_tid}'})
                            time.sleep(2)
                            scroll_human(c2)
                            c2.close()
                        except Exception:
                            pass

                    # 从配置读取停留归因等待时间 (默认 60~120 秒，快速模式 3~6 秒)
                    from msr import settings
                    st_card = settings.load_settings()
                    fast_mode = bool(st_card.get('schedule', {}).get('fast_mode'))
                    w_min = 3 if fast_mode else int(st_card.get('cards', {}).get('attribution_wait_min', 60))
                    w_max = 6 if fast_mode else int(st_card.get('cards', {}).get('attribution_wait_max', 120))
                    w = random.uniform(w_min, w_max)
                    log('%s 步骤页面停留 %.1f 秒等待服务器归因积分' % (q_name, w))
                    time.sleep(w)
                finally:
                    for tid in new_tabs:
                        try:
                            ch.close_tab(tid)
                        except Exception:
                            pass
            else:
                time.sleep(random.uniform(5, 8))

            total_steps_done += 1
            time.sleep(random.uniform(2, 4))

    return '处理%d个打卡步骤' % total_steps_done


# ---------- 日常任务卡 (More Activities) ----------

def phase_moreactivities(name, cdp, pr, ch=None, budget_seconds=1500, log=print):
    """日常任务(More Activities 更多活动卡)。"""
    navigate(cdp, 'https://rewards.bing.com/earn')
    time.sleep(random.uniform(2, 4))

    def _ensure_ma_expanded():
        try:
            ev(cdp, """(() => {
              const ma = document.getElementById("moreactivities");
              if (!ma) return;
              const trigger = ma.querySelector("button[slot='trigger'][aria-expanded='false'], [class*=Disclosure]:not([data-expanded='true']) button[aria-expanded='false']");
              if (trigger) trigger.click();
            })()""")
        except Exception:
            pass

    _ensure_ma_expanded()

    # 智能轮询等待 #moreactivities 容器渲染 (最多等待 20 秒)
    deadline = time.time() + 20
    while time.time() < deadline:
        _ensure_ma_expanded()
        has_ma = ev(cdp, 'document.getElementById("moreactivities") !== null')
        if has_ma:
            break
        time.sleep(1.0)

    cards = ev(cdp, """(() => {
      const ma = document.getElementById("moreactivities");
      if (!ma) return [];
      return Array.from(ma.querySelectorAll("a[href]")).map((a, idx) => ({
        idx: idx,
        href: a.href,
        text: a.innerText.replace(/\\n/g, ' '),
        isRedeem: a.href.includes("/redeem/"),
        isDone: /已完成|complete|completed/i.test(a.innerText) || 
                /bg-statusSuccess|check/i.test(a.innerHTML) || 
                /已完成|complete|completed/i.test(a.getAttribute("aria-label") || "")
      })).filter(c => !c.isRedeem && !c.isDone && (c.text.includes("+") || c.text.includes("分") || /\\b\\d+\\s*(?:points|pts)\\b/i.test(c.text)));
    })()""") or []

    if not cards:
        log('今日日常任务无待完成卡片')
        return '今日无日常任务(跳过)'

    done_count = 0
    for card in cards:
        idx = card['idx']
        text = card['text']
        href = card['href']
        log('日常任务卡片: %s -> %s' % (text[:30], href[:60]))

        _ensure_ma_expanded()
        selector = f'document.querySelectorAll("#moreactivities a[href]")[{idx}]'
        new_tabs = human_click(cdp, selector, label=text[:15], close_new=False)
        from msr import settings
        st = settings.load_settings()
        fast = st.get('schedule', {}).get('fast_mode', False)
        wmin = 3 if fast else int(st.get('cards', {}).get('attribution_wait_min', 60))
        wmax = 6 if fast else int(st.get('cards', {}).get('attribution_wait_max', 120))
        if new_tabs and ch:
            try:
                time.sleep(random.uniform(wmin, wmax))
            finally:
                for tid in new_tabs:
                    try:
                        ch.close_tab(tid)
                    except Exception:
                        pass
        else:
            time.sleep(random.uniform(min(wmin, 10), min(wmax, 20)))
        done_count += 1

    navigate(cdp, 'https://rewards.bing.com/earn')
    time.sleep(random.uniform(3.5, 5.0))
    for _ in range(8):
        if ev(cdp, 'document.getElementById("moreactivities") !== null'):
            break
        time.sleep(1.0)
    time.sleep(2.0)
    _ensure_ma_expanded()

    remaining_ma = ev(cdp, """(() => {
      const ma = document.getElementById("moreactivities");
      if (!ma) return 0;
      return Array.from(ma.querySelectorAll("a[href]")).filter(a => {
        const text = a.innerText.replace(/\\n/g, ' ');
        const href = a.href || '';
        const isRedeem = href.includes("/redeem/");
        const isSpecialProtocol = !href.startsWith('http://') && !href.startsWith('https://');
        const isDone = /已完成|complete|completed/i.test(a.innerText) || 
                       /bg-statusSuccess|check/i.test(a.innerHTML) || 
                       /已完成|complete|completed/i.test(a.getAttribute("aria-label") || "");
        const hasPoints = text.includes("+") || text.includes("分") || /\\b\\d+\\s*(?:points|pts)\\b/i.test(text);
        return !isRedeem && !isSpecialProtocol && !isDone && hasPoints;
      }).length;
    })()""") or 0
    if remaining_ma == 0 or (done_count >= len(cards) and remaining_ma <= 1):
        return '日常任务已全部完成(共%d张)' % done_count
    return '部分完成:处理%d张(剩%d张待做)' % (done_count, remaining_ma)


phase_earn = phase_moreactivities


def phase_claim(name, cdp, pr, ch=None, log=print):
    """检查并领取待领取积分 (两级抽屉精准交互 + 真实余额回读核销)。"""
    dash_url = 'https://rewards.bing.com/dashboard'
    log('开始检查是否有可领取积分...')
    navigate(cdp, dash_url)
    time.sleep(random.uniform(3.5, 5.0))

    # 1. 读当前可用余额作为基准
    bal_before = ev(cdp, '''(() => {
      const t = document.body.innerText;
      const m = t.match(/(?:可用积分|Available points|balance)[^\\d]*([\\d,]+)/i) || t.match(/([\\d,]+)\\s*(?:可用积分|points)/i);
      return m ? parseInt(m[1].replace(/,/g, ''), 10) : null;
    })()''')

    # 2. 定位外层待领取卡片/按钮 (中英文适配，如 "可领取 6 领取", "Ready to claim 15 Claim")
    claim_card_js = '''(() => {
      const btns = Array.from(document.querySelectorAll('button, [role="button"], a'));
      return btns.find(b => {
        const t = (b.innerText || '').trim().replace(/\\s+/g, ' ');
        return (/可领取|待领取|ready to claim|claimable/i.test(t) || (/^claim\\b/i.test(t) && /\\d+/.test(t))) &&
               /\\d+/.test(t) && !/已领取/i.test(t);
      }) || null;
    })()'''

    has_card = ev(cdp, f"!!({claim_card_js})")
    if not has_card:
        # 也尝试 earn 页面
        navigate(cdp, 'https://rewards.bing.com/earn')
        time.sleep(random.uniform(2.5, 3.5))
        has_card = ev(cdp, f"!!({claim_card_js})")

    if not has_card:
        log('今日无可领取积分 (未发现待领取卡片), 跳过')
        return '无可领取积分(跳过)'

    card_text = ev(cdp, f"({claim_card_js}) ? ({claim_card_js}).innerText.replace(/\\s+/g, ' ').trim() : ''")
    m_pts = re.search(r'(\d+)', card_text)
    total_claimable = int(m_pts.group(1)) if m_pts else 0
    log(f'检测到待领取卡片: "{card_text}" (约 {total_claimable} 分)')

    # 3. 第一步：点击外层卡片展开 Dialog 抽屉 (单次精准点击，避免连击导致抽屉重新闭合)
    log('正在点击外层待领取卡片以展开领取抽屉...')
    opened = False
    try:
        human_click(cdp, claim_card_js, label='待领取卡片', close_new=False)
        time.sleep(random.uniform(2.0, 3.0))
        opened = ev(cdp, '!!document.querySelector(\'[role="dialog"], section[role="dialog"]\')')
    except Exception:
        pass

    if not opened:
        ev(cdp, f"(() => {{ const b = ({claim_card_js}); if (b) b.click(); }})()")
        time.sleep(random.uniform(2.5, 3.5))

    # 4. 第二步：在抽屉内部点击二次确认按钮 (例如 "6 待领取 领取积分" / "15 Pending Claim points")
    confirm_btn_js = '''(() => {
      const d = document.querySelector('[role="dialog"], section[role="dialog"], [role="alertdialog"], .modal');
      const scope = d ? Array.from(d.querySelectorAll('button, a, [role="button"]')) : Array.from(document.querySelectorAll('button, a'));
      return scope.find(el => {
        const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
        return (
          t.includes('领取积分') ||
          /claim\\s*points/i.test(t) ||
          /claim\\s*now/i.test(t) ||
          /\\d+\\s*(?:待领取|pending)\\s*(?:领取积分|claim)/i.test(t) ||
          /^(?:领取|claim|collect)$/i.test(t)
        );
      }) || null;
    })()'''

    has_confirm = ev(cdp, f"!!({confirm_btn_js})")
    if not has_confirm:
        time.sleep(2.0)
        has_confirm = ev(cdp, f"!!({confirm_btn_js})")

    if not has_confirm:
        log('未在抽屉内找到可执行领取的确认按钮 (积分可能处于锁定期或待满足条件)')
        ev(cdp, "(() => { const c = document.querySelector('[role=\"dialog\"] button, section[role=\"dialog\"] button'); if (c) c.click(); })()")
        return f'检测到{total_claimable}分(待存入锁定中)' if total_claimable > 0 else '待存入锁定中'

    c_text = ev(cdp, f"({confirm_btn_js}) ? ({confirm_btn_js}).innerText.replace(/\\s+/g, ' ').trim() : ''")
    log(f'抽屉内确认领取按钮已就绪: "{c_text}", 正在触发领取结算...')

    clicked_confirm = False
    try:
        human_click(cdp, confirm_btn_js, label='确认领取按钮', close_new=False)
        clicked_confirm = True
    except Exception:
        pass

    if not clicked_confirm:
        ev(cdp, f"(() => {{ const b = ({confirm_btn_js}); if (b) b.click(); }})()")
    time.sleep(random.uniform(4.0, 6.0))

    # 尝试关闭抽屉
    ev(cdp, "(() => { const c = document.querySelector('[role=\"dialog\"] button, section[role=\"dialog\"] button'); if (c) c.click(); })()")
    time.sleep(1.0)

    # 5. 第三步：刷新页面并严格回读可用余额核销
    navigate(cdp, dash_url)
    time.sleep(random.uniform(3.5, 5.0))
    bal_after = ev(cdp, '''(() => {
      const t = document.body.innerText;
      const m = t.match(/(?:可用积分|Available points|balance)[^\\d]*([\\d,]+)/i) || t.match(/([\\d,]+)\\s*(?:可用积分|points)/i);
      return m ? parseInt(m[1].replace(/,/g, ''), 10) : null;
    })()''')

    if bal_before is not None and bal_after is not None and bal_after > bal_before:
        gained = bal_after - bal_before
        log(f'积分领取成功已入账！余额从 {bal_before} 增至 {bal_after} (+{gained}分)')
        return f'成功领取{gained}分(余额:{bal_after})'

    card_still_there = ev(cdp, f"!!({claim_card_js})")
    if not card_still_there:
        log(f'已提交领取请求，卡片已核销 (预计 +{total_claimable}分，等待入账)')
        return f'已提交领取{total_claimable}分(待入账)'

    return f'领取结算中(约{total_claimable}分)'


def auto_activate_streaks(cdp, ch=None, log=print):
    """自动扫描并激活 rewards 页面上所有待激活的连续打卡任务 (Streak)。
    
    兼容 Visual Search Streak, Edge Browsing Streak, Bing Search Streak, Bing App Streak 等。
    若页面卡片显示“如何激活”/“How to activate”/“Activate streak”，自动展开抽屉并点击激活。
    """
    find_streaks_js = '''(() => {
      const results = [];
      const btns = Array.from(document.querySelectorAll('button, div[role="button"], a'));
      for (const b of btns) {
        const t = (b.innerText || '').toLowerCase();
        const aria = (b.getAttribute('aria-label') || '').toLowerCase();
        const pText = (b.parentElement ? b.parentElement.innerText || '' : '').toLowerCase();
        const combined = t + ' ' + aria + ' ' + pText;
        if (/如何激活|how to activate|activate\\s*streak|activate\\s*now|立即激活/i.test(combined) || 
            (/activate|激活/i.test(t) && /streak|打卡|edge|visual|search|browsing/i.test(combined))) {
          const cardTitle = b.querySelector('p, h2, h3, div')?.innerText || b.innerText.slice(0, 30);
          results.push({
            id: b.id,
            title: cardTitle.replace(/\\n/g, ' ').trim(),
            fullText: b.innerText.replace(/\\n/g, ' ').trim()
          });
        }
      }
      return results;
    })()'''

    navigate(cdp, 'https://rewards.bing.com/earn')
    time.sleep(random.uniform(2.5, 4.0))
    unactivated = ev(cdp, find_streaks_js) or []

    if not unactivated:
        # 回退至 dashboard 页面进行二次扫描
        navigate(cdp, 'https://rewards.bing.com/dashboard')
        time.sleep(random.uniform(2.5, 3.5))
        unactivated = ev(cdp, find_streaks_js) or []

    if not unactivated:
        return 0

    log('[Streak 自动激活] 发现 %d 个待激活打卡任务: %s' % (len(unactivated), [u['title'] for u in unactivated]))
    activated_count = 0

    for s in unactivated:
        title = s['title']
        log('>>> 正在激活连续打卡任务: %s...' % title)
        # 点击展开卡片抽屉
        click_card_js = ('''(() => {
          const btns = Array.from(document.querySelectorAll('button, div[role="button"], a'));
          const btn = btns.find(b => (b.innerText || '').includes(%s) || (b.id && b.id === %s));
          if (btn) { btn.click(); return true; }
          return false;
        })()''' % (json.dumps(title), json.dumps(s.get('id') or '')))
        
        opened = ev(cdp, click_card_js)
        time.sleep(random.uniform(1.8, 2.8))

        act_target_js = '''(() => {
          const dialog = document.querySelector('[role="dialog"]');
          if (!dialog) return null;
          const cand = Array.from(dialog.querySelectorAll('a, button')).find(el => {
            const t = (el.innerText || '').toLowerCase();
            const h = (el.getAttribute('href') || '').toLowerCase();
            return t.includes('激活') || t.includes('activate') || h.includes('features=vsstreak') || h.includes('features=') || h.includes('streak');
          });
          if (cand) {
            return {tag: cand.tagName, href: cand.href || null, text: (cand.innerText || '').trim()};
          }
          return null;
        })()'''
        target = ev(cdp, act_target_js)
        if target:
            log('检测到激活操作: %s -> %s' % (target.get('text'), target.get('href') or '按钮'))
            if target.get('href') and ch:
                try:
                    new_tab = ch.new_tab(target['href'])
                    time.sleep(random.uniform(4.0, 6.0))
                    ch.close_tab(new_tab.get('id'))
                    activated_count += 1
                    log('已成功激活 Streak: %s' % title)
                except Exception as ex:
                    log('激活访问异常: %s' % ex)
            else:
                # 点击激活按钮
                try:
                    ev(cdp, '''(() => {
                      const dialog = document.querySelector('[role="dialog"]');
                      if (!dialog) return;
                      const cand = Array.from(dialog.querySelectorAll('button, a')).find(el => {
                        const t = (el.innerText || '').toLowerCase();
                        return t.includes('激活') || t.includes('activate');
                      });
                      if (cand) cand.click();
                    })()''')
                    time.sleep(3.0)
                    activated_count += 1
                    log('已成功点击激活按钮: %s' % title)
                except Exception as ex:
                    log('点击激活按钮异常: %s' % ex)
            
            # 关闭抽屉
            ev(cdp, '''(() => {
              const dialog = document.querySelector('[role="dialog"]');
              if (!dialog) return;
              const cls = dialog.querySelector('button[aria-label*="关闭" i], button[aria-label*="close" i], button.group\\\\/ctrl');
              if (cls) cls.click();
            })()''')
            time.sleep(1.0)
        else:
            log('未能定位到抽屉激活按钮或该卡片已激活')

    return activated_count

