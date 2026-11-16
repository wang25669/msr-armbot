MAX_STEPS_PER_RUN = 8        # 单次运行每张卡最多推进步数 (2026-11-11 固化, 实测每日实际只推进 1 步)
MULTI_DAY_GUARD_HOURS = 20   # 多日任务保护窗: 20 小时内不重复推进同一张跨日卡
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

from msr.browser import ev, navigate, human_click, close_tab, tab_ids, _http_json


# ---------- P5 只读判据: 卡内对号徽章 + x/y 快照 (语言无关, 禁止用"卡片消失"推断) ----------
BADGE_JS_TPL = '''(() => {
  const box = document.getElementById(%s);
  if (!box) return { found: false, total: 0, cards: [] };
  // 卡片级归属: 只看卡片自身(或只含一张卡的父层)的徽章, 绝不借用兄弟卡徽章
  const pick = Array.from(box.querySelectorAll("a[href], button, [role='button']"));
  const cards = [];
  for (const el of pick) {
    const cardEl = el.closest("div[class*=rounded], li, div.flex.flex-col, div.flex.flex-row") || el;
    const own = cardEl !== el ? cardEl : el;
    const badge = own.querySelector('[class*="bg-statusSuccessRewardsBg"]');
    const t = (own.innerText || "").replace(/\\n/g, " ").trim();
    const m = t.match(/(\\d+)\\s*\\/\\s*(\\d+)/);
    if (!t) continue;
    cards.push({ text: t.slice(0, 80), badge: !!badge, prog: m ? (m[1] + "/" + m[2]) : null });
  }
  // 去重 (同一卡片内多个可点元素)
  const seen = new Set(); const uniq = [];
  for (const c of cards) { if (seen.has(c.text)) continue; seen.add(c.text); uniq.push(c); }
  return { found: true, total: uniq.length,
           badged: uniq.filter(c => c.badge).length, cards: uniq };
})()'''


def container_snapshot(cdp, container_id, log=print, tag=''):
    """读容器内每张卡的 x/y 与对号徽章, 只写日志 (P5 完成判据的观测点, 零副作用)。"""
    try:
        js = BADGE_JS_TPL % json.dumps(container_id)
        snap = ev(cdp, js) or {}
        if not snap.get('found'):
            return None
        log('%s%s 快照: 卡=%d 带徽章=%d' % (tag, container_id, snap.get('total', 0), snap.get('badged', 0)))
        for c in (snap.get('cards') or [])[:12]:
            log('%s  · [%s] %s%s' % (tag, c.get('prog') or '-', '✅ ' if c.get('badge') else '', c.get('text', '')[:70]))
        return snap
    except Exception as ex:
        log('%s 快照失败(容忍): %s' % (tag, str(ex)[:60]))
        return None


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




def phase_search(name, cdp, pr, ch=None, budget_seconds=None, target=None, log=print, token=None, proxy=None):
    """必应 PC 搜索打卡 (按账户等级读动态上限，自适应冷板停留与防卡死熔断)。"""
    from msr import settings
    st = settings.load_settings()
    s_cfg = st.get('search', {})
    sch_cfg = st.get('schedule', {})
    dwell_min = int(s_cfg.get('dwell_min_seconds', 180))
    dwell_max = int(s_cfg.get('dwell_max_seconds', 300))
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
    if target_max > 60:
        hard_cap = max(hard_cap, int((target_max / 5) * 1.5))
    if c_api and (c_api.get('complete') or current_prog >= target_max):
        log('PC搜索已满额: %d/%d (根据促销属性)' % (current_prog, target_max))
        return '%d/%d' % (current_prog, target_max)

    log('PC搜索启动: 当前进度 %d/%d (模式=%s, 单次停留 %.1f-%.1f 秒/词)' % (
        current_prog, target_max, '防风控真机', dwell_min, dwell_max))

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

            # 核心节奏停留: 180-300 秒 (等待 MSR 风控冷却)
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

    # 1. 优先判定落地页/搜索体验类 (排除 UrlOffer/tgrew4/rnoreward 误当成测验卡死)
    if 'urloffer' in lower or 'rnoreward=1' in lower or 'form=tgrew4' in lower:
        return 'urloffer'
    m = re.search(r'filters=([^&]+)', raw_url)
    filters = urllib.parse.unquote(m.group(1)).lower() if m else ''
    if 'btepokey:"rewardsquiz_dailyset_urloffer"' in filters:
        return 'urloffer'
    if re.search(r'filters=sid:"[0-9a-f-]{36}"', filters, re.I):
        return 'sid'

    # 2. 测验/问答/投票
    if 'pollscenarioid' in lower or 'poll' in lower or '投票' in lower:
        return 'quiz'
    if 'isconversation:"true"' in filters or 'wqoskey:' in filters or 'form=dsetqu' in lower or 'gamification_dailyset' in lower:
        return 'quiz'
    if ('quiz' in lower or 'trivia' in lower or 'btroid' in lower) and 'urloffer' not in lower:
        return 'quiz'

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
          const sels = ['.btq_opt a', '.btq_opt button', '.btq_opts .acf-button-standard__link',
                        '.btq_opt', '[class*=btq_opt]', '.btp_choice', '[class*=btp_choice]',
                        '.btp_choice_wrapper', 'acf-button-standard.btp_choice',
                        '[role=radio]', '[class*=quiz] button', '[class*=option]', '[class*=choice]'];
          for (const s of sels) {
            const el = [...document.querySelectorAll(s)].find(e => e.offsetParent !== null
                        && e.getBoundingClientRect().width > 20);
            if (el) return el;
          }
          return null;
        })()"""

        next_btn_js = """(() => {
          const sels = ['.btq_nxtQues button', '.btq_nxtQues a', '.btq_nxtQues',
                        '[class*=nxtQues] button', '[class*=nxtQues]',
                        '#checkAnswerBtn', '#nextQuestionBtn', '[class*=nextQuestion]'];
          for (const s of sels) {
            const el = document.querySelector(s);
            if (el && el.getBoundingClientRect().width > 0) return el;
          }
          return null;
        })()"""

        is_poll = 'poll' in label.lower() or '投票' in label
        max_questions = 1 if is_poll else 6

        # 逐题点击 (通常 1-4 题，投票仅 1 题)
        for qi in range(max_questions):
            # 轮询等待选项或下一题按钮出现 (最多 8 秒以容忍动画/网络延迟)
            hit = False
            for _ in range(8):
                if ev(cdp_quiz, find_js + " !== null") or ev(cdp_quiz, next_btn_js + " !== null"):
                    hit = True
                    break
                time.sleep(1.0)
            if not hit:
                if qi > 0:
                    log('quiz交互: 已答%d题, 题目已全部完成 (%s)' % (qi, label))
                break

            # 若当前存在下一题/过渡按钮则先行推进
            if ev(cdp_quiz, next_btn_js + " !== null"):
                try:
                    ev(cdp_quiz, f"(({next_btn_js}))?.click()")
                    time.sleep(random.uniform(1.5, 2.5))
                except Exception:
                    pass

            # 点击题目选项
            try:
                if ev(cdp_quiz, find_js + " !== null"):
                    human_click(cdp_quiz, find_js, label=f'quiz第{qi+1}题:{label}', close_new=True)
                    log('quiz第%d题已点击 (%s)' % (qi + 1, label))
            except Exception as e:
                log('quiz交互点击容忍: %s' % str(e)[:60])
                break

            # 答题后等待并在出现下一题按钮时自动点击推进
            time.sleep(random.uniform(2.0, 3.5))
            if ev(cdp_quiz, next_btn_js + " !== null"):
                try:
                    ev(cdp_quiz, f"(({next_btn_js}))?.click()")
                except Exception:
                    pass

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


def phase_dailyset(name, cdp, pr, ch=None, budget_seconds=1500, log=print, proxy=None):
    """Daily Set 3张每日活动卡 (支持成熟账号平铺与新账号抽屉双模自适应)。

    2026-11-11: 完成状态以服务端 /earn payload 的 partner=dailyset(3/3) 为准,
    空抽屉 / 卡片判据不可靠时用服务端真值兜底, 不再靠"猜"。
    """
    earn_url = 'https://rewards.bing.com/earn'
    dash_url = 'https://rewards.bing.com/dashboard'

    from msr import settings
    st_card = settings.load_settings()

    # 1. 优先访问统一任务中心 /earn (杜绝 /dashboard 掉入 /about FAQ 重定向黑洞)
    navigate(cdp, earn_url)
    time.sleep(random.uniform(2.5, 4.0))

    def _server_dailyset():
        """服务端 dailyset 打卡状态; 读不到返回 None。"""
        try:
            from msr.tasks import streaks
            return (streaks.fetch_streaks(name, proxy=proxy) or {}).get('dailyset')
        except Exception as ex:
            log('[Daily Set] 服务端状态读取异常(忽略): %s' % str(ex)[:60])
            return None

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

    # 检查是否为老号平铺模式 (#dailyset 直接渲染在页面)
    cards = []
    _ensure_expanded()
    deadline = time.time() + 6
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
            isDone: /已完成|complete|completed/i.test(a.innerText) || 
                    /bg-statusSuccess|check/i.test(a.innerHTML) || 
                    /已完成|complete|completed/i.test(a.getAttribute("aria-label") || "")
          }));
        })()""") or []
        if cards:
            break
        time.sleep(1.0)

    # 2. 如果存在平铺模式卡片，执行平铺模式
    if cards:
        log('[Daily Set] 识别为成熟账号平铺模式 (#dailyset，共 %d 张)' % len(cards))
        container_snapshot(cdp, 'dailyset', log=log, tag='[P5] ')
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
                    if kind == 'quiz':
                        interact_quiz(ch, target_tid, label=title, log=log)
                        wait = random.uniform(25, 45)
                        log('%s 答题后停留 %.1f 秒等待结算' % (title, wait))
                        time.sleep(wait)
                    else:
                        w_min = int(st_card.get('cards', {}).get('attribution_wait_min', 60))
                        w_max = int(st_card.get('cards', {}).get('attribution_wait_max', 120))
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
                time.sleep(random.uniform(5, 10))

            done_count += 1

        navigate(cdp, earn_url)
        time.sleep(random.uniform(3.0, 4.5))
        _ensure_expanded()
        remaining_todo = ev(cdp, """(() => {
          const ds = document.getElementById("dailyset");
          if (!ds) return 0;
          return Array.from(ds.querySelectorAll("a[href]")).filter(a => {
            const isDone = /已完成|complete|completed/i.test(a.innerText) || 
                           /bg-statusSuccess|check/i.test(a.innerHTML) || 
                           /已完成|complete|completed/i.test(a.getAttribute("aria-label") || "");
            return !isDone;
          }).length;
        })()""") or 0
        if remaining_todo == 0 or (done_count >= len(cards) and remaining_todo <= 1):
            return '3/3已完成'
        return '部分完成:处理%d张(剩%d张待完成)' % (done_count, remaining_todo)

    # 3. 抽屉模式 (新账号 /earn 页面上的 Daily Set Streak 汇总抽屉)
    find_streak_js = """(() => {
      const btns = Array.from(document.querySelectorAll("button, div[role='button']"));
      const b = btns.find(el => {
        const t = (el.innerText || '');
        return (t.includes('每日连续打卡活动') || t.toLowerCase().includes('daily set streak')) &&
               !t.includes('连续 7 天完成每日活动');
      });
      if (!b) return null;
      const text = (b.innerText || '').replace(/\\s+/g, ' ');
      const m = text.match(/(?:活动|activity):\\s*(\\d+)\\s*\\/\\s*(\\d+)/i) || text.match(/(\\d+)\\s*\\/\\s*(\\d+)/);
      const prog = m ? parseInt(m[1], 10) : 0;
      const total = m ? parseInt(m[2], 10) : 3;
      return {
        id: b.id,
        text: text,
        prog: prog,
        total: total,
        isDone: prog >= total && total > 0
      };
    })()"""

    streak_info = ev(cdp, find_streak_js)
    if not streak_info:
        # 回退至 dashboard 页面检测
        log('在 /earn 未发现平铺或打卡抽屉，回退至 /dashboard 检测...')
        navigate(cdp, dash_url)
        time.sleep(random.uniform(3.0, 4.5))
        _ensure_expanded()
        streak_info = ev(cdp, find_streak_js)

    if not streak_info:
        # 页面没有卡片时不要只报"未发现"：用服务端真值说清是"已完成"还是"当日任务还没发布"
        _sv_missing = _server_dailyset()
        if _sv_missing is not None and (_sv_missing.get('total') or 0) > 0:
            from msr.tasks import streaks as _sk3
            if _sk3.is_done(_sv_missing):
                log('[Daily Set] 页面无卡片, 服务端 %s/%s 已完成 -> 判完成'
                    % (_sv_missing.get('completed'), _sv_missing.get('total')))
                return '3/3已完成(服务端)'
            log('[Daily Set] 页面无卡片, 服务端 %s/%s 未完成 -> 疑似当日任务尚未发布(美西 0 点后一段时间才出卡)'
                % (_sv_missing.get('completed'), _sv_missing.get('total')))
            return '未发现卡片(服务端 %s/%s, 疑似任务未发布)' % (
                _sv_missing.get('completed'), _sv_missing.get('total'))
        log('未发现每日活动区域或 Daily Set 卡片')
        return '未发现每日活动'

    if streak_info.get('isDone'):
        log('今日每日活动已全部完成 (%d/%d)' % (streak_info.get('prog', 3), streak_info.get('total', 3)))
        return '3/3已完成'

    log('[Daily Set] 识别为新版抽屉模式 (%s, 当前进度 %d/%d)，正在展开抽屉...' % (
        streak_info.get('text', '')[:40], streak_info.get('prog', 0), streak_info.get('total', 3)))

    # 点击展开抽屉
    click_drawer_js = """(() => {
      const btns = Array.from(document.querySelectorAll("button, div[role='button']"));
      const b = btns.find(el => {
        const t = (el.innerText || '');
        return (t.includes('每日连续打卡活动') || t.toLowerCase().includes('daily set streak')) &&
               !t.includes('连续 7 天完成每日活动');
      });
      if (b) { b.click(); return true; }
      return false;
    })()"""
    ev(cdp, click_drawer_js)
    time.sleep(random.uniform(2.5, 3.5))

    # 提取抽屉内的子卡片
    extract_drawer_cards_js = """(() => {
      const dialog = document.querySelector("[role='dialog'], section[role='dialog']");
      if (!dialog) return [];
      const links = Array.from(dialog.querySelectorAll("a[href]"));
      return links.map((a, idx) => {
        const text = (a.innerText || '').replace(/\\n/g, ' ').trim();
        const isDone = /已完成|complete|completed/i.test(text) || 
                       a.innerHTML.includes('statusSuccess') || 
                       a.innerHTML.includes('check');
        const h3 = a.querySelector('h3, h2, div');
        const title = h3 ? h3.innerText.replace(/\\n/g, ' ').trim() : text.split(' ')[0];
        return {
          idx: idx,
          href: a.href,
          text: text,
          title: title.slice(0, 30),
          isDone: isDone
        };
      });
    })()"""

    drawer_cards = ev(cdp, extract_drawer_cards_js) or []
    log('抽屉已展开，发现 %d 张每日子卡片: %s' % (len(drawer_cards), [c['title'] for c in drawer_cards]))
    # 抽屉偶发"展开慢/首次渲染为空"(2026-11-10 实测: 登录后立刻跑 -> 0 张; 几小时后跑 -> 3 张),
    # 旧逻辑把空抽屉当"已完成" -> 当日 Daily Set 三张卡 0 分且误报成功。这里改成硬轮询:
    # 每 3 秒重试一次, 每两次重连刷新一次 /earn, 最多约 30 秒; 仍为空才判失败交阶段10回补。
    if not drawer_cards:
        _sv0 = _server_dailyset()
        if _sv0 is not None:
            from msr.tasks import streaks as _sk
            if _sk.is_done(_sv0):
                log('[Daily Set] 抽屉暂时没卡片, 但服务端已是 %s/%s(今日完成) -> 判完成'
                    % (_sv0.get('completed'), _sv0.get('total')))
                return '3/3已完成(服务端)'
            log('[Daily Set] 抽屉暂时没卡片, 服务端 %s/%s 未完成 -> 继续尝试展开'
                % (_sv0.get('completed'), _sv0.get('total')))
    _open_try = 0
    while not drawer_cards and _open_try < 3:
        _open_try += 1
        ev(cdp, click_drawer_js)
        time.sleep(3.0)
        drawer_cards = ev(cdp, extract_drawer_cards_js) or []
        if drawer_cards:
            log('[Daily Set] 第 %d 次尝试取到 %d 张子卡片' % (_open_try, len(drawer_cards)))
            break
        if _open_try % 2 == 0:
            log('[Daily Set] 第 %d 次尝试仍为空, 重新加载 /earn 后再试' % _open_try)
            navigate(cdp, earn_url)
            time.sleep(random.uniform(3.0, 4.5))
            ev(cdp, click_drawer_js)
            time.sleep(2.0)
            drawer_cards = ev(cdp, extract_drawer_cards_js) or []
    if not drawer_cards:
        # 诊断: 抽屉结构没取到卡片时, 打印页面真实结构, 避免再次误判"已完成"
        try:
            dump = ev(cdp, """(() => {
              const ds = document.getElementById('dailyset');
              const dialogs = Array.from(document.querySelectorAll("[role='dialog'], section[role='dialog'], [class*='Dialog'], [class*='dialog']"));
              const links = Array.from(document.querySelectorAll('a[href]')).map(a => ({
                t: (a.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 50),
                h: (a.getAttribute('href') || '').slice(0, 90),
                c: (a.className || '').toString().slice(0, 60)
              }));
              return {
                title: document.title.slice(0, 40),
                hasDailyset: !!ds,
                dailysetHtml: ds ? ds.outerHTML.replace(/\\s+/g, ' ').slice(0, 1200) : null,
                dialogCount: dialogs.length,
                dialogInfo: dialogs.slice(0, 4).map(d => ({ tag: d.tagName, cls: (d.className || '').toString().slice(0, 80),
                                                           links: d.querySelectorAll('a[href]').length,
                                                           txt: (d.innerText || '').replace(/\\s+/g, ' ').slice(0, 200) })),
                linkCount: links.length,
                links: links.slice(0, 20)
              };
            })()""")
            log('[Daily Set 诊断] ' + json.dumps(dump, ensure_ascii=False)[:2600])
        except Exception as ex_dump:
            log('[Daily Set 诊断] 失败: %s' % str(ex_dump)[:80])

    if not drawer_cards:
        # 空抽屉绝不能当"已完成": 交回 runner 阶段10 用 legacy 路径回补
        log('[Daily Set] 抽屉重试后仍取不到子卡片, 判失败并交由阶段10回补')
        return '失败(dailyset抽屉未取到卡片)'
    todo_drawer = [c for c in drawer_cards if not c.get('isDone')]
    if not todo_drawer:
        log('抽屉内卡片已全部完成')
        ev(cdp, "(() => { const c = document.querySelector('[role=\"dialog\"] button, section[role=\"dialog\"] button'); if (c) c.click(); })()")
        return '3/3已完成'

    done_drawer_count = 0
    for card in todo_drawer:
        idx = card['idx']
        title = card['title']
        href = card['href']
        kind = dailyset_kind({'destinationUrl': href})
        log('执行抽屉每日卡片: %s [%s] -> %s' % (title, kind, href[:60]))

        try:
            # 确保抽屉处于展开状态 (若此前卡片交互导致抽屉失焦闭合，自动重新唤醒展开)
            is_open = ev(cdp, 'Boolean(document.querySelector("[role=\\"dialog\\"], section[role=\\"dialog\\"]"))')
            if not is_open:
                ev(cdp, click_drawer_js)
                time.sleep(random.uniform(2.0, 3.0))

            # 使用 human_click 真实点击抽屉内的 <a> 卡片，触发 React 与微软官方点击归因追踪
            selector = f'document.querySelectorAll("[role=\\"dialog\\"] a[href], section[role=\\"dialog\\"] a[href]")[{idx}]'
            new_tabs = human_click(cdp, selector, label=title[:15], close_new=False)
            if not new_tabs and ch and href:
                new_tab = ch.new_tab(href)
                if new_tab and new_tab.get('id'):
                    new_tabs = [new_tab['id']]

            if new_tabs and ch:
                target_tid = new_tabs[0]
                try:
                    time.sleep(random.uniform(2.5, 4.0))
                    if kind == 'quiz':
                        interact_quiz(ch, target_tid, label=title, log=log)
                        wait = random.uniform(25, 45)
                        log('%s 答题后停留 %.1f 秒等待结算' % (title, wait))
                        time.sleep(wait)
                    else:
                        w_min = int(st_card.get('cards', {}).get('attribution_wait_min', 60))
                        w_max = int(st_card.get('cards', {}).get('attribution_wait_max', 120))
                        wait = random.uniform(w_min, w_max)
                        log('%s 停留 %.1f 秒等待归因' % (title, wait))
                        time.sleep(wait)
                finally:
                    for tid in new_tabs:
                        try:
                            ch.close_tab(tid)
                        except Exception:
                            pass
            done_drawer_count += 1
        except Exception as card_err:
            log('抽屉卡片 %s 执行容错: %s' % (title, str(card_err)[:80]))
        time.sleep(random.uniform(1.5, 3.0))

    # 关闭抽屉
    ev(cdp, "(() => { const c = document.querySelector('[role=\"dialog\"] button, section[role=\"dialog\"] button'); if (c) c.click(); })()")
    time.sleep(1.5)

    # 重新导航 /earn 回读最新指标
    navigate(cdp, earn_url)
    time.sleep(random.uniform(3.0, 4.5))
    final_streak = ev(cdp, find_streak_js) or {}
    prog_now = final_streak.get('prog', done_drawer_count)
    tot_now = final_streak.get('total', 3)
    log('每日活动执行后最终回读: %s/%s' % (prog_now, tot_now))
    if prog_now >= tot_now or (done_drawer_count >= len(drawer_cards) and prog_now >= 2):
        return '3/3已完成'
    _sv1 = _server_dailyset()
    if _sv1 is not None:
        from msr.tasks import streaks as _sk2
        if _sk2.is_done(_sv1):
            return '3/3已完成(服务端确认)'
        return '部分完成:处理%d张(服务端 %s/%s)' % (done_drawer_count, _sv1.get('completed'), _sv1.get('total'))
    return '部分完成:处理%d张(当前%d/%d)' % (done_drawer_count, prog_now, tot_now)


phase_quick = phase_dailyset


def resolve_quest_subtask_action(step_info, q_name):
    """根据任务卡片固有通用属性(URL结构、Query参数、卡片元数据)自适应解析动作类型。

    绝不针对任何特定节日、主题硬编码关键词。
    通用操作分流:
    0. 'activate': 卡片首步是「激活优惠 / opt-in」类 CTA —— 真人点击锚点即可，不做搜索
    1. 'quiz': 具有官方测验标识 (filters=BTBQ/BTB/REWARDSQUIZ 或路径含 quiz)
    2. 'search_direct': 具有搜索终结点且已有预设关键词 q
    3. 'search_semantic': 具有搜索终结点但无 q (开放式搜索任务)，从卡片 title 元数据提取核心题干搜索
    4. 'landing': 常规网页/壁纸/活动介绍落地页，模拟自然浏览与停留
    """
    href = step_info.get('href') or ''
    title = (step_info.get('title') or '').strip()
    card_text = (step_info.get('cardText') or '').strip()
    step_text = (step_info.get('text') or '').strip()
    has_query = step_info.get('hasQuery', False)

    parsed = urllib.parse.urlparse(href)
    is_bing = 'bing.com' in parsed.netloc
    is_search_path = parsed.path.rstrip('/') == '/search'

    # 0. 激活/opt-in 类属性判定
    # 2026-11-11 某新号 实测: Spotify 打卡第一步是 "Activate Spotify offer now"
    # -> https://www.bing.com/?form=ML2X8X... 旧逻辑因卡片文本含"搜索"被判成 search_semantic,
    # 结果把 "But first ACTIVATE the offer" 当关键词搜了一次, 优惠到当晚仍未激活。
    # 判据只看 CTA 自身文案(标题/步骤文字), 且排除已带 q 的真搜索终结点。
    _act_pat = r'(?:activate|opt[\s\-]?in|enable\s|turn\s*on|join\s*now|激活|开启|启用|立即领取)'
    _is_real_search = is_bing and is_search_path and has_query
    if not _is_real_search and (re.search(_act_pat, title, re.I) or re.search(_act_pat, step_text, re.I)):
        return {'action': 'activate', 'search_term': None,
                'reason': '激活/opt-in 类 CTA, 真人点击锚点, 不搜索'}

    # 1. 测验/竞答类属性判定 (Quiz / Trivia)
    if any(k in href for k in ('REWARDSQUIZ', 'BTBQ', 'BTB')) or '/quiz' in href.lower():
        return {'action': 'quiz', 'search_term': None, 'reason': 'URL携带官方测验标识(REWARDSQUIZ/BTBQ/quiz)'}

    # 2. 搜索类属性判定 (Search，含新手打卡 Onboarding / Search now 引导)
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

# 打卡步骤锚点的**唯一**判定实现（"找步骤"与"点步骤"共用一份，禁止再各写一套）
# 判定只看元素结构与属性，**不猜文案、不看 URL 关键字**（2026-11-16 用户纠正）：
#   · 一行步骤 = class 含 flex-row 且内部恰好一个 h3 的容器（条款正文在 .text-legal 块里、没有 h3，天然排除）
#   · 已完成   = 行内有成功徽章（div[class*=bg-statusSuccess] / svg[class*=text-statusSuccess] …）
#   · 未解锁   = 行内没有任何 a/button（服务端只给可做的那一步挂可点元素）
#   · 可点     = 无徽章 + 行内第一个可点元素就是它 + 该元素没有 disabled/aria-disabled/data-disabled
# 旧实现按文案与 URL 关键字过滤，把收尾领奖步骤（文案含 completed、href=/redeem/<sku>）双重挡死，
# 卡片永久停在 x-1/x；而首日能点两步的卡也被"取第一个"的逻辑吃掉一步。
_QUEST_STEP_PICK_JS = r"""
  const __hasBadge = (el) => !!(el && el.querySelector("div[class*=bg-statusSuccess], div[class*=statusSuccessRewardsBg], svg[class*=text-statusSuccess], svg[aria-label*=complete i], svg[aria-label*=已完成]"));
  const __rowOf = (a) => {
    let n = a, i = 0;
    while (n && i < 6) {
      if (String(n.className || "").includes("flex-row")) return n;
      n = n.parentElement; i++;
    }
    return null;
  };
  const __isDisabled = (a) => {
    if (!a) return true;
    if (a.disabled === true) return true;
    if (a.getAttribute("aria-disabled") === "true") return true;
    if (a.hasAttribute("data-disabled")) return true;
    return false;
  };
  const __pickStep = (a) => {
    const row = __rowOf(a);
    if (!row) return false;
    if (row.querySelectorAll("h3").length !== 1) return false;
    if (__hasBadge(row)) return false;
    const first = row.querySelector("a[href], button, [role=button]");
    if (first !== a) return false;
    return !__isDisabled(a);
  };
"""


def phase_quests(name, cdp, pr, ch=None, budget_seconds=1500, log=print, proxy=None):
    """任务打卡(Quests / Punchcards，含桌面专属打卡)。"""
    # P1: 打卡参数全部来自 settings.json (原地生效, 不再写死)
    from msr import settings as _S
    _st = _S.load_settings()
    _q = _st.get('quests', {})
    activation_wait = int(_q.get('activate_wait_seconds', 15))
    dw_min = float(_q.get('step_dwell_min', 60))
    dw_max = float(_q.get('step_dwell_max', 120))
    max_steps = MAX_STEPS_PER_RUN
    guard_hours = MULTI_DAY_GUARD_HOURS
    log('打卡参数: 激活等待=%ds 步骤停留=%g~%gs 单卡步数=%d 跨日保护=%gh' % (
        activation_wait, dw_min, dw_max, max_steps, guard_hours))

    def _md_state(qn, write=None):
        """多日任务推进时间戳 (P1: multi_day_guard_hours 的真实读取点)。"""
        try:
            from msr import config as _C
            sp = _C.pfile(name, 'state.json')
            s = _C.load_json(sp, {}) or {}
            m = s.get('multi_day_advance') or {}
            if write is not None:
                m[qn] = int(write)
                s['multi_day_advance'] = m
                _C.save_json(sp, s)
                return write
            return m.get(qn)
        except Exception:
            return None

    navigate(cdp, 'https://rewards.bing.com/earn')
    time.sleep(random.uniform(2.5, 4.0))
    # 触发页面下滚以完成 Next.js 组件水合与打卡任务卡片懒加载
    ev(cdp, 'window.scrollTo(0, document.body.scrollHeight/2);')
    time.sleep(1.0)

    # 智能轮询等待 #quests 容器及链接渲染完成 (最多等待 20 秒)
    def _poll_quests(seconds=20):
        _q = []
        _dl = time.time() + seconds
        while time.time() < _dl:
            _q = ev(cdp, """(() => {
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
            if _q:
                break
            time.sleep(1.0)
        return _q

    quests = _poll_quests()

    if not quests:
        # 2026-11-16 事故: 某 SG 号 11-13 这里 47 秒空跑就返回"无打卡任务", 当天该推进的一步没了。
        # 周卡 8 步 = 8 个 24h 窗口, 卡只活 7 天, 少一天必然 7/8 领不到 100 分(实测 11-15 收尾 7/8)。
        # 所以空列表不能当"今日没任务": 先重载 /earn 两次, 再用服务端 payload 交叉验证。
        for _retry in range(2):
            log('[重试] #quests 为空, 第%d次重载 /earn' % (_retry + 1))
            navigate(cdp, 'https://rewards.bing.com/earn')
            time.sleep(random.uniform(3.0, 5.0))
            ev(cdp, 'window.scrollTo(0, document.body.scrollHeight*0.4);')
            time.sleep(1.5)
            ev(cdp, 'window.scrollTo(0, document.body.scrollHeight);')
            time.sleep(1.5)
            quests = _poll_quests(12)
            if quests:
                log('[重试] 第%d次重载后取到 %d 张打卡卡' % (_retry + 1, len(quests)))
                break
    if not quests:
        _ids = None
        try:
            from msr.tasks import quest_activity as _qa
            _ids = _qa.list_quest_ids(name, proxy=proxy)
        except Exception as _ex:
            log('打卡列表空, 服务端 payload 交叉验证失败(忽略): %s' % str(_ex)[:60])
        if _ids:
            log('!!! 服务端 payload 里仍有 %d 张打卡卡 (%s), 页面 #quests 未渲染 -> 判失败交阶段10回补'
                % (len(_ids), ','.join(_ids[:3])))
            return '失败(quests 列表未渲染, 服务端有%d张)' % len(_ids)
        if _ids is None:
            log('今日未发现打卡任务 (#quests, 服务端 payload 读取失败, 无法交叉验证)')
        else:
            log('今日未发现打卡任务 (#quests, 服务端 payload 亦无打卡卡)')
        return '无打卡任务'

    active_quests = [q for q in quests if not q.get('isDone')]
    log('发现 %d 个打卡任务 (待处理 %d 个)' % (len(quests), len(active_quests)))
    # P5: 每张打卡卡 x/y 进度逐条落日志 (回归度量, 只读)
    prog_before = {}
    for q in quests:
        m = re.search(r'(\d+)\s*/\s*(\d+)', q.get('text') or '')
        if m:
            prog_before[q.get('href')] = m.group(0)
        log('  quest 进度: %-46s %s%s' % (
            (q.get('href') or '').split('/')[-1][:46],
            m.group(0) if m else '-',
            ' [已完成]' if q.get('isDone') else ''))
    container_snapshot(cdp, 'quests', log=log, tag='[P5] ')

    total_steps_done = 0
    cards_done = 0
    cards_unscoped = 0
    executed = {}          # q_name -> [步骤文案]，本阶段真正执行过的步骤（服务端步骤级回读用）
    pre_snap = {}          # q_name -> 执行前的已完成步骤快照 {标题: 条数}（执行后 diff 出真正计分的步数）
    t0 = time.time()

    for q in active_quests:
        if time.time() - t0 > budget_seconds:
            break
        q_url = q['href']
        q_name = q_url.split('/')[-1]

        # P1: 多日任务跨日保护 —— 保护窗内不重复推进 (避免同日回补重复提交)
        try:
            last_adv = _md_state(q_name)
            if last_adv and guard_hours > 0 and (time.time() - float(last_adv)) < guard_hours * 3600:
                log('%s 跨日保护: %.1f 小时前已推进 (<%gh), 本轮跳过' % (
                    q_name, (time.time() - float(last_adv)) / 3600.0, guard_hours))
                continue
        except Exception:
            pass

        log('进入打卡任务: %s' % q_name)
        seen_step_urls = set()

        # 循环完成该任务中今天可执行的所有子步骤 (步数上限 = MAX_STEPS_PER_RUN)
        for step_attempt in range(max_steps):
            navigate(cdp, q_url)
            time.sleep(random.uniform(2, 4))

            # 智能等待打卡步骤组件水合渲染 (最多 15 秒)
            step_info = None
            step_deadline = time.time() + max(5, activation_wait)
            while time.time() < step_deadline:
                res = ev(cdp, "(() => {" + _QUEST_STEP_PICK_JS + """
                  const mainEl = document.querySelector("main");
                  if (!mainEl) return { ready: false, step: null };
                  const anchors = Array.from(mainEl.querySelectorAll("a[href], button")).filter(__pickStep);
                  if (anchors.length === 0) {
                    const allRows = mainEl.querySelectorAll("div.flex.flex-row, div.flex.flex-col.gap-3, div[class*=rounded-2xl]");
                    if (allRows.length > 0) return { ready: true, step: null };
                    return { ready: false, step: null };
                  }
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
                    ready: true,
                    step: {
                      text: target.innerText.replace(/\\n/g, ' ').trim() || pTitle,
                      title: pTitle,
                      href: href,
                      hasQuery: hasQ,
                      cardText: pText,
                      isMultiDay: !!multiDayMatch,
                      multiDayProg: multiDayMatch ? multiDayMatch[1] : null
                    }
                  };
                })()""")
                if res and res.get('ready'):
                    step_info = res.get('step')
                    break
                time.sleep(1.0)

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
                _md_state(q_name, write=time.time())
            elif step_href in seen_step_urls:
                log('%s 步骤已处理过(需等待冷却或次日刷新)，进入下一打卡' % q_name)
                break
            seen_step_urls.add(step_href)

            log('执行步骤: %s -> %s' % (step_text[:30], step_href[:60]))

            # 与"找步骤"共用同一份过滤(__pickStep): 两处漂移过一次, 结果 find 认出的收尾领奖步骤
            # 在 click 侧又用旧规则被拒, 实测报 "点击目标不存在: 立即兑换"。
            click_selector = "(() => {" + _QUEST_STEP_PICK_JS + """
              const el = Array.from(document.querySelectorAll("main a[href], main button")).filter(__pickStep)[0];
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

            # 2026-11-12: 点这一卡的第一个步骤之前，先取一次卡详情页的「已完成步骤」快照。
            # 阶段末再取一次，diff 出来的增量就是本次真正计分的步数（外层 x/y 刷新滞后数小时，不能当判据）。
            if q_name not in pre_snap:
                try:
                    from msr.tasks import quest_activity as _qa0
                    pre_snap[q_name] = _qa0.snapshot(
                        _qa0.fetch_activities(name, q_name, proxy=proxy))
                    if pre_snap[q_name] is None:
                        log('%s 执行前快照读取失败(该卡按未知处理)' % q_name[:40])
                except Exception as ex_pre:
                    pre_snap[q_name] = None
                    log('%s 执行前快照异常(忽略): %s' % (q_name[:40], str(ex_pre)[:60]))

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

                    # P1: 单步骤停留来自 quests.step_dwell_min/max (快速模式 3~6 秒)
                    w = random.uniform(dw_min, dw_max)
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
            executed.setdefault(q_name, []).append(step_text)
            time.sleep(random.uniform(2, 4))

    # P5: 全部卡片处理完再回 /earn 统一回读 (打卡详情页里没有 #quests 列表, 单卡回读必然定位失败 —— 2026-11-09 实测)
    try:
        navigate(cdp, 'https://rewards.bing.com/earn')
        time.sleep(random.uniform(3.0, 4.5))
        deadline = time.time() + 20
        while time.time() < deadline:
            if ev(cdp, 'document.getElementById("quests") !== null'):
                break
            time.sleep(1.0)
        for q in active_quests:
            q_name = (q.get('href') or '').split('/')[-1]
            if not q_name:
                continue
            try:
                snap = ev(cdp, '''(() => {
                  const target = %s;
                  const qs = document.getElementById("quests") || document;
                  const a = Array.from(qs.querySelectorAll("a[href*='/earn/quest/']"))
                              .find(x => (x.href || "").includes(target));
                  if (!a) return { found: false };
                  // 2026-11-11 某新号 实测: a.closest("div[class*=rounded]") 会抓到包含多张卡的
                  // 外层容器, 卡片文本的第一个 x/y 与首个徽章来自**别的卡** —— Spotify 因此被报成
                  // "进度=6/7 徽章=✅已完成"(那是 onboarding 卡的 6/7), 实际 Spotify 是 3/6 未激活。
                  // 规则: 只在"祖先里仅含本卡唯一 quest 锚点"时向上走, 一旦会引入兄弟卡就停。
                  let card = a, el = a.parentElement, scoped = false;
                  for (let i = 0; i < 6 && el; i++, el = el.parentElement) {
                    const n = el.querySelectorAll("a[href*='/earn/quest/']").length;
                    if (n > 1) break;
                    card = el; scoped = true;
                  }
                  const t = (card.innerText || "").replace(/\\n/g, " ");
                  // 优先咬住紧跟"个任务/tasks/days"的 x/y (2026-11-11 某新号 实测: 7 张卡全都有)
                  const m = t.match(/(\\d+)\\s*\\/\\s*(\\d+)\\s*(?:个任务|个活動|tasks|days|天)/i)
                         || t.match(/(\\d+)\\s*\\/\\s*(\\d+)/)
                         || t.match(/(\\d+)\\s*of\\s*(\\d+)/i);
                  // 该版式(某新号 实测)7 张卡的锚点同属一个列表容器, 卡级徽章归属不可判 ——
                  // 徽章必须返回 null(未知), 不能兜底成 false 报"未完成"。
                  return { found: true, prog: m ? (m[1] + "/" + m[2]) : null,
                           scoped: scoped,
                           badge: scoped ? !!card.querySelector('[class*="bg-statusSuccessRewardsBg"]') : null,
                           text: t.slice(0, 50) };
                })()''' % json.dumps(q_name)) or {}
                if snap.get('found'):
                    prog = snap.get('prog') or '-'
                    badge = snap.get('badge')
                    if badge is None:
                        # 该版式无卡级容器: 徽章归属不可判, 只能用进度增量当证据, 不许报"完成/未完成"
                        cards_unscoped += 1
                        advanced = bool(prog != '-' and prog != prog_before.get(q.get('href')) and
                                        (prog_before.get(q.get('href')) or '0/0') != prog)
                        if advanced:
                            cards_done += 1
                        log('%s 回读: 进度=%s 徽章=未回读(无卡级容器)%s' % (
                            q_name[:46], prog, ' 进度已推进' if advanced else ''))
                    else:
                        log('%s 回读: 进度=%s 徽章=%s' % (q_name[:46], prog,
                                                       '✅已完成' if badge else '未完成'))
                        if badge:
                            cards_done += 1
                else:
                    log('%s 回读: 卡片未定位 (按未完成处理)' % q_name[:46])
            except Exception as ex_snap:
                log('%s 回读异常(容忍): %s' % (q_name[:40], str(ex_snap)[:60]))
    except Exception as ex_sum:
        log('打卡完成后统一回读异常(容忍): %s' % str(ex_sum)[:80])

    # 2026-11-12: 外层 `x/y tasks` 的服务端刷新滞后可达数小时（实测 12:14 读 Sea 仍 1/4、22:20 才变 2/4；
    # Duolingo 21:01 读 1/6、22:20 已 2/6），只按外层判就把**已计分**的步骤报成"进度推进0/K张"。
    # 这里改成**执行前后快照对比**：卡的已完成步骤数增量 = 本次真正计分的步数（HTTP，零浏览器）。
    # 为什么要 diff 而不是按文案匹配：已完成的步骤在 payload 里没有 anchor/linkText，只有徽章。
    total_exec = sum(len(v) for v in executed.values())
    credited = 0
    unknown = 0
    if pre_snap:
        try:
            from msr.tasks import quest_activity as _qa
        except Exception as ex_qa:
            _qa = None
            log('步骤级回读不可用(忽略): %s' % str(ex_qa)[:60])
        if _qa is not None:
            def _diff():
                ok, unk = 0, 0
                for _qn, _before in pre_snap.items():
                    _after = _qa.snapshot(_qa.fetch_activities(name, _qn, proxy=proxy))
                    _d = _qa.newly_completed(_before, _after)
                    if _d is None:
                        unk += 1
                    else:
                        ok += _d
                        if _d:
                            log('%s 服务端新增已完成 %d 步' % (_qn[:40], _d))
                return ok, unk

            try:
                credited, unknown = _diff()
                if credited < total_exec:
                    log('步骤级回读: %d/%d 步已计分, 等待 180 秒后复读一次...' % (credited, total_exec))
                    time.sleep(180)
                    _c2, unknown = _diff()
                    credited = max(credited, _c2)
                    log('步骤级回读复读: %d/%d 步已计分' % (credited, total_exec))
                credited = min(credited, total_exec)
            except Exception as ex_cf:
                log('步骤级回读异常(容忍): %s' % str(ex_cf)[:60])
    if unknown:
        log('步骤级回读: %d 张卡快照读取失败, 按未知处理(不据此判完成/未完成)' % unknown)

    if cards_unscoped:
        return '处理%d个打卡步骤(服务端确认%d/%d步已计分, 外层进度推进%d/%d张, %d张版式无卡级徽章)' % (
            total_steps_done, credited, total_exec, cards_done, len(active_quests), cards_unscoped)
    return '处理%d个打卡步骤(服务端确认%d/%d步已计分, %d/%d张带对号徽章)' % (
        total_steps_done, credited, total_exec, cards_done, len(active_quests))


def extract_semantic_explore_query(title, desc):
    """从 Explore on Bing 卡片中通用提炼搜索短语。

    不使用任何硬编码关键词；直接剥离引导前缀（如 'Search on Bing' / '在必应上搜索'），
    原汁原味使用后续完整描述内容作为搜索词，确保对未来任意新卡片及多语言环境天然通用。
    """
    text = (desc or title or '').strip()
    if not text:
        return title or 'trending searches'

    # 1. 剥离中英文常见搜索引导前缀（如 'Search on Bing', 'Search Bing', '在必应上搜索' 等）
    cleaned = re.sub(
        r'^(?:search\s+on\s+bing\s*|search\s+bing\s*|在必应(?:上|中)?搜索\s*|通过必应搜索\s*|在必应(?:上|中)?查找\s*|在必应(?:上|中)?探索\s*)',
        '',
        text,
        flags=re.IGNORECASE
    ).strip()

    # 2. 剥掉句首语法胶水词 (for a / to the / about ...): 卡片描述常是句子片段, 直接当 query 会搜偏
    #    例: "for a flight to your perfect vacation" -> "flight to your perfect vacation"
    while True:
        m_glue = re.match(r'^(?:for|to|about|on|in|of|the|a|an)\b[\s,:-]*', cleaned, re.IGNORECASE)
        if not m_glue:
            break
        rest = cleaned[m_glue.end():].strip()
        if len(rest) < 8:
            break
        cleaned = rest

    # 3. 去除末尾标点符号 (句号、问号、叹号等)
    cleaned = re.sub(r'[\.\?\!\;。？！；]+$', '', cleaned).strip()

    # 4. 兜底保护：若剥离后内容有效则直接使用，否则退回使用卡片标题 title
    if len(cleaned) >= 2 and not re.search(r'^(?:completed|activated|unlocks|已完成|明天解锁)', cleaned, re.I):
        return cleaned

    return (title or desc or 'trending searches').strip()


# ---------- 必应探索打卡 (Explore on Bing，美区及部分新版区域 40 分模块) ----------

def phase_explore(name, cdp, pr, ch=None, log=print):
    """必应探索打卡 (Explore on Bing / 在必应上探索，通常包含 4 张 10 分主题搜索卡片)。"""
    # P1: 探索参数 (每卡独立关键词 = 红线; 停留时长可配)
    from msr import settings as _S
    _e = _S.load_settings().get('explore', {})
    ex_min = float(_e.get('dwell_min', 16))
    ex_max = float(_e.get('dwell_max', 20))
    if ex_max < ex_min:
        ex_min, ex_max = ex_max, ex_min
    navigate(cdp, 'https://rewards.bing.com/earn')
    time.sleep(random.uniform(2.5, 4.0))

    # 1. 探测是否存在 Explore on Bing 折叠容器
    find_explore_js = """(() => {
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

      // 若处于折叠状态，点击触发器展开面板
      if (disc) {
        const btn = disc.querySelector("button[aria-expanded='false']");
        if (btn) btn.click();
      }

      return { found: true, prog: prog, max: max, isDone: prog >= max && max > 0 };
    })()"""

    info = ev(cdp, find_explore_js)
    if not info or not info.get('found'):
        log('当前页面未检测到 Explore on Bing 模块(跳过)')
        return '无此模块(跳过)'

    if info.get('isDone'):
        log('Explore on Bing 今日已满额 (%d/%d)' % (info.get('prog', 40), info.get('max', 40)))
        return '已满额(%d/%d)' % (info.get('prog', 40), info.get('max', 40))

    log('发现 Explore on Bing 模块 (当前进度 %d/%d)，提取可用打卡卡片...' % (
        info.get('prog', 0), info.get('max', 40)))
    time.sleep(random.uniform(1.5, 2.5))

    # 2. 提取面板内所有未完成卡片
    extract_cards_js = """(() => {
      const headings = Array.from(document.querySelectorAll("h2, h3, button, span, div")).filter(el => {
        const t = (el.innerText || "").replace(/\\s+/g, " ").trim();
        return /^(?:Explore on Bing|在必应上探索|探索必应)/i.test(t) && t.length < 50;
      });
      if (!headings.length) return [];

      const heading = headings[0];
      const disc = heading.closest(".react-aria-Disclosure, [class*=Disclosure]") || heading.parentElement;
      const panel = disc ? (disc.querySelector(".react-aria-DisclosurePanel, [class*=DisclosurePanel]") || disc) : null;
      if (!panel) return [];

      const links = Array.from(panel.querySelectorAll("a[href]"));
      return links.map((a, idx) => {
        const text = (a.innerText || "").replace(/\\s+/g, " ").trim();
        const paragraphs = Array.from(a.querySelectorAll("p")).map(p => (p.innerText || "").trim());
        const title = paragraphs[0] || text.split("\\n")[0] || "";
        const desc = paragraphs[1] || text || "";
        const href = a.href || "";
        const isDone = /completed|已完成/i.test(text) || 
                       Boolean(a.querySelector(".mee-icon-SkypeCircleCheck, svg.text-fgSuccess, img[src*='check']"));
        const isLocked = /unlocks tomorrow|明天解锁|locked/i.test(text);
        return {
          idx: idx,
          text: text,
          title: title,
          desc: desc,
          href: href,
          isDone: isDone,
          isLocked: isLocked,
          isActivated: /activated|已激活/i.test(text)
        };
      }).filter(c => !c.isDone && !c.isLocked && c.text.length > 5);
    })()"""

    cards = ev(cdp, extract_cards_js) or []
    if not cards:
        log('Explore on Bing 暂无可用待打卡卡片(均已完成或处于未解锁状态)')
        return '无可用卡片(跳过)'

    log('发现 %d 张待打卡探索卡片' % len(cards))

    # 3. 逐张卡片在新标签页中打开并执行精准搜索归因，主 /earn 标签页保持常驻监听
    done_count = 0
    fixed_query = None
    import urllib.parse
    for c in cards:
        idx = c['idx']
        text = c['text']
        title = c.get('title') or ''
        desc = c.get('desc') or ''

        # 从卡片标题和描述中提炼最优搜索词
        if not fixed_query:
            fixed_query = extract_semantic_explore_query(title, desc)
        query = fixed_query
        log('执行探索打卡[%d/%d]: %s -> 提取搜索词: "%s"' % (done_count + 1, len(cards), (title or text)[:35], query))

        t_before = set(tab_ids(cdp))

        # 点击卡片，在新标签页打开（绝不直接覆盖或导航主 /earn 标签，以保持 Rewards 会话与事件监听）
        click_card_js = f"""(() => {{
          const headings = Array.from(document.querySelectorAll("h2, h3, button, span, div")).filter(el => {{
            const t = (el.innerText || "").replace(/\\s+/g, " ").trim();
            return /^(?:Explore on Bing|在必应上探索|探索必应)/i.test(t) && t.length < 50;
          }});
          if (!headings.length) return false;
          const disc = headings[0].closest(".react-aria-Disclosure, [class*=Disclosure]") || headings[0].parentElement;
          const panel = disc ? (disc.querySelector(".react-aria-DisclosurePanel, [class*=DisclosurePanel]") || disc) : null;
          if (!panel) return false;
          const links = panel.querySelectorAll("a[href]");
          if (links && links[{idx}]) {{
              links[{idx}].target = "_blank";
              links[{idx}].click();
              return true;
          }}
          return false;
        }})()"""
        ev(cdp, click_card_js)
        time.sleep(random.uniform(2.5, 3.5))

        # 抓取新打开的子标签页
        new_tabs = []
        if hasattr(cdp, 'port'):
            try:
                all_tabs = _http_json(f'http://127.0.0.1:{cdp.port}/json/list', timeout=3)
                new_tabs = [t for t in all_tabs if t.get('id') not in t_before and t.get('type') == 'page']
            except Exception:
                pass

        target_cdp = cdp
        new_tab_id = None
        if new_tabs and ch:
            new_tab_id = new_tabs[0].get('id')
            try:
                target_cdp = ch.attach(new_tabs[0])
            except Exception:
                target_cdp = cdp
                new_tab_id = None

        search_url = f'https://www.bing.com/search?q={urllib.parse.quote_plus(query)}&form=ML2PCR&OCID=ML2PCR&PUBL=RewardsDO&CREA=ML2PCR&PC=ML2PCR'
        dwell = random.uniform(ex_min, ex_max)
        navigate(target_cdp, search_url)
        log('在搜索结果页停留 %.1f 秒等待服务器归因积分...' % dwell)
        time.sleep(dwell)
        try:
            ev(target_cdp, 'window.scrollBy(0, 400);')
            time.sleep(2.0)
        except Exception:
            pass

        # 归因完成，关闭子标签页，主 /earn 标签继续接收积分更新
        if new_tab_id and ch:
            try:
                ch.close_tab(new_tab_id)
            except Exception:
                pass
            time.sleep(1.5)

        done_count += 1
        time.sleep(random.uniform(1.5, 2.5))

    # 4. 重新刷新 /earn 回读最新指标
    navigate(cdp, 'https://rewards.bing.com/earn')
    time.sleep(random.uniform(3.0, 5.0))
    final_info = ev(cdp, find_explore_js) or {}
    prog_after = final_info.get('prog', done_count * 10)
    max_after = final_info.get('max', 40)
    log('Explore on Bing 打卡后回读进度: %d/%d' % (prog_after, max_after))
    return '完成%d个探索打卡(进度%d/%d)' % (done_count, prog_after, max_after)


# ---------- 日常任务卡 (More Activities) ----------

def phase_moreactivities(name, cdp, pr, ch=None, budget_seconds=1500, log=print):
    """日常任务(More Activities 更多活动卡)。"""
    navigate(cdp, 'https://rewards.bing.com/earn')
    time.sleep(random.uniform(2.5, 4.0))
    # 触发页面下滚以完成 Next.js 组件水合与卡片懒加载
    ev(cdp, 'window.scrollTo(0, document.body.scrollHeight/2);')
    time.sleep(1.0)
    ev(cdp, 'window.scrollTo(0, document.body.scrollHeight);')
    time.sleep(1.0)

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
        wmin = int(st.get('cards', {}).get('attribution_wait_min', 60))
        wmax = int(st.get('cards', {}).get('attribution_wait_max', 120))
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
        container_snapshot(cdp, 'moreactivities', log=log, tag='[P5] ')
        return '日常任务已全部完成(共%d张)' % done_count
    container_snapshot(cdp, 'moreactivities', log=log, tag='[P5] ')
    return '部分完成:处理%d张(剩%d张待做)' % (done_count, remaining_ma)


phase_earn = phase_moreactivities



def extract_claim_points(text: str) -> int:
    """从待领取卡片或按钮文案中精准提取待领取积分数值，杜绝匹配到用户余额或其它无关数字。"""
    if not text:
        return 0
    clean = re.sub(r'(\d+),(\d+)', r'\1\2', str(text).strip())
    patterns = [
        r'(?:可领取|待领取|ready\s*to\s*claim|claimable)\s*[:：]?\s*(\d+)',
        r'(\d+)\s*(?:待领取|可领取|pending\s*claim|pending)',
        r'(?:claim|collect)\s*(\d+)',
        r'(\d+)\s*(?:分|points|pts)\b',
    ]
    for pat in patterns:
        m = re.search(pat, clean, re.IGNORECASE)
        if m:
            return int(m.group(1))
    clean_no_bal = re.sub(r'(?:可用积分|available\s*points|balance)\s*[:：]?\s*\d+', '', clean, flags=re.IGNORECASE)
    m = re.search(r'\b(\d+)\b', clean_no_bal)
    if m:
        return int(m.group(1))
    return 0


def phase_claim(name, cdp, pr, ch=None, log=print):
    """检查并领取待领取积分 (两级抽屉精准交互 + 原地响应等待与真实余额回读核销)。"""
    # 待领取卡片主要渲染在 Rewards Dashboard 首页，部分区域在 /earn
    log('开始检查是否有可领取积分...')
    navigate(cdp, 'https://rewards.bing.com/dashboard')
    time.sleep(random.uniform(3.5, 5.0))

    read_balance_js = '''(() => {
      const t = document.body ? document.body.innerText : '';
      const m = t.match(/(?:可用积分|Available points|balance)[^\\d]*([\\d,]+)/i) || t.match(/([\\d,]+)\\s*(?:可用积分|points)/i);
      return m ? parseInt(m[1].replace(/,/g, ''), 10) : null;
    })()'''

    # 1. 读当前可用余额作为基准
    bal_before = ev(cdp, read_balance_js)

    # 2. 定位外层待领取卡片/按钮 (中英文适配，如 "可领取 6 领取", "Ready to claim 15 Claim")
    # 限制文案长度并按最精确的子元素匹配，防止选中包裹用户资料的大容器
    claim_card_js = '''(() => {
      const btns = Array.from(document.querySelectorAll('button, [role="button"], a'));
      const candidates = btns.filter(b => {
        const t = (b.innerText || '').trim().replace(/\\s+/g, ' ');
        if (!t || t.length > 80) return false;
        return (/可领取|待领取|ready\\s*to\\s*claim|claimable/i.test(t) || (/^claim\\b/i.test(t) && /\\d+/.test(t))) &&
               /\\d+/.test(t) && !/已领取|completed/i.test(t);
      });
      if (!candidates.length) return null;
      candidates.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
      return candidates[0];
    })()'''

    has_card = ev(cdp, f"!!({claim_card_js})")
    current_page = 'https://rewards.bing.com/dashboard'
    if not has_card:
        # 也尝试 earn 页面
        navigate(cdp, 'https://rewards.bing.com/earn')
        time.sleep(random.uniform(2.5, 3.5))
        has_card = ev(cdp, f"!!({claim_card_js})")
        if has_card:
            current_page = 'https://rewards.bing.com/earn'

    if not has_card:
        log('今日无可领取积分 (未发现待领取卡片), 跳过')
        return '无可领取积分(跳过)'

    card_text = ev(cdp, f"({claim_card_js}) ? ({claim_card_js}).innerText.replace(/\\s+/g, ' ').trim() : ''")
    total_claimable = extract_claim_points(card_text)
    if total_claimable == 0:
        log(f'待领取卡片显示 0 分可领 ("{card_text}"), 跳过')
        return '无可领取积分(跳过)'
    log(f'检测到待领取卡片: "{card_text}" (约 {total_claimable} 分)')

    # 3. 第一步：点击外层卡片展开 Dialog 抽屉 (单次精准点击，结合轮询等待挂载，杜绝连击反向关闭)
    log('正在点击外层待领取卡片以展开领取抽屉...')
    opened = False
    try:
        human_click(cdp, claim_card_js, label='待领取卡片', close_new=False)
    except Exception:
        pass

    # 轮询等待抽屉就绪 (React Aria 抽屉滑入动画通常需要 1~4 秒)
    for _ in range(12):
        time.sleep(0.5)
        opened = ev(cdp, '!!document.querySelector(\'[role="dialog"], section[role="dialog"]\')')
        if opened:
            break

    # 若轮询后仍未打开，尝试原生 DOM click 一次作为兜底再等待
    if not opened:
        ev(cdp, f"(() => {{ const b = ({claim_card_js}); if (b) b.click(); }})()")
        for _ in range(8):
            time.sleep(0.5)
            opened = ev(cdp, '!!document.querySelector(\'[role="dialog"], section[role="dialog"]\')')
            if opened:
                break

    # 4. 第二步：在抽屉内部点击二次确认按钮 (例如 "6 待领取 领取积分" / "15 Pending Claim points")
    confirm_btn_js = '''(() => {
      const d = document.querySelector('[role="dialog"], section[role="dialog"], [role="alertdialog"], .modal');
      if (!d) return null; // 严格限定在弹窗作用域内，绝不回退至全局 document，防止误选外层卡片
      const scope = Array.from(d.querySelectorAll('button, a, [role="button"]'));
      const candidates = scope.filter(el => {
        const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
        if (!t || t.length > 60) return false;
        return (
          t.includes('领取积分') ||
          /claim\\s*points/i.test(t) ||
          /claim\\s*now/i.test(t) ||
          /\\d+\\s*(?:待领取|pending)\\s*(?:领取积分|claim)/i.test(t) ||
          /^(?:领取|claim|collect)$/i.test(t)
        );
      });
      if (!candidates.length) return null;
      candidates.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
      return candidates[0];
    })()'''

    has_confirm = False
    for _ in range(10):
        if ev(cdp, f"!!({confirm_btn_js})"):
            has_confirm = True
            break
        time.sleep(1.0)

    if not has_confirm:
        log('未在抽屉内找到可执行领取的确认按钮 (积分可能处于锁定期或待满足条件)')
        ev(cdp, "(() => { const c = document.querySelector('[role=\"dialog\"] button, section[role=\"dialog\"] button'); if (c) c.click(); })()")
        return f'检测到{total_claimable}分(待存入锁定中)' if total_claimable > 0 else '待存入锁定中'

    c_text = ev(cdp, f"({confirm_btn_js}) ? ({confirm_btn_js}).innerText.replace(/\\s+/g, ' ').trim() : ''")
    log(f'抽屉内确认领取按钮已就绪: "{c_text}", 正在触发领取结算...')

    # 抽屉滑入动画期间为防止坐标计算偏移，结合 human_click 与 DOM 原生 click 双重触发
    try:
        human_click(cdp, confirm_btn_js, label='确认领取按钮', close_new=False)
    except Exception:
        pass
    ev(cdp, f"(() => {{ const b = ({confirm_btn_js}); if (b) b.click(); }})()")

    # 关键：等待异步网络请求提交与服务端入账
    # 注意：严禁在点击确认后因抽屉关闭而立刻调用 navigate() 强刷！
    # Next.js Server Action 异步 POST /dashboard 与 RSC 渲染通常需要 2~8 秒，强刷会中断进行中的网络请求。
    # 我们在此优先原地轮询 DOM 变化与余额增长，最长耐心等待 12 秒。
    settled = False
    claimed_points = total_claimable

    for i in range(12):
        time.sleep(1.0)
        bal_now = ev(cdp, read_balance_js)
        if bal_before is not None and bal_now is not None and bal_now > bal_before:
            claimed_points = bal_now - bal_before
            log(f'积分领取已在页面原地结算入账！余额从 {bal_before} 增至 {bal_now} (+{claimed_points}分)')
            settled = True
            break

        card_t = ev(cdp, f"({claim_card_js}) ? ({claim_card_js}).innerText.replace(/\\s+/g, ' ').trim() : ''")
        if card_t:
            rem = extract_claim_points(card_t)
            if rem == 0:
                log(f'待领取卡片已原地核销为 0 分 ("{card_t}")')
                settled = True
                break
        elif not ev(cdp, f"!!({claim_card_js})"):
            log('待领取卡片已从页面消失 (已核销)')
            settled = True
            break

    # 若抽屉仍未自动关闭，尝试点击关闭按钮收起弹窗
    ev(cdp, """(() => {
      const d = document.querySelector('[role="dialog"], section[role="dialog"]');
      if (!d) return;
      const cls = d.querySelector('button[aria-label*="关闭" i], button[aria-label*="close" i], button.group\\\\/ctrl, button');
      if (cls) cls.click();
    })()""")
    time.sleep(1.0)

    if settled:
        # 已原地结算完成
        bal_now = ev(cdp, read_balance_js)
        if bal_now is not None:
            return f'成功领取{claimed_points}分(余额:{bal_now})'
        return f'成功领取{claimed_points}分'

    # 若 12 秒内未原地结算，此时网络 POST 请求已充分完成传输，刷新当前页面进行严格二次核销
    log('页面尚未原地更新，正在刷新页面以回读服务端积分状态...')
    navigate(cdp, current_page)
    time.sleep(random.uniform(4.0, 6.0))

    bal_after = ev(cdp, read_balance_js)
    if bal_before is not None and bal_after is not None and bal_after > bal_before:
        gained = bal_after - bal_before
        log(f'刷新后确认积分领取成功！余额从 {bal_before} 增至 {bal_after} (+{gained}分)')
        return f'成功领取{gained}分(余额:{bal_after})'

    card_after = ev(cdp, f"({claim_card_js}) ? ({claim_card_js}).innerText.replace(/\\s+/g, ' ').trim() : ''")
    rem_pts = extract_claim_points(card_after) if card_after else 0
    if not card_after or rem_pts == 0:
        log(f'刷新后待领取卡片已核销 (原{total_claimable}分)')
        return f'成功领取{total_claimable}分'

    log(f'卡片仍显示待领取 ({card_after})，可能由于代理网络延迟或服务端排队，将在回补阶段复检')
    return f'领取结算中(约{rem_pts}分)'


def auto_activate_promotions_and_streaks(cdp, ch=None, log=print, edge_activated=None):
    """自动扫描并激活 rewards 页面上所有待激活的福利特权、促销活动与连续打卡任务 (Perks & Streaks)。
    
    通用任务信息提取与自适应处理：
      - 任务识别: 提取标题、描述、收益类别 (如 double_search/edge_streak/visual_streak/general_promo)、CTA文字与目标URL。
      - 任务类型覆盖:
        1. 福利与特权解锁 (Perk Unlocked / 福利已解锁):
           如“限时每天搜索次数翻倍，赚取更多积分” (2X search opt-in, WW_banner_optin_*) 等弹出窗口激活任务。
        2. 连续打卡任务 (Streaks):
           Edge Browsing Streak, Visual Search Streak, Bing Search Streak 等侧边抽屉/弹窗激活任务。
        3. 其他未激活但可立即参与的限时特惠活动 (Limited-time Promotional Opt-ins)。
      - 状态防护: 自动排除已完成 (Completed / 已完成 / Activated / 已激活) 的卡片，避免重复触发。
      - 交互闭环: 模拟真人点击卡片展开弹窗 -> 提取并记录通用任务详情 -> 点击弹出窗口内的“立即激活”/“Activate” -> 处理外部/落地页跳转与原生双重触发 -> 安全关闭弹窗。

    2026-11-11 用户口径 + 真机日志: 激活是**每个账号一次性**的事 —— 新号首次下发 Edge 30 分钟 /
    视觉搜索时需要先激活才会计分, 之后每天自动运行**不需要**再激活。
    实测旧实现每天(甚至每次重跑)都点一次 "[Edge 专属入口]", 某US号/某SG号 一天两次、
    某SG号2 诊断轮 4 分钟点 6 次。现在由调用方传入 API 事实 `edge_activated`
    (即 /dapi/me 里已有 edge_browsing_streak_flight), 已激活就整段跳过。
    """
    find_tasks_js = '''(() => {
      const results = [];
      const clickables = Array.from(document.querySelectorAll('button, div[role="button"], a, span[role="link"], [data-react-aria-pressable="true"], [role="button"], [role="link"]'));
      
      const perkPattern = /(?:福利已解锁|福利解锁|特权已解锁|特权解锁|搜索次数翻倍|搜索翻倍|限时翻倍|每日搜索翻倍|双倍积分|限时特惠|限时福利|perk\\s*unlocked|perks\\s*unlocked|benefit\\s*unlocked|reward\\s*unlocked|double\\s*search|2x\\s*search|search\\s*2x|double\\s*daily\\s*search|2x\\s*points|opt[\\s-]?in|limited\\s*time\\s*search)/i;
      const streakPattern = /(?:activate\\s*streak|how\\s*to\\s*activate|如何激活|开始连续打卡|start\\s*streak|join\\s*streak|inactive|pending\\s*activation|browse\\s*on\\s*edge|browse\\s*with\\s*edge|在\\s*edge\\s*上浏览)/i;
      const activateCtaPattern = /(?:立即激活|activate\\s*now|activate|激活|开启|加入|get\\s*started|start\\s*browsing|start|开始)/i;
      const completedPattern = /(?:completed|已完成|activated|已激活|claimed|已领取)/i;

      const seenIds = new Set();

      for (const b of clickables) {
        const t = (b.innerText || '').trim();
        const aria = (b.getAttribute('aria-label') || '').trim();
        const alt = (b.querySelector('img')?.getAttribute('alt') || '').trim();
        const pText = (b.parentElement ? b.parentElement.innerText || '' : '').trim();
        const combined = (t + ' ' + aria + ' ' + alt + ' ' + pText).replace(/\\s+/g, ' ');

        // 排除已完成状态 (如果整个文字块明确显示已完成且无激活按钮，则跳过)
        if (completedPattern.test(t) || (completedPattern.test(combined) && !activateCtaPattern.test(t) && !perkPattern.test(t))) {
          continue;
        }

        // 带 x/y 个任务 的是打卡卡(归 quests 阶段处理), 不是"待激活"特权卡。
        // 否则 onboarding 6/7 这种跨日卡每天都会被当成待激活再点一次(2026-11-11 实测)。
        if (/(\\d+)\\s*\\/\\s*(\\d+)\\s*(?:个任务|个活動|tasks|days|天)/i.test(combined)) {
          continue;
        }

        const isPerk = perkPattern.test(combined);
        const isStreak = streakPattern.test(combined) || (activateCtaPattern.test(t) && /streak|打卡/i.test(combined));
        const isGenericActivate = activateCtaPattern.test(t) && !completedPattern.test(combined);

        if (isPerk || isStreak || isGenericActivate) {
          const cardTitle = b.querySelector('p, h1, h2, h3, [class*="title" i], [class*="Strong" i]')?.innerText || 
                            alt || 
                            t.split('\\n')[0] || 
                            t.slice(0, 30);
          const cardDesc = Array.from(b.querySelectorAll('p, span, div'))
                            .map(el => (el.innerText || '').trim())
                            .filter(txt => txt && txt !== cardTitle && !completedPattern.test(txt) && txt.length > 5)[0] || '';

          const cid = b.id || (cardTitle + '_' + (isPerk ? 'perk' : 'streak'));
          if (seenIds.has(cid)) continue;
          seenIds.add(cid);

          const isDouble = /翻倍|double|2x/i.test(combined);
          const isEdge = /edge|browsing/i.test(combined);
          const isVisual = /visual|视觉/i.test(combined);

          let benefitCategory = 'general_promo';
          if (isDouble) benefitCategory = 'double_search';
          else if (isEdge) benefitCategory = 'edge_streak';
          else if (isVisual) benefitCategory = 'visual_streak';

          results.push({
            id: b.id || null,
            title: cardTitle.replace(/\\n/g, ' ').trim(),
            description: cardDesc.replace(/\\n/g, ' ').trim(),
            fullText: t.replace(/\\n/g, ' ').trim(),
            isPerk: isPerk,
            isStreak: isStreak,
            isDoubleSearch: isDouble,
            isEdge: isEdge,
            isVisual: isVisual,
            category: benefitCategory
          });
        }
      }
      return results;
    })()'''

    navigate(cdp, 'https://rewards.bing.com/earn')
    time.sleep(random.uniform(2.5, 4.0))
    unactivated = ev(cdp, find_tasks_js) or []

    if not unactivated:
        # 回退至 dashboard 页面进行二次扫描
        navigate(cdp, 'https://rewards.bing.com/dashboard')
        time.sleep(random.uniform(2.5, 3.5))
        unactivated = ev(cdp, find_tasks_js) or []

    activated_types = []
    if unactivated:
        log('[特权与打卡自动激活] 发现 %d 个待激活任务: %s' % (
            len(unactivated), ['%s (%s)' % (u['title'], u['category']) for u in unactivated]))

    for s in unactivated:
        title = s['title']
        cat = s['category']
        log('>>> 正在激活任务: %s (分类: %s)...' % (title, cat))
        
        # 提取并记录通用任务信息
        log('[通用任务信息] 标题: "%s" | 收益描述: "%s" | 类别: %s' % (
            title, s.get('description') or s.get('fullText', '')[:60], cat))

        # 点击展开卡片抽屉/弹窗 (兼容 id、innerText、alt 匹配，并定位到真正的可点击外层元素)
        click_card_js = ('''(() => {
          const clickables = Array.from(document.querySelectorAll('button, div[role="button"], a, span[role="link"], [data-react-aria-pressable="true"], [role="button"], [role="link"]'));
          const targetTitle = %s;
          const targetId = %s;
          for (const b of clickables) {
            const t = (b.innerText || '');
            const alt = b.querySelector('img')?.getAttribute('alt') || '';
            if ((targetId && b.id === targetId) || t.includes(targetTitle) || alt.includes(targetTitle)) {
              let clickable = b;
              while (clickable && clickable !== document.body) {
                if (clickable.getAttribute('role') === 'link' || 
                    clickable.getAttribute('role') === 'button' ||
                    clickable.tagName === 'BUTTON' ||
                    clickable.tagName === 'A' ||
                    clickable.getAttribute('tabindex') === '0' ||
                    clickable.getAttribute('data-react-aria-pressable') === 'true') {
                  break;
                }
                clickable = clickable.parentElement;
              }
              const target = clickable || b;
              target.scrollIntoView({behavior: 'instant', block: 'center'});
              target.click();
              return true;
            }
          }
          return false;
        })()''' % (json.dumps(title), json.dumps(s.get('id') or '')))

        opened = ev(cdp, click_card_js)
        time.sleep(random.uniform(2.0, 3.0))

        # 抽屉/弹窗内部目标激活按钮扫描与元数据提取
        act_target_js = '''(() => {
          const dialog = document.querySelector('dialog, [role="dialog"], [role="alertdialog"], section[role="dialog"], [class*="modal" i]');
          if (!dialog) return null;
          const cand = Array.from(dialog.querySelectorAll('a, button, [role="button"]')).find(el => {
            const t = (el.innerText || '').toLowerCase();
            const h = (el.getAttribute('href') || '').toLowerCase();
            return t.includes('激活') || t.includes('activate') || t.includes('start') || t.includes('开启') || t.includes('加入') || 
                   h.includes('features=vsstreak') || h.includes('features=') || h.includes('streak') || 
                   h.includes('modal=pointbreakdown') || h.includes('form=ML2XVP') || h.includes('optin');
          });
          if (cand) {
            return {
              tag: cand.tagName, 
              href: cand.href || cand.getAttribute('href') || null, 
              text: (cand.innerText || '').trim(),
              dialogTitle: dialog.querySelector('h1, h2, h3, p, [class*="title" i]')?.innerText || '',
              dialogText: (dialog.innerText || '').slice(0, 300)
            };
          }
          return null;
        })()'''
        target = ev(cdp, act_target_js)
        if target:
            log('[弹窗任务确认] 检测到激活操作: %s -> %s (弹窗标题: "%s")' % (
                target.get('text'), target.get('href') or 'DOM按钮', target.get('dialogTitle') or title))
            target_url = target.get('href')

            # 优先在当前弹窗中触发原生点击
            try:
                ev(cdp, '''(() => {
                  const dialog = document.querySelector('dialog, [role="dialog"], [role="alertdialog"], section[role="dialog"], [class*="modal" i]');
                  if (!dialog) return;
                  const cand = Array.from(dialog.querySelectorAll('button, a, [role="button"]')).find(el => {
                    const t = (el.innerText || '').toLowerCase();
                    const h = (el.getAttribute('href') || '').toLowerCase();
                    return t.includes('激活') || t.includes('activate') || t.includes('start') || t.includes('开启') || t.includes('加入') ||
                           h.includes('features=vsstreak') || h.includes('features=') || h.includes('streak') || 
                           h.includes('modal=pointbreakdown') || h.includes('form=ML2XVP') || h.includes('optin');
                  });
                  if (cand) cand.click();
                })()''')
                time.sleep(2.5)
            except Exception as ex:
                log('点击激活按钮异常: %s' % ex)

            # 若激活目标包含特定链接，执行新标签或直接导航兜底
            if target_url:
                try:
                    if target_url.lower().startswith('microsoft-edge:'):
                        real_url = re.sub(r'^microsoft-edge:(?://)?', '', target_url, flags=re.I)
                        if not real_url.startswith('http'):
                            real_url = 'https://' + real_url
                        if ch:
                            new_tab = ch.new_tab(real_url)
                            time.sleep(random.uniform(4.0, 6.0))
                            try:
                                new_cdp = ch.attach(new_tab)
                                ev(new_cdp, '''(() => {
                                  const a = Array.from(document.querySelectorAll('a, button')).find(el => {
                                    const h = el.getAttribute('href') || el.href || '';
                                    const t = (el.innerText || '').trim();
                                    return h.includes('rwgbopen=1') || t === '开始' || t === 'Get started' || t.includes('开始在 Edge 上浏览');
                                  });
                                  if (a) a.click();
                                })()''')
                                time.sleep(random.uniform(2.5, 4.0))
                            except Exception:
                                pass
                            ch.close_tab(new_tab.get('id'))
                    elif 'modal=pointbreakdown' in target_url or 'form=ML2XVP' in target_url or 'optin' in target_url:
                        curr_url = ev(cdp, 'window.location.href') or ''
                        if 'form=ML2XVP' not in curr_url and 'modal=pointbreakdown' not in curr_url:
                            navigate(cdp, target_url)
                            time.sleep(random.uniform(3.0, 5.0))
                    elif ch:
                        new_tab = ch.new_tab(target_url)
                        time.sleep(random.uniform(3.0, 5.0))
                        ch.close_tab(new_tab.get('id'))
                except Exception as ex:
                    log('激活访问目标URL异常: %s' % ex)

            # 记录激活类型
            if s.get('isDoubleSearch') or cat == 'double_search' or '翻倍' in title or '2x' in title.lower():
                activated_types.extend(['double_search', 'search_2x', 'perk'])
            if s.get('isEdge') or 'edge' in title.lower() or 'browsing' in title.lower():
                activated_types.append('edge')
            if s.get('isVisual') or 'visual' in title.lower() or '视觉' in title:
                activated_types.append('visual')
            if s.get('isPerk') or 'perk' in title.lower() or '福利' in title:
                activated_types.append('perk')

            log('已成功激活特权/任务: %s' % title)

            # 关闭抽屉/弹窗
            try:
                ev(cdp, '''(() => {
                  const dialog = document.querySelector('dialog, [role="dialog"], [role="alertdialog"], section[role="dialog"], [class*="modal" i]');
                  if (!dialog) return;
                  const cls = dialog.querySelector('button[aria-label*="关闭" i], button[aria-label*="close" i], button.group\\\\/ctrl') ||
                              Array.from(dialog.querySelectorAll('button')).find(b => (b.innerText || '').trim().toLowerCase() === 'close');
                  if (cls) cls.click();
                })()''')
                time.sleep(1.0)
            except Exception:
                pass
        else:
            log('未能定位到弹窗激活按钮或该卡片已激活')

    # 额外触发 Edge 专属奖励面板入口 (触发 Edge 浏览识别与连续打卡激活)
    # 只在"尚未激活"时做一次: /dapi/me 已有 edge_browsing_streak_flight 就说明激活过, 不再重复点击
    if edge_activated:
        log('[激活跳过] Edge 打卡已激活 (flight 在册), 不再重复点击入口')
    elif ch:
        try:
            edge_tab = ch.new_tab('https://www.bing.com/?rwgbopen=1&FORM=rwbsl1')
            time.sleep(random.uniform(2.5, 3.5))
            edge_cdp = ch.attach(edge_tab)
            clicked = ev(edge_cdp, '''(() => {
              const cand = Array.from(document.querySelectorAll('a, button, [role="button"]')).find(el => {
                const t = (el.innerText || '').trim();
                const h = el.getAttribute('href') || el.href || '';
                return /开始在 Edge 上浏览|开始|Get started|Start browsing|Activate streak|立即激活/i.test(t) ||
                       h.includes('rwgbopen=1');
              });
              if (cand) { cand.click(); return true; }
              return false;
            })()''')
            if clicked:
                log('[Edge 专属入口] 成功点击 Edge 浏览打卡/特权激活按钮')
                if 'edge' not in activated_types:
                    activated_types.append('edge')
            time.sleep(1.0)
            ch.close_tab(edge_tab.get('id'))
        except Exception as ex_edge:
            log(f'[Edge 专属入口激活] 容错(跳过): {ex_edge}')

    return list(set(activated_types))


auto_activate_streaks = auto_activate_promotions_and_streaks


def phase_perks(name, cdp, pr, ch=None, log=print):
    """浏览器阶段: 扫描并激活所有待激活的福利特权与促销活动 (Perks & Promotions)。"""
    activated = auto_activate_promotions_and_streaks(cdp, ch=ch, log=log)
    if activated:
        return '成功激活%d项特权/促销(%s)' % (len(activated), ', '.join(activated))
    return '无可激活福利/特权(跳过)'


