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
    """真人式搜索: 首页输入框 -> 逐字插入 -> 随机延迟 -> 回车。"""
    navigate(cdp, f'https://{domain}/')
    time.sleep(random.uniform(1.5, 2.5))
    focused = ev(cdp, """(function(){
      var el = document.querySelector('textarea[name=q], input[name=q], #sb_form_q');
      if (!el) return 'NO-INPUT';
      el.focus();
      return 'OK';
    })()""")
    if focused != 'OK':
        raise StepError('搜索框失踪')
    cdp.call('Input.insertText', {'text': query})
    time.sleep(random.uniform(1.0, 2.0))
    press_enter(cdp)
    time.sleep(random.uniform(4, 7))
    return ev(cdp, 'location.href')


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
          const m = text.match(/搜索:\\s*(\\d+)\\s*\\/\\s*(\\d+)/);
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

            # 核心节奏停留: 180-300 秒 (等待微软风控冷却，fast_mode 下 3-6 秒)
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

    return '%d/%d (尝试%d次)' % (current_prog, target_max, searches_done)


# ---------- 每日活动卡 (Daily Set) ----------

def dailyset_kind(card):
    """判定每日活动卡类型 (quiz / urloffer / sid / direct)。"""
    url = card.get('destinationUrl') or ''
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


def interact_quiz(ch, tab_id, label='', log=print):
    """在指定标签内自动完成页内交互答题 (零文案, 按结构属性)。"""
    if not ch:
        return
    ws_url = f'ws://127.0.0.1:{ch.port}/devtools/page/{tab_id}'
    try:
        cdp_quiz = ch.attach({'id': tab_id, 'webSocketDebuggerUrl': ws_url})
    except Exception as ex:
        log('quiz attach失败: %s' % str(ex)[:60])
        return

    try:
        # 等待页面加载及 quiz 组件异步渲染 (最多 30s)
        for _ in range(30):
            try:
                has_quiz = ev(cdp_quiz, "document.querySelector('.btq_opt, [class*=btq_opt], [role=radio], [class*=quiz]') !== null")
                if has_quiz:
                    log('quiz组件已渲染: %s' % label)
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
                        '[class*=btq_opt]', '[role=radio]',
                        '[class*=quiz] button', '[class*=option]', '[class*=choice]'];
          for (const s of sels) {
            const el = [...document.querySelectorAll(s)].find(e => e.offsetParent !== null
                        && e.getBoundingClientRect().width > 20);
            if (el) return el;
          }
          return null;
        })()"""

        # 逐题点击 (通常 1-4 题)
        for qi in range(6):
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

    # 智能轮询等待 #dailyset 容器及卡片渲染完成 (应对 Next.js SPA 水合与网络延迟，最多等待 25 秒)
    cards = []
    deadline = time.time() + 25
    while time.time() < deadline:
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
            isDone: a.innerText.includes("已完成") || a.innerHTML.includes("bg-statusSuccess")
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
        cards = ev(cdp, """(() => {
          const ds = document.getElementById("dailyset");
          if (!ds) return [];
          return Array.from(ds.querySelectorAll("a[href]")).map((a, idx) => ({
            idx: idx,
            href: a.href,
            text: a.innerText.replace(/\\n/g, ' '),
            title: a.querySelector("h3")?.innerText || a.innerText.split("\\n")[0].slice(0, 30),
            isDone: a.innerText.includes("已完成") || a.innerHTML.includes("bg-statusSuccess")
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
                    wait = random.uniform(3, 6) if fast_mode else random.uniform(60, 90)
                    log('%s 答题后停留 %.1f 分钟等待结算' % (title, wait / 60))
                    time.sleep(wait)
                else:
                    w_min = 3 if fast_mode else int(st_card.get('cards', {}).get('attribution_wait_min', 120))
                    w_max = 6 if fast_mode else int(st_card.get('cards', {}).get('attribution_wait_max', 180))
                    wait = random.uniform(w_min, w_max)
                    log('%s 停留 %.1f 分钟等待归因' % (title, wait / 60))
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
    time.sleep(2)
    return '处理%d张' % done_count


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

    # 2. 搜索类属性判定 (Search)
    parsed = urllib.parse.urlparse(href)
    is_search = 'bing.com' in parsed.netloc and parsed.path.rstrip('/') == '/search'

    if is_search:
        if has_query:
            return {'action': 'search_direct', 'search_term': None, 'reason': 'URL已携带官方预置搜索词'}
        # 开放式搜索任务: 从卡片元数据中的 title (标题) 净化提取主题词
        clean_term = title
        for prefix in ('Bing 有您的', 'Bing有您的', '通过', '使用', '在您的桌面上', '在桌面上', '探索', '查找', '关于'):
            clean_term = clean_term.replace(prefix, '')
        clean_term = re.sub(r'[^\w\s\u4e00-\u9fa5]', ' ', clean_term).strip()

        if 2 <= len(clean_term) <= 30:
            chosen_term = clean_term
        else:
            matches = re.findall(r'[\u4e00-\u9fa5a-zA-Z0-9]{2,8}', card_text)
            chosen_term = matches[0] if (matches and len(matches[0]) >= 2) else '今日热点要闻'
        return {'action': 'search_semantic', 'search_term': chosen_term, 'reason': f'开放式搜索任务，从卡片元数据提取核心词: "{chosen_term}"'}

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
              isDone: text.includes("已完成") || /\\b(\\d+)\\s*\\/\\s*\\1\\s*个任务/.test(text)
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
                if (row && row.querySelector("div[class*=bg-statusSuccess], div[class*=statusSuccessRewardsBg], svg[class*=text-statusSuccess]")) {
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

              const multiDayMatch = pText.match(/(\\d+\\s*\\/\\s*\\d+\\s*天已完成)/) || pText.match(/(\\d+\\s*of\\s*\\d+\\s*days)/);

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
                    log('%s 跨日累计步骤 (%s) 今日打卡搜索已完成，微软规则要求间隔24小时次日继续，转入下一打卡' % (q_name, prog_str))
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
                if (row && row.querySelector("div[class*=bg-statusSuccess], div[class*=statusSuccessRewardsBg], svg[class*=text-statusSuccess]")) {
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

    # 智能轮询等待 #moreactivities 容器渲染 (最多等待 20 秒)
    deadline = time.time() + 20
    while time.time() < deadline:
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
        isDone: a.innerText.includes("已完成") || a.innerHTML.includes("bg-statusSuccess")
      })).filter(c => !c.isRedeem && !c.isDone && (c.text.includes("+") || c.text.includes("分")));
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

    return '处理%d张' % done_count


phase_earn = phase_moreactivities


def phase_claim(name, cdp, pr, ch=None, log=print):
    """检查并领取待领取积分 (移植自现网 bing_daily_v2.phase6)。

    1. 导航到 rewards dashboard;
    2. 检查是否有待领取积分 (API pointClaimBannerPromotion / DOM 可领取文案 / pr 探测属性);
    3. 若有，通过真人点击展开可领取面板，并点击领取积分;
    4. 回读确认并记录增量。
    """
    dash_url = 'https://rewards.bing.com/'
    log('开始检查是否有可领取积分...')
    navigate(cdp, dash_url)
    time.sleep(3)

    # 1. 先通过页面内 API 获取 pointClaimBannerPromotion 结构化信息
    claim_api = ev(cdp, """(async () => {
      try {
        const res = await fetch('/api/getuserinfo?type=1', {credentials: 'include'});
        if (!res.ok) return null;
        const d = await res.json();
        return d && d.dashboard ? (d.dashboard.pointClaimBannerPromotion || null) : null;
      } catch (e) {
        return null;
      }
    })()""")

    # 2. 从 DOM 中提取待领取数字 (若 DOM 明确显示 0 则表示已无待领)
    dom_points = ev(cdp, """(() => {
      const t = document.body.innerText || '';
      const m = t.match(/可领取\\s*(\\d+)/) || t.match(/(\\d+)\\s*(?:待领取|可领取)/) || t.match(/claimable\\s*(\\d+)/i);
      return m ? parseInt(m[1], 10) : null;
    })()""")

    if dom_points is not None and dom_points == 0:
        log('今日无可领取积分 (页面显示可领取为 0), 跳过')
        return '今日无可领取积分(跳过)'

    # 3. 结合 pr promotions 兜底判断
    pr_points = 0
    if dom_points is None and not claim_api:
        for promo in (pr or {}).get('promotions', []):
            attrs = promo.get('attributes', {})
            bd = attrs.get('claimable_points_breakdown')
            if bd:
                try:
                    bd_obj = json.loads(bd) if isinstance(bd, str) else bd
                    pts = int(bd_obj.get('TotalClaimablePoints') or 0)
                    if pts > 0:
                        pr_points = pts
                        break
                except Exception:
                    pass
            if attrs.get('claimable_points') in ('True', True) and int(attrs.get('claimable_balance_lsearchcap', 0) or 0) > 0:
                pr_points = int(attrs.get('claimable_balance_lsearchcap'))
                break

    total_claimable = dom_points if dom_points is not None else pr_points
    if claim_api and not total_claimable:
        try:
            total_claimable = int(claim_api.get('attributes', {}).get('claimable_points') or 0)
        except Exception:
            total_claimable = 0

    if not claim_api and (total_claimable is None or total_claimable <= 0):
        log('今日无可领取积分 (API/DOM 无待领取记录), 跳过')
        return '今日无可领取积分(跳过)'

    pts_str = f'{total_claimable}分' if total_claimable and total_claimable > 0 else '有待领积分'
    log(f'检测到可领取积分: 约 {pts_str} (API: {bool(claim_api)})')

    # 4. 执行 UI 点击领取
    # 第一步：展开“可领取”面板按钮 (如果尚未展开)
    panel_selector = """(() => {
      const btns = [...document.querySelectorAll('button, [role="button"]')];
      return btns.find(b => {
        const t = (b.innerText || '').trim();
        return (t.includes('可领取') || /claimable/i.test(t)) && !t.includes('领取积分') && !t.includes('已领取');
      }) || null;
    })()"""

    has_panel = ev(cdp, f"!!({panel_selector})")
    if has_panel:
        try:
            human_click(cdp, panel_selector, label='可领取面板', close_new=False)
            time.sleep(random.uniform(2.0, 3.5))
        except Exception as ex:
            log(f'点击可领取面板提示: {ex}')

    # 第二步：在弹层或页面内点击“领取积分”按钮
    btn_selector = """(() => {
      const dialog = document.querySelector('[role="dialog"], [role="alertdialog"]');
      const scope = dialog ? [...dialog.querySelectorAll('button, a')] : [...document.querySelectorAll('button, a, [role="button"]')];
      return scope.find(x => {
        const t = (x.innerText || '').trim().replace(/\\s+/g, ' ');
        return /^\\d+\\s*待领取\\s*领取积分/.test(t) || t.includes('领取积分') || /claim\\s*points/i.test(t) || (t.includes('待领取') && t.includes('领取'));
      }) || null;
    })()"""

    has_btn = ev(cdp, f"!!({btn_selector})")
    if not has_btn:
        scroll_human(cdp)
        time.sleep(2)
        has_btn = ev(cdp, f"!!({btn_selector})")

    if not has_btn:
        log('未找到"领取积分"按钮 (可能已自动入账或无需点击)')
        return f'检测到{total_claimable}分(未见领取按钮)' if (total_claimable and total_claimable > 0) else '无可领取'

    human_click(cdp, btn_selector, label='领取积分', close_new=False)
    time.sleep(random.uniform(4.0, 6.0))
    log('已点击领取积分按钮并完成结算等待')
    return f'领取{total_claimable}分' if (total_claimable and total_claimable > 0) else '领取完成'

