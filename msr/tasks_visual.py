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
import time
import urllib.request

from msr.browser import ev, navigate, StepError, human_click

EARN = 'https://rewards.bing.com/earn'
DASH = 'https://rewards.bing.com/'
HOME = 'https://www.bing.com/?features=vsstreak,vstooltip&form=ML2XES'
ICON = 'img[src*="/search_visual.svg"]'

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

PANEL_JS = '''(() => {
  const els = [...document.querySelectorAll('*')];
  for (const el of els) {
    let own = "";
    for (const n of el.childNodes) { if (n.nodeType === 3) own += n.textContent; }
    own = own.replace(/\\s+/g, " ").trim();
    const m = own.match(/^(?:活动|Activities|Activity|Tasks|Task):\\s*(\\d+)\\/(\\d+)$/i);
    if (!m) continue;
    let box = el.parentElement, hops = 0;
    while (box && hops < 8) {
      const ctx = (box.textContent || "").replace(/\\s+/g, " ");
      if (/视觉搜索|visual\\s*search/i.test(ctx)) {
        return {progress: Number(m[1]), max: Number(m[2]),
                complete: Number(m[1]) >= Number(m[2]) && Number(m[2]) > 0,
                found: true, unactivated: false, source: 'panel'};
      }
      box = box.parentElement; hops++;
    }
  }
  return null;
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
    if (!u.hostname.endsWith('bing.com') || u.pathname !== '/search') return false;
    return u.searchParams.has('bcid') || (u.searchParams.get('FORM') || '').startsWith('SBI') || (u.searchParams.get('features') || '').includes('vsstreak');
  } catch(e) {
    return false;
  }
})()'''


def wait_js(cdp, expr, seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            v = ev(cdp, expr)
            if v:
                return v
        except StepError:
            pass
        time.sleep(0.5)
    raise StepError('等待超时(%ss): %s' % (seconds, expr[:50]))


def read_status(cdp, refresh=True):
    """读视觉进度。优先检测 earn 页面，若无再回退 dashboard。"""
    if refresh:
        navigate(cdp, EARN)
        time.sleep(random.uniform(3.0, 5.0))

    # 先查 earn 页面卡片
    for _ in range(2):
        try:
            st = ev(cdp, STATUS_CARD_JS)
            if st and st.get('found'):
                return st
        except Exception:
            pass
        time.sleep(1.0)

    # 尝试 panel 结构
    try:
        st_p = ev(cdp, PANEL_JS)
        if st_p and st_p.get('found'):
            return st_p
    except Exception:
        pass

    # 兜底访问 DASH
    if refresh:
        try:
            navigate(cdp, DASH)
            time.sleep(random.uniform(3.0, 4.5))
            st = ev(cdp, STATUS_CARD_JS) or ev(cdp, PANEL_JS)
            if st and st.get('found'):
                return st
        except Exception:
            pass

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


def submit_image(cdp, image, entry, log=print):
    """提交图片 URL 并等落页。"""
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

    # 真人点击输入框 + 输入图片 URL
    try:
        human_click(cdp, inp_js, label='图片URL框', close_new=False)
    except Exception:
        pass

    ev(cdp, '''((inp, img) => {
        if (inp) {
            inp.focus();
            inp.value = img;
            inp.dispatchEvent(new Event('input', {bubbles: true}));
            inp.dispatchEvent(new Event('change', {bubbles: true}));
        }
    })(%s, %s)''' % (inp_js, json.dumps(image)))
    time.sleep(0.8)

    # 派发 Enter 键并触发表单提交
    for t in ('keyDown', 'keyUp'):
        cdp.call('Input.dispatchKeyEvent', {
            'type': t, 'key': 'Enter', 'code': 'Enter',
            'windowsVirtualKeyCode': 13, 'nativeVirtualKeyCode': 13,
            'text': '\r' if t == 'keyDown' else '',
            'unmodifiedText': '\r' if t == 'keyDown' else ''
        })

    # 触发可能的提交按钮 (#sb_sbi_gh 或 submit)
    ev(cdp, '''(() => {
      const go = document.querySelector('#sb_sbi_gh, #sb_form_go, button.sbi_submit');
      if (go && go.getBoundingClientRect().width > 0) go.click();
    })()''')

    # 等待落页确认 (放宽至 35 秒以容忍网络高延迟代理)
    deadline = time.time() + 35
    landed = False
    while time.time() < deadline:
        if ev(cdp, LAND):
            landed = True
            break
        time.sleep(1.0)

    if not landed:
        raise StepError('视觉提交完成但落页未确认(LAND超时)')

    dwell = random.uniform(18.0, 25.0)
    log('视觉搜索已成功落页，在结果页停留 %.1f 秒归因...' % dwell)
    time.sleep(dwell)
    return 'success'


def image_pool(n=8):
    """Bing 每日壁纸直链池。"""
    url = 'https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=%d&mkt=en-US' % n
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode() or '{}')
    out = []
    for img in d.get('images', []):
        u = img.get('url') or img.get('urlbase') or ''
        if u.startswith('/'):
            u = 'https://www.bing.com' + u
        if u:
            out.append(u)
    return out


def _load_used(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f).get('used', [])
    except Exception:
        return []


def _mark_used(path, url):
    if not path:
        return
    used = _load_used(path)
    if url not in used:
        used.append(url)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'used': used[-40:]}, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def run(name, cdp, pr, log=print, used_path=None, max_attempts=3, ch=None):
    """视觉搜索任务执行。优先激活连续打卡，识图落页归因。"""
    st = read_status(cdp)
    log('视觉状态: %s' % st)
    if not st.get('found'):
        return '本账号未参与视觉打卡(跳过)'
    if st.get('complete'):
        return '今日视觉已完成, 跳过'

    # 仅当处于未激活状态时，在 earn 页面展开抽屉并激活打卡；已激活账号直达打卡入口，避免多余开关标签页
    if st.get('unactivated'):
        entry = get_entry_and_activate(cdp, ch=ch, log=log)
    else:
        entry = 'https://www.bing.com/?features=vsstreak,vstooltip&form=ML2XES'
    log('视觉任务入口就绪: %s' % entry[:70])

    pool = image_pool(8)
    if not pool:
        return '取图失败, 跳过'
    used = _load_used(used_path) if used_path else []
    fresh = [u for u in pool if u not in used]
    cand = fresh or pool

    for i in range(max_attempts):
        img = cand[i % len(cand)]
        try:
            submit_image(cdp, img, entry, log=log)
            _mark_used(used_path, img)

            # 回读状态确认 (多次回读以吸收网络端延迟与服务端统计延迟)
            st2 = None
            for r_read in range(3):
                time.sleep(random.uniform(3.0, 5.0))
                st2 = read_status(cdp)
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
