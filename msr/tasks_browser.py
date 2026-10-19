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


def phase_search(name, cdp, pr, ch=None, budget_seconds=None, target=None, log=print):
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
    if c_api and c_api.get('complete'):
        log('PC搜索已满额: %d/%d (根据促销属性)' % (c_api['progress'], c_api['max']))
        return '%d/%d' % (c_api['progress'], c_api['max'])

    # 检查 dashboard streaks
    target_max = (c_api or {}).get('max', 60)
    current_prog = (c_api or {}).get('progress', 0)
    try:
        navigate(cdp, 'https://rewards.bing.com/dashboard')
        time.sleep(2)
        c_dom = search_counters_dom(cdp)
        if c_dom:
            target_max = c_dom.get('max') or target_max
            current_prog = c_dom.get('progress') or current_prog
            if c_dom.get('complete') or current_prog >= target_max:
                log('PC搜索已满额: %d/%d (根据仪表板)' % (current_prog, target_max))
                return '%d/%d' % (current_prog, target_max)
    except Exception:
        pass

    log('PC搜索启动: 当前进度 %d/%d (单次停留 %.1f-%.1f 分钟/词)' % (
        current_prog, target_max, dwell_min / 60, dwell_max / 60))

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
            scroll_human(cdp)
            log('搜索[%d] %s -> %s' % (searches_done, q, str(href)[:60]))
            consecutive_errors = 0

            # 模拟真人点击任意搜索结果并停留浏览
            if random.random() < click_prob:
                try:
                    result_links = ev(cdp, """(() => {
                        const anchors = [...document.querySelectorAll('#b_results .b_algo h2 a')];
                        return anchors.filter(a => a.href && a.offsetParent).slice(0, 8).map(a => a.href);
                    })()""") or []
                    if result_links:
                        pick = random.choice(result_links)
                        navigate(cdp, pick)
                        scroll_human(cdp)
                except Exception:
                    pass
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

        # 核心节奏等待: 3-5 分钟 (等待微软冷却计数器刷新)
        dwell = random.uniform(dwell_min, dwell_max)
        log('搜索[%d] 模拟真人阅读停留 %.1f 分钟...' % (searches_done, dwell / 60))
        time.sleep(dwell)

        # 回读积分进度
        new_c = None
        try:
            navigate(cdp, 'https://rewards.bing.com/dashboard')
            time.sleep(2)
            new_c = search_counters_dom(cdp)
        except Exception:
            pass

        if new_c:
            current_prog = new_c.get('progress', current_prog)
            target_max = new_c.get('max', target_max)
            log('进度回读: %d/%d' % (current_prog, target_max))

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
            time.sleep(random.uniform(3, 6))
    finally:
        try:
            cdp_quiz.close()
        except Exception:
            pass


def phase_dailyset(name, cdp, pr, ch=None, budget_seconds=1500, log=print):
    """Daily Set 3张每日活动卡。"""
    navigate(cdp, 'https://rewards.bing.com/dashboard')
    time.sleep(random.uniform(3, 5))

    # 获取 dailyset 卡片列表
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
        return '未发现每日活动'

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


# ---------- 任务打卡 (Quests / Punchcards，含桌面专属打卡) ----------

def phase_quests(name, cdp, pr, ch=None, budget_seconds=1500, log=print):
    """任务打卡(Quests / Punchcards，含桌面专属打卡)。"""
    navigate(cdp, 'https://rewards.bing.com/earn')
    time.sleep(random.uniform(3, 5))

    quests = ev(cdp, """(() => {
      const qs = document.getElementById("quests");
      if (!qs) return [];
      return Array.from(qs.querySelectorAll("a[href*='/earn/quest/']")).map(a => {
        const text = a.innerText.replace(/\\n/g, ' ');
        return {
          href: a.href,
          text: text,
          isDone: text.includes("已完成") || /\\b(\\d+)\\s*\\/\\s*\\1\\s*个任务/.test(text)
        };
      });
    })()""") or []

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
              const anchors = Array.from(document.querySelectorAll("main a[href]")).filter(a => {
                const p = a.closest("div[class*=flex-col], div[class*=rounded]") || a.parentElement;
                const isSuccess = p && p.innerHTML.includes("bg-statusSuccess");
                if (isSuccess) return false;

                const t = a.innerText || "";
                return a.href.includes("bing.com/search") || a.href.includes("ms-search://") ||
                       t.includes("分") || t.includes("搜索") || t.includes("尝试") ||
                       t.includes("定义") || t.includes("查找") || t.includes("试用");
              });
              if (anchors.length === 0) return null;
              const target = anchors[0];
              if (target.href.startsWith("ms-search://")) {
                let q = "";
                try {
                  const u = new URL(target.href);
                  q = u.searchParams.get("q") || "";
                } catch(e) {
                  const m = target.href.match(/[?&]q=([^&]+)/);
                  if (m) q = decodeURIComponent(m[1]);
                }
                target.href = "https://www.bing.com/search?q=" + encodeURIComponent(q || "today news");
                target.target = "_blank";
              }
              let hasQ = false;
              try {
                const u = new URL(target.href, location.href);
                hasQ = !!u.searchParams.get("q");
              } catch(e) {}
              return {
                text: target.innerText.replace(/\n/g, ' ').trim(),
                href: target.href,
                hasQuery: hasQ
              };
            })()""")

            if not step_info:
                log('%s 今日无更多待做步骤' % q_name)
                break

            step_text = step_info['text']
            step_href = step_info['href']
            if step_href in seen_step_urls:
                log('%s 步骤已处理过(需等待冷却或次日刷新)，进入下一打卡' % q_name)
                break
            seen_step_urls.add(step_href)

            log('执行步骤: %s -> %s' % (step_text[:30], step_href[:60]))

            click_selector = """(() => {
              const el = Array.from(document.querySelectorAll("main a[href]")).find(a => {
                const p = a.closest("div[class*=flex-col], div[class*=rounded]") || a.parentElement;
                if (p && p.innerHTML.includes("bg-statusSuccess")) return false;
                const t = a.innerText || "";
                return a.href.includes("bing.com/search") || a.href.includes("ms-search://") ||
                       t.includes("分") || t.includes("搜索") || t.includes("尝试") ||
                       t.includes("定义") || t.includes("查找") || t.includes("试用");
              });
              if (el && el.href.startsWith("ms-search://")) {
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
                    # 若搜索链接无 q (如 form=ML2YBF 开始搜索)，在目标标签真实发起一次搜索
                    if not step_info.get('hasQuery') and 'bing.com' in step_href:
                        try:
                            c2 = ch.attach({'id': target_tid, 'webSocketDebuggerUrl': f'ws://127.0.0.1:{ch.port}/devtools/page/{target_tid}'})
                            time.sleep(2)
                            focused = ev(c2, """(() => {
                              const i = document.querySelector('#sb_form_q, textarea[name=q], input[name=q]');
                              if (i) { i.focus(); return 'OK'; }
                              return null;
                            })()""")
                            if focused:
                                c2.call('Input.insertText', {'text': 'today news'})
                                time.sleep(random.uniform(1.0, 1.8))
                                press_enter(c2)
                                time.sleep(4)
                            c2.close()
                        except Exception as ex:
                            log('附带搜索容忍: %s' % str(ex)[:60])
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
    time.sleep(random.uniform(3, 5))

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
