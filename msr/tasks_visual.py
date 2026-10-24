"""视觉搜索任务（移植自现网 bing_visual_task.py）。

历史教训（全部实测，勿踩）：
- 视觉入口卡片可能不渲染（dashboard A/B），卡片不存在时直接走 Bing 首页镜头；
- 镜头按钮新版是可见的 `div.b_icon[aria-label="使用图像搜索"]`（24x24），
  旧的 `#sbi_b` / `#sb_sbi` 已变成 width 0 / display:none —— 必须按**可见性**查找；
- 提交方式：真人点 URL 框 + 真实键入（insertText）+ Enter 是唯一在凌晨 cron
  环境下验证有效的；合成 ClipboardEvent(paste) 仅作降级（10-10~10-14 合成事件
  在凌晨连续失败）；
- 落页判据 `/search?q=...&bcid=...`；落页后停留 15s（网络波动时上报未完成）；
- 计分裁决：面板“视觉搜索 活动: x/1”，reportActivity 200 不等于计分。
"""
import json
import time
import urllib.request

from msr.browser import ev, navigate, StepError, human_click

DASH = 'https://rewards.bing.com/'
HOME = 'https://www.bing.com/'
ICON = 'img[src*="/search_visual.svg"]'

STATUS_JS = '''(() => {
 const icon = document.querySelector('img[src*="/search_visual.svg"]');
 const card = icon && icon.closest('button');
 const bar = card && card.querySelector('[role="progressbar"]');
 if (!bar) return null;
 const raw = bar.getAttribute('aria-valuenow'), max = bar.getAttribute('aria-valuemax');
 if (raw === null || max === null || !Number.isFinite(Number(raw)) || !(Number(max)>0)) return null;
 return {progress:Number(raw), max:Number(max), complete:Number(raw)>=Number(max)};
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
               complete: Number(m[1]) >= Number(m[2]) && Number(m[2]) > 0};
     }
     box = box.parentElement; hops++;
   }
 }
 return null;
})()'''

LENS_PICK = '''(() => {
  const vis = x => x && x.getBoundingClientRect().width > 0 && x.getBoundingClientRect().height > 0;

  // 1. 结构化入口 (Bing 首页镜头与搜索页镜头)
  const structural = [
    document.querySelector('#sb_cam_island'),
    document.querySelector('#sb_cam_island .b_icon'),
    document.querySelector('[data-island="cam"]'),
    document.querySelector('#sb_sbi'),
    document.querySelector('#sbi_b'),
    document.querySelector('.hp_island_host.camera'),
    document.querySelector('#sb_form .camera'),
    document.querySelector('.sbi_hpicn')
  ];
  for (const el of structural) {
    if (vis(el)) return el;
  }

  // 2. 多语言语义属性入口 (适配新加坡/海外英文环境及多语言 Bing)
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
    document.querySelector('input[aria-label*="URL" i]'),
    document.querySelector('[role="dialog"] input[type=text]'),
    document.querySelector('#sbi_b input')
  ];
  for (const i of inps) {
    if (vis(i)) return true;
  }
  // 若弹层有粘贴按钮需先触发展开输入框
  const pasteBtn = document.querySelector('[role="dialog"] button, #sbi_b button, .sbi_paste');
  if (pasteBtn && vis(pasteBtn)) {
    try { pasteBtn.click(); } catch(e){}
  }
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
    last = None
    while time.time() < deadline:
        try:
            v = ev(cdp, expr)
            if v:
                return v
            last = v
        except StepError:
            pass
        time.sleep(0.5)
    raise StepError('等待超时(%ss): %s' % (seconds, expr[:50]))


def read_status(cdp, refresh=True):
    """读视觉进度。三层：旧卡片 progressbar -> 连续打卡面板 -> 未检测到。

    found=False 表示当前页面上找不到任何视觉任务痕迹（按用户策略：没有就不执行）。
    """
    if refresh:
        navigate(cdp, DASH)
        time.sleep(6)
    for js, src in ((STATUS_JS, 'card'), (PANEL_JS, 'panel')):
        try:
            r = wait_js(cdp, js, 10)
            if r:
                r['found'] = True
                r['source'] = src
                return r
        except StepError:
            continue
    return {'progress': 0, 'max': 1, 'complete': False, 'found': False,
            'source': 'unavailable'}


def get_entry(cdp):
    """视觉入口：卡片在时点开取 vsstreak 链接页，否则回首页镜头（实测等效）。"""
    if not ev(cdp, '(() => !!document.querySelector(%s))()' % json.dumps(ICON)):
        return HOME
    try:
        human_click(cdp, 'document.querySelector(%s).closest("button")' % json.dumps(ICON),
                    label='视觉任务详情', close_new=False)
        expr = ('''(() => {const as=[...document.querySelectorAll('[role="dialog"] a[href]')];
         const a=as.find(a=>{try {const u=new URL(a.href);return u.hostname==='www.bing.com' &&
            (u.searchParams.get('features')||'').split(',').includes('vsstreak');}catch(e){return false;}});
         return a && a.href;})()''')
        href = wait_js(cdp, expr, 5)
        if href:
            return href
    except Exception:
        pass
    return HOME


def submit_image(cdp, image, entry):
    """提交图片 URL 并等落页。方式1=点输入框+赋值+Enter，方式2=合成 paste（降级）。"""
    navigate(cdp, entry)
    wait_js(cdp, '!!(%s)' % LENS_PICK, 25)
    opened = False
    for _ in range(3):
        try:
            human_click(cdp, LENS_PICK, label='视觉镜头', close_new=False)
        except Exception:
            pass
        try:
            wait_js(cdp, BOX_READY, 5)
            opened = True
            break
        except StepError:
            pass
        # 降级：直接触发镜头元素及父级 click() 事件（应对部分桌面版外层 div 拦截）
        try:
            ev(cdp, '(() => { const el = (%s); if (el) { el.click(); if (el.parentElement) el.parentElement.click(); } })()' % LENS_PICK)
            wait_js(cdp, BOX_READY, 6)
            opened = True
            break
        except Exception:
            time.sleep(1.5)

    if not opened:
        # 降级通道：直接导航至 Bing 官方视觉搜索专用入口
        sbi_url = 'https://www.bing.com/images/search?view=detailv2&iss=sbi&FORM=SBIHPC'
        navigate(cdp, sbi_url)
        time.sleep(3)
        try:
            wait_js(cdp, BOX_READY, 8)
            opened = True
        except StepError:
            pass

    if not opened:
        raise StepError('视觉镜头/弹层 3 轮未就绪')
    time.sleep(1.5)

    inp_js = '''(() => {
      const sels = ['#sb_imgpst', '#sb_sbi_ipt', 'input.imgpst', '#sbi_input', 'input[placeholder*="URL" i]', 'input[aria-label*="URL" i]', '[role="dialog"] input[type=text]', '#sbi_b input'];
      for (const s of sels) {
        const el = document.querySelector(s);
        if (el && el.getBoundingClientRect().width > 0) return el;
      }
      return document.querySelector('#sb_imgpst') || document.querySelector('input');
    })()'''

    # 方式1：真人点 URL 框 + 填入图片地址 + Enter（实战验证最稳定）
    try:
        try:
            human_click(cdp, inp_js, label='图片URL框', close_new=False)
        except Exception:
            pass

        # 填入并触发 input/change 事件
        ev(cdp, '''((inp, img) => {
            if (inp) {
                inp.focus();
                inp.value = img;
                inp.dispatchEvent(new Event('input', {bubbles: true}));
                inp.dispatchEvent(new Event('change', {bubbles: true}));
            }
        })(%s, %s)''' % (inp_js, json.dumps(image)))

        time.sleep(0.8)

        # 派发 Enter 键
        for t in ('keyDown', 'keyUp'):
            cdp.call('Input.dispatchKeyEvent', {
                'type': t, 'key': 'Enter', 'code': 'Enter',
                'windowsVirtualKeyCode': 13, 'nativeVirtualKeyCode': 13,
                'text': '\r' if t == 'keyDown' else '',
                'unmodifiedText': '\r' if t == 'keyDown' else ''
            })

        try:
            wait_js(cdp, LAND, 25)
            time.sleep(15)
            return 'typed'
        except StepError:
            pass
    except Exception:
        pass

    # 方式2：合成 paste（降级）
    js = ('(() => {const el=%s;'
          'if(!el) return false; const dt=new DataTransfer();'
          'dt.setData("text/plain", %s);'
          'el.dispatchEvent(new ClipboardEvent("paste",{clipboardData:dt,bubbles:true,cancelable:true}));'
          'return true;})()' % (inp_js, json.dumps(image)))
    if ev(cdp, js):
        time.sleep(0.5)
        for t in ('keyDown', 'keyUp'):
            cdp.call('Input.dispatchKeyEvent', {
                'type': t, 'key': 'Enter', 'code': 'Enter',
                'windowsVirtualKeyCode': 13, 'nativeVirtualKeyCode': 13,
                'text': '\r' if t == 'keyDown' else '',
                'unmodifiedText': '\r' if t == 'keyDown' else ''
            })
        try:
            wait_js(cdp, LAND, 25)
            time.sleep(15)
            return 'paste'
        except StepError:
            pass

    raise StepError('视觉提交完成但落页未确认(LAND超时)')


def image_pool(n=8):
    """Bing 每日壁纸（8 张轮换，直链是 kblob 白名单；外部图会被 500 拒）。"""
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


def run(name, cdp, pr, log=print, used_path=None, max_attempts=2):
    """主流程。有任务才执行，没有就跳过（用户策略）。"""
    st = read_status(cdp)
    log('视觉状态: %s' % st)
    if not st.get('found'):
        return '今日无视觉任务(面板未检测到), 跳过'
    if st.get('complete'):
        return '今日视觉已完成, 跳过'
    pool = image_pool(8)
    if not pool:
        return '取图失败, 跳过'
    used = _load_used(used_path) if used_path else []
    fresh = [u for u in pool if u not in used]
    cand = fresh or pool
    entry = get_entry(cdp)
    for i in range(max_attempts):
        img = cand[i % len(cand)]
        try:
            how = submit_image(cdp, img, entry)
            time.sleep(10)
            st2 = read_status(cdp)
            log('视觉第%d轮(%s): %s' % (i + 1, how, st2))
            if st2.get('complete'):
                _mark_used(used_path, img)
                return '视觉搜索完成(第%d轮, %s)' % (i + 1, how)
        except Exception as ex:
            log('视觉第%d轮失败: %s' % (i + 1, str(ex)[:90]))
        if i < max_attempts - 1:
            time.sleep(30)
    return '视觉搜索未完成(尝试%d轮)' % max_attempts
