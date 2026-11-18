"""视觉搜索任务（适配最新版 Rewards 连续打卡与新手激活机制）。

核心机制与实操规范:
- 连续打卡卡片优先渲染在 `https://rewards.bing.com/earn`，未激活时呈现“如何激活”/“How to activate”/“Activate streak”；
- 激活通道：展开卡片抽屉，点击“激活连续打卡”链接 (携带 features=vsstreak,vstooltip&form=ML2XES)；
- 镜头按钮：新版 Bing 首页桌面为 `#sb_cam_island` (可见且尺寸 24x24)，浮层输入框为 `#sb_imgpst`；
- 提交判定：填入壁纸直链 + Enter 派发，等待 `/search?q=...&bcid=...` 落页并停留 15s 归因；
- 状态裁决：回读卡片 Activity: 1/1、活动: 1/1 或落页归因成功判定。
"""
import json
import random

from msr.tasks import streaks
import time
import urllib.parse
import urllib.request

from msr.browser import ev, navigate, StepError, human_click

EARN = 'https://rewards.bing.com/earn'
HOME = 'https://www.bing.com/?features=vsstreak,vstooltip&form=ML2XES'

# 现代 Rewards 页面视觉搜索卡片状态提取
STATUS_CARD_JS = '''(() => {
  const btns = Array.from(document.querySelectorAll('button, div[role="button"], a'));
  const card = btns.find(b => {
    const t = (b.innerText || '').toLowerCase();
    const aria = (b.getAttribute('aria-label') || '').toLowerCase();
    return t.includes('视觉搜索') || t.includes('visual search') || 
           aria.includes('视觉搜索') || aria.includes('visual search') ||
           b.querySelector('img[src*="/search_visual.svg"]');
  });
  if (!card) return null;

  const text = (card.innerText || '').replace(/\\s+/g, ' ').trim();
  
  // 1. 优先提取当天具体的活动比例指标 (例如 活动: 0/1, 活动: 1/1, Activity: 0/1, Activity: 1/1)
  const ratioMatch = text.match(/\\b(?:活动|activity|tasks?):\\s*(\\d+)\\s*\\/\\s*(\\d+)\\b/i) || text.match(/\\b(\\d+)\\s*\\/\\s*(\\d+)\\b/);
  if (ratioMatch) {
    const p = Number(ratioMatch[1]), max = Number(ratioMatch[2]);
    return {
      progress: p,
      max: max,
      complete: p >= max && max > 0,
      found: true,
      unactivated: false,
      source: 'card_ratio',
      text: text
    };
  }

  // 2. 待激活 (如何激活 / How to activate / Activate streak)
  if (/如何激活|how to activate|activate streak/i.test(text) || /\\b激活\\b/.test(text)) {
    return {progress: 0, max: 1, complete: false, found: true, unactivated: true, source: 'card_unactivated', text: text};
  }

  // 3. 排除连续打卡累计天数干扰 (例如 "已完成连续打卡 2 天" / "Day 2 of 7 streak completed") 后再判断是否已完成
  const cleanText = text.replace(/已完成连续打卡\\s*\\d+\\s*天/gi, '')
                        .replace(/Day\\s*\\d+\\s*(?:of\\s*\\d+)?\\s*streak\\s*completed/gi, '');
  if (/已完成|completed/i.test(cleanText)) {
    return {progress: 1, max: 1, complete: true, found: true, unactivated: false, source: 'card_done', text: text};
  }

  return {progress: 0, max: 1, complete: false, found: true, unactivated: false, source: 'card_default', text: text};
})()'''

LENS_PICK = '''(() => {
  const vis = x => x && x.getBoundingClientRect().width > 0 && x.getBoundingClientRect().height > 0;

  // 1. 结构化首页及搜索页镜头 (优先点击具备 role="button" 的内层交互节点，避免只点中外层容器)
  const structural = [
    document.querySelector('#sb_cam_island .b_icon'),
    document.querySelector('#sb_cam_island [role="button"]'),
    document.querySelector('#sb_cam_island'),
    document.querySelector('.sbi_hpicn'),
    document.querySelector('[data-island="cam"] [role="button"]'),
    document.querySelector('[data-island="cam"]'),
    document.querySelector('#sb_sbi'),
    document.querySelector('#sbi_b'),
    document.querySelector('.hp_island_host.camera [role="button"]'),
    document.querySelector('.hp_island_host.camera'),
    document.querySelector('#sb_form .camera')
  ];
  for (const el of structural) {
    if (vis(el)) return el;
  }

  // 2. 属性语义入口
  const isLensAria = s => {
    if (!s) return false;
    const l = s.toLowerCase();
    return (
      (l.includes('image') && l.includes('search')) ||
      (l.includes('visual') && l.includes('search')) ||
      (l.includes('search') && l.includes('by image')) ||
      l.includes('图像搜索') || l.includes('图片搜索') ||
      l.includes('视觉搜索') || l.includes('识图') ||
      l.includes('按图像搜索') || l.includes('使用图像搜索')
    );
  };

  const formCandidates = [
    ...document.querySelectorAll('#sb_form [aria-label], #sb_form [title], #sb_form [data-sbtip], #sb_form [data-tooltip], [role="button"]')
  ];
  for (const el of formCandidates) {
    if (isLensAria(el.getAttribute('aria-label')) ||
        isLensAria(el.getAttribute('title')) ||
        isLensAria(el.getAttribute('data-sbtip')) ||
        isLensAria(el.getAttribute('data-tooltip'))) {
      if (vis(el)) return el;
    }
  }

  return null;
})()'''

BOX_READY = '''(() => {
  const vis = x => x && x.getBoundingClientRect().width > 0 && x.getBoundingClientRect().height > 0;
  const inps = [
    document.querySelector('#sb_imgpst'),
    document.querySelector('#sb_sbi_ipt'),
    document.querySelector('input.imgpst'),
    document.querySelector('#sbi_input'),
    document.querySelector('input[placeholder*="URL" i]'),
    document.querySelector('input[placeholder*="链接" i]'),
    document.querySelector('input[placeholder*="图像" i]'),
    document.querySelector('input[aria-label*="URL" i]'),
    document.querySelector('[role="dialog"] input[type=text]'),
    document.querySelector('#sbi_b input')
  ];
  for (const i of inps) {
    if (vis(i)) return true;
  }
  return false;
})()'''

LAND = '''(() => {
  try {
    const u = new URL(location.href);
    if (!u.hostname.endsWith('bing.com')) return false;
    if (u.pathname === '/search' || u.pathname === '/images/search') {
      return u.searchParams.has('bcid') || 
             (u.searchParams.get('FORM') || '').startsWith('SBI') || 
             (u.searchParams.get('features') || '').includes('vsstreak') ||
             (u.searchParams.get('iss') || '') === 'sbi';
    }
    return false;
  } catch(e) {
    return false;
  }
})()'''


def read_status_http(name, proxy=None, log=None):
    """HTTP 版状态: 命中返回与 read_status 同构的 dict, 读不到/缺对象返回 None(交给 DOM 判)。

    2026-11-13 修正: payload 里没有 visualsearch **不再**等于"账号未下发"。
    该 RSC 片段不稳定 —— 某新号2 20:47 读到 dailyset+visualsearch 双双缺失, 事后连读 10 次全有;
    某US号 同一轮 12 分钟内 dailyset 由有变无。缺对象现在只作诊断, 一律回退 DOM 由页面说话,
    页面没有 vsstreak 入口时自然报"未下发"(旧版本的判定路径, 从没出过这类漏做)。
    """
    diag = {}
    st = streaks.fetch_streaks(name, proxy=proxy, stats=diag)
    if st is None:
        if log:
            log('[视觉诊断] /earn 读取失败(status=%s) -> 改走 DOM' % diag.get('status'))
        return None
    v = st.get('visualsearch')
    if not v:
        if log:
            log('[视觉诊断] payload 无 visualsearch(partner=[%s], %d 字节) '
                '-> 不据此判未下发, 改走 DOM' % (','.join(diag.get('partners') or []) or '无',
                                              diag.get('bytes') or 0))
        return None
    return {'progress': v.get('completed') or 0, 'max': v.get('total') or 0,
            'complete': bool(v.get('complete')), 'found': True,
            'unactivated': v.get('enabled') is False,
            'source': 'rsc', 'detail': v}




def read_status(cdp, refresh=True):
    """读视觉进度。优先检测 earn 页面，若无再回退 dashboard。"""
    if refresh:
        navigate(cdp, EARN)
        time.sleep(random.uniform(3.0, 5.0))

    # 只读一次 /earn 的卡片(单层)。
    # 2026-11-11: 删掉原 PANEL_JS 全页 querySelectorAll('*') 扫描与 dashboard 换页重试 ——
    # 权威状态已改由 streaks.fetch_streaks() 从 RSC payload 一次读回, 这里只是它读不到时的兜底,
    # 10 天日志里 PANEL_JS / dashboard 兜底一次都没救回过来(67 次全是 card_ratio)。
    for _ in range(2):
        try:
            st = ev(cdp, STATUS_CARD_JS)
            if st and st.get('found'):
                return st
        except Exception:
            pass
        time.sleep(1.0)

    return {'progress': 0, 'max': 1, 'complete': False, 'found': False, 'source': 'unavailable'}


def get_entry_and_activate(cdp, ch=None, log=print):
    """视觉入口与打卡激活：展开卡片取官方 vsstreak 入口，若未激活则自动激活。"""
    default_entry = 'https://www.bing.com/?features=vsstreak,vstooltip&form=ML2XES'
    try:
        # 点击视觉卡片展开抽屉
        card_click_js = '''(() => {
          const btns = Array.from(document.querySelectorAll('button, div[role="button"]'));
          const card = btns.find(b => {
            const t = (b.innerText || '').toLowerCase();
            return t.includes('视觉搜索') || t.includes('visual search') || b.querySelector('img[src*="/search_visual.svg"]');
          });
          if (card) { card.click(); return true; }
          return false;
        })()'''
        clicked = ev(cdp, card_click_js)
        if not clicked:
            return default_entry

        time.sleep(random.uniform(1.5, 2.5))

        # 抽屉内部检测 vsstreak 链接或激活链接
        drawer_target_js = '''(() => {
          const dialog = document.querySelector('[role="dialog"]');
          if (!dialog) return null;
          const cand = Array.from(dialog.querySelectorAll('a[href]')).find(a => {
            const h = a.href.toLowerCase();
            return h.includes('features=vsstreak') || h.includes('features=') || h.includes('streak') || (a.innerText || '').includes('激活');
          });
          return cand ? cand.href : null;
        })()'''
        href = ev(cdp, drawer_target_js)

        # 真人点击抽屉内的激活链接以确保服务器激活归因 (仅当确实为激活按钮时点击，避免误点 Search now 等普通跳转)
        try:
            new_tabs = human_click(cdp, '''(() => {
              const dialog = document.querySelector('[role="dialog"]');
              if (!dialog) return null;
              return Array.from(dialog.querySelectorAll('a, button')).find(el => {
                const t = (el.innerText || '').toLowerCase();
                return t.includes('激活') || t.includes('activate');
              });
            })()''', label='激活打卡', close_new=False)
            if new_tabs and ch:
                time.sleep(3.0)
                for tid in new_tabs:
                    try:
                        ch.close_tab(tid)
                    except Exception:
                        pass
        except Exception:
            pass

        # 关闭抽屉
        ev(cdp, '''(() => {
          const dialog = document.querySelector('[role="dialog"]');
          const cls = dialog && dialog.querySelector('button[aria-label*="关闭" i], button[aria-label*="close" i], button.group\\\\/ctrl');
          if (cls) cls.click();
        })()''')
        time.sleep(1.0)

        if href:
            return href
    except Exception as ex:
        log('获取入口抽屉异常: %s, 采用标准 vsstreak 链接' % ex)

    return default_entry


def submit_image(cdp, image, entry, log=print, ch=None):
    """提交图片 URL 并等落页。"""
    # 彻底清除可能残留的瞬态首页状态 Cookie，杜绝 /images/kblob 接口报 400 Bad Request 导致落页超时
    try:
        cdp.call('Network.deleteCookies', {'name': 'SRCHHPGUSR', 'domain': '.bing.com'})
        cdp.call('Network.deleteCookies', {'name': 'SRCHHPGUSR', 'domain': 'www.bing.com'})
    except Exception:
        pass

    log('正在导航至视觉打卡入口: %s' % entry[:70])
    navigate(cdp, entry)
    time.sleep(random.uniform(3.0, 4.5))

    # 清理可能遮挡搜索栏与镜头的浮动横幅、未登录诱导框或隐私通知
    try:
        ev(cdp, '''(() => {
          const sels = ['.b_siprov2_body', '.b_siprov2', '#bnp_container', '#b_notificationContainer', '[id*="cookie-banner" i]'];
          for (const s of sels) {
            const el = document.querySelector(s);
            if (el) {
              const cls = el.querySelector('button[aria-label*="close" i], button[aria-label*="关闭" i], .b_close, .bnp_close');
              if (cls) cls.click();
              else el.remove();
            }
          }
        })()''')
    except Exception:
        pass

    opened = False
    for attempt in range(4):
        # 1. 尝试真人点击镜头
        try:
            human_click(cdp, LENS_PICK, label='视觉镜头', close_new=False)
        except Exception:
            pass
        time.sleep(1.0)

        # 检查输入框是否就绪
        if ev(cdp, BOX_READY):
            opened = True
            break

        # 2. 降级：直接派发原生 DOM click 事件
        ev(cdp, '''(() => {
          const el = (%s);
          if (el) {
            el.click();
            if (el.parentElement) el.parentElement.click();
            if (el.firstElementChild) el.firstElementChild.click();
          }
        })()''' % LENS_PICK)
        time.sleep(1.5)

        if ev(cdp, BOX_READY):
            opened = True
            break

    if not opened:
        raise StepError('视觉镜头/弹层多次尝试未就绪')

    log('视觉弹层与输入框已就绪，正在填入壁纸直链...')
    time.sleep(1.0)

    inp_js = '''(() => {
      const sels = ['#sb_imgpst', '#sb_sbi_ipt', 'input.imgpst', '#sbi_input', 'input[placeholder*="URL" i]', 'input[placeholder*="链接" i]', '[role="dialog"] input[type=text]'];
      for (const s of sels) {
        const el = document.querySelector(s);
        if (el && el.getBoundingClientRect().width > 0) return el;
      }
      return document.querySelector('#sb_imgpst') || document.querySelector('input');
    })()'''

    # 1. 物理点击聚焦并清空输入框
    try:
        human_click(cdp, inp_js, label='视觉输入框', close_new=False, timeout=2.0)
    except Exception:
        pass
    ev(cdp, '''((inp) => {
        if (inp) {
            inp.focus();
            inp.value = '';
            inp.dispatchEvent(new Event('input', {bubbles: true}));
        }
    })(%s)''' % inp_js)
    time.sleep(0.2)

    # 2. 采用 CDP 原生字符输入流 (Input.insertText) 填入直链文本
    try:
        cdp.call('Input.insertText', {'text': image})
    except Exception as ex:
        log('CDP Input.insertText 异常: %s, 降级采用 DOM 注入' % ex)
        ev(cdp, '''((inp, img) => {
            if (inp) {
                inp.value = img;
                inp.dispatchEvent(new Event('input', {bubbles: true}));
            }
        })(%s, %s)''' % (inp_js, json.dumps(image)))
    time.sleep(0.3)

    # 3. 模拟真实剪贴板粘贴流水线 (ClipboardEvent + DataTransfer)
    # 逆向证据证明：Bing 前端 ImagePaste/SbiUtil 核心绑定了 paste 事件，收到后立即触发识图并在当前标签页原地落页
    ev(cdp, '''((inp, imgUrl) => {
        if (!inp) return;
        inp.focus();
        inp.value = imgUrl;
        const dt = new DataTransfer();
        dt.setData('text/plain', imgUrl);
        const evt = new ClipboardEvent('paste', {
            clipboardData: dt,
            bubbles: true,
            cancelable: true
        });
        inp.dispatchEvent(evt);
        inp.dispatchEvent(new Event('input', {bubbles: true}));
        inp.dispatchEvent(new Event('change', {bubbles: true}));
    })(%s, %s)''' % (inp_js, json.dumps(image)))

    # 等待落页确认 (粘贴事件通常在 1.5~3 秒内确定性完成原地落页；设 25 秒充分容忍高延迟代理)
    deadline = time.time() + 25
    landed = False
    compensated = False
    err_count = 0
    while time.time() < deadline:
        if ev(cdp, LAND):
            landed = True
            break

        # 检查是否在新标签页中落页
        if ch:
            try:
                cur_id = cdp.path.split('/')[-1].split('?')[0] if hasattr(cdp, 'path') else ''
                for t_item in ch.tabs():
                    tid = t_item.get('id')
                    if tid and tid != cur_id:
                        tab_cdp = ch.attach(t_item)
                        if ev(tab_cdp, LAND):
                            landed = True
                            ch.close_tab(tid)
                            break
            except Exception:
                pass
            if landed:
                break

        # 检查是否有识图错误提示 (必须处于可见状态，排除隐藏模板节点干扰)
        err = ev(cdp, '''(() => {
            const els = Array.from(document.querySelectorAll('#url-err-msg, #bcid-err-msg, .errcntr .errmsg, #url-err, #bcid-err'));
            const vis = els.find(e => e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0 && e.innerText.trim());
            return vis ? vis.innerText.trim() : null;
        })()''')
        if err:
            err_count += 1
            if err_count >= 2:
                raise StepError(f'视觉图片提交被必应拒绝: {err}')
        else:
            err_count = 0

        # 若等待超过 4 秒仍未落页，再次聚焦输入框并补发 keypress(Enter) 与按键唤醒
        if not compensated and (deadline - time.time() < 21):
            compensated = True
            ev(cdp, '''((inp) => {
                if (inp) {
                    inp.focus();
                    const kp = new KeyboardEvent('keypress', {
                        key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true
                    });
                    inp.dispatchEvent(kp);
                }
            })(%s)''' % inp_js)
            for t in ('keyDown', 'keyUp'):
                try:
                    cdp.call('Input.dispatchKeyEvent', {
                        'type': t, 'key': 'Enter', 'code': 'Enter',
                        'windowsVirtualKeyCode': 13, 'nativeVirtualKeyCode': 13,
                        'text': '\r' if t == 'keyDown' else '',
                        'unmodifiedText': '\r' if t == 'keyDown' else ''
                    })
                except Exception:
                    pass

        time.sleep(0.5)

    if not landed:
        raise StepError('视觉提交完成但落页未确认(LAND超时)')

    base = 18.0
    try:
        from msr import settings as _S
        base = float(_S.get('visual.landing_wait_seconds', 18) or 18)
    except Exception:
        pass
    dwell = base + random.uniform(0.0, 7.0)
    # 2026-11-13: 落页 URL 与登录态必须留证 —— 某SG号 当天 9 轮全部 progress=0,
    # 事后无法判定"落页是不是真视觉搜索页"、"会话是否已登录", 只能靠再跑一次观察。
    try:
        _u = ev(cdp, '(() => location.href)()')
        _li = ev(cdp, r"(() => !!document.querySelector('#id_l, #id_n, a#id_s, [aria-label*=\"Account\" i]'))()")
        log('视觉落页 URL: %s | 页面登录态元素=%s' % (str(_u)[:160], _li))
    except Exception as _ex:
        log('视觉落页留证失败(忽略): %s' % str(_ex)[:50])
    log('视觉搜索已成功落页，在结果页停留 %.1f 秒归因...' % dwell)
    time.sleep(dwell)
    return 'success'


def today_wallpaper():
    """当日 Bing 壁纸直链(单张)。

    2026-10-xx 口径: 视觉搜索用**当天壁纸**即可稳定落页归因, 不需要 8 张的"图片池"轮换 ——
    多地区抓取 + used 记录只是增加请求和状态文件, 没有换来成功率。这里退化成取 idx=0 一张。
    """
    for mkt in ('en-US', 'zh-CN'):
        try:
            url = ('https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=1&mkt=%s' % mkt)
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode() or '{}')
            imgs = d.get('images') or []
            if not imgs:
                continue
            u = imgs[0].get('url') or imgs[0].get('urlbase') or ''
            if u.startswith('/'):
                u = 'https://www.bing.com' + u
            if u:
                return u
        except Exception:
            continue
    return None


IMGSRCH_TPL = ('https://www.bing.com/images/search?iss=sbi&FORM=SBIIDP&sbisrc=UrlPaste'
               '&q=imgurl:%s')


def submit_image_direct(cdp, image, log=print, dwell=20.0):
    """直接打开「粘贴图片链接」得到的图片搜索 URL，让服务端识别并计入视觉打卡。

    2026-11-18 实测：走首页镜头弹层→粘贴 URL 的旧流程，SG 三个号连续两天 6 轮全 0；
    而直接访问本函数的 URL，一次即 `progress=1 complete=True`（同一张图、同一个号）。
    旧流程落页到的是文本搜索结果页（q=<图片描述>&FORM=SBIHMP），服务端不再据此计入；
    本 URL 是真正的图片搜索（/images/search?iss=sbi&...q=imgurl:<直链>）。
    """
    target = IMGSRCH_TPL % urllib.parse.quote(image, safe='')
    log('视觉直接图片搜索: %s' % target[:90])
    navigate(cdp, target)
    # 等图片搜索结果页水合（标题/大图区就绪即可，归因靠 dwell）
    deadline = time.time() + 25
    while time.time() < deadline:
        ready = ev(cdp, """(() => document.readyState === 'complete'
            && !!(document.querySelector('#detailPage img, .mimg, #mmComponent_images_2 img, [class*=sbi]')))()""")
        if ready:
            break
        time.sleep(1.0)
    time.sleep(dwell)


def run(name, cdp, pr, log=print, used_path=None, max_attempts=None, ch=None):
    """视觉搜索任务执行。优先激活连续打卡，识图落页归因。"""
    # P1: 尝试轮数来自 visual.submit_rounds
    if max_attempts is None:
        try:
            from msr import settings as _S
            max_attempts = int(_S.get('visual.submit_rounds', 3) or 3)
        except Exception:
            max_attempts = 3
    max_attempts = max(1, int(max_attempts))

    # 优先走 HTTP(RSC payload) 判定 —— 与 Edge 30 分钟同样的思路: 状态从服务端数据拿,
    # 不靠 DOM 扫描。命中时这一步只要 1~2 秒, 且账号已做完/未下发的连浏览器都不用操作。
    try:
        from msr import config as _cfg
        _acc = _cfg.get_account(name) or {}
        _proxy = _acc.get('proxy')
        if _proxy and '://' not in _proxy:
            _proxy = 'http://' + _proxy
    except Exception:
        _proxy = None
    st = read_status_http(name, proxy=_proxy, log=log)
    src = 'HTTP'
    if st is None:
        st = read_status(cdp)          # payload 读不到/缺对象 -> 回退 DOM(页面说话)
        src = 'DOM'
    log('视觉状态(%s): %s' % (src, st))
    if not st.get('found'):
        # 2026-11-11: 区分"账号没下发视觉打卡"与"卡片定位失败"——两者都跳过, 但根因不同,
        # 旧写法一律报"未参与", 会把选择器失效伪装成账号没任务。
        # 2026-11-13: 这里只可能是 DOM 判定(src=HTTP 时 payload 缺对象已回退 DOM), 单一真值源。
        try:
            has_text = ev(cdp, r"(() => /视觉搜索|visual\s*search|vsstreak/i.test(document.body.innerText || ''))()")
        except Exception:
            has_text = None
        if has_text:
            log('视觉: 页面存在视觉搜索文案但卡片未定位 (选择器待复查, 不用"未参与"掩盖)')
            return '视觉卡定位失败(跳过)'
        return '本账号未下发视觉打卡(跳过)'
    # complete 与 today_done 两个字段一致才认"今日已完成"; 单看 complete 可能与窗口口径混淆
    if st.get('complete') and st.get('today_done') is not False:
        return '今日视觉已完成, 跳过'

    # 仅当处于未激活状态时，在 earn 页面展开抽屉并激活打卡；已激活账号直达打卡入口，避免多余开关标签页
    if st.get('unactivated'):
        entry = get_entry_and_activate(cdp, ch=ch, log=log)
    else:
        entry = ((st.get('detail') or {}).get('entry')
                 or 'https://www.bing.com/?features=vsstreak,vstooltip&form=ML2XES')
    log('视觉任务入口就绪: %s' % entry[:70])

    img = today_wallpaper()
    if not img:
        return '取图失败, 跳过'
    log('视觉提交图: %s' % img[-60:])

    for i in range(max_attempts):
        try:
            # 主路径: 直接图片搜索 URL（2026-11-18 起旧"镜头+粘贴"流程在 SG 号上不再计分，
            # 直接 URL 一次计分；旧流程保留为直连失败的兜底）
            try:
                submit_image_direct(cdp, img, log=log)
            except Exception as ex_direct:
                log('视觉直接搜索异常: %s, 回退镜头粘贴流程' % str(ex_direct)[:70])
                submit_image(cdp, img, entry, log=log, ch=ch)

            # 回读状态确认 (多次回读以吸收网络端延迟与服务端统计延迟)
            st2 = None
            for r_read in range(3):
                time.sleep(random.uniform(3.0, 5.0))
                st2 = read_status_http(name, proxy=_proxy, log=log)     # 先 HTTP, 秒级
                if st2 is None:
                    st2 = read_status(cdp)                      # 兜底才动 DOM
                if st2 and st2.get('complete'):
                    break
            log('视觉第%d轮提交后回读: %s' % (i + 1, st2))
            if st2 and st2.get('complete'):
                return '视觉搜索完成(第%d轮)' % (i + 1)

            # 若服务端未立即标记完成且仍有重试机会，继续下一轮
            if i < max_attempts - 1:
                log('视觉第%d轮提交后回读未满额，等待冷却后进入第%d轮重试...' % (i + 1, i + 2))
                time.sleep(random.uniform(15.0, 25.0))
                continue
        except Exception as ex:
            log('视觉第%d轮失败: %s' % (i + 1, str(ex)[:90]))
            if i < max_attempts - 1:
                time.sleep(random.uniform(15.0, 25.0))

    return '未完成:视觉搜索(尝试%d轮)' % max_attempts
