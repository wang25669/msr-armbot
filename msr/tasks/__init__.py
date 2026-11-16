"""P3 · 只读发现层（dry-run）：把"今天该做什么"变成一份 Task 清单。

用法：
    python3 -m msr discover --all            # 6 个账号顺序跑，约 4–6 分钟
    python3 -m msr discover <账号> --json  # 单账号，输出 JSON
    python3 -m msr discover --all --no-dom   # 只看 API 侧（秒级，不起浏览器）

约束：零点击、零提交、零进度变化；每个账号只短暂起一次 headless Chromium。
"""
import json
import os
import time

from msr.tasks import discovery, normalize as norm
from msr.tasks.model import make_task, is_done, task_line

__all__ = ['discover_account', 'format_report', 'cmd_discover']


def _static_tasks():
    """无法从服务端"发现"的盲投任务（App 签到 / App 阅读）。

    API 没有任务清单，只能尝试提交直到额度满（现有实现的真实语义）。
    列出来是为了让 dry-run 的清单完整，不隐藏这部分工作。
    """
    return [
        make_task('api:appcheckin', 'api_attempt', 'static', label='App 签到',
                  verify={'api': 'balance_delta'}, needs='api', discovered='static:blind',
                  extra={'blind': True}),
        make_task('api:appread', 'api_attempt', 'static', label='App 阅读',
                  verify={'api': 'balance_delta'}, needs='api', discovered='static:blind',
                  extra={'blind': True}),
    ]


def discover_account(name, config, api, log=None, with_dom=True, extra_wait=0):
    """只读发现单个账号。返回 dict（不抛异常，错误进 error 字段）。"""
    log = log or (lambda m: None)
    out = {'account': name, 'tasks': [], 'unknowns': [], 'notes': [], 'error': None}
    acc = config.get_account(name) or {}
    proxy = acc.get('proxy')
    if proxy and '://' not in proxy:
        proxy = 'http://' + proxy
    out['proxy'] = proxy or '直连'

    tok = config.load_json(config.pfile(name, 'token.json'), {}) or {}
    rg = config.load_json(config.pfile(name, 'region.json'), {}) or {}
    region = {
        'country': (rg.get('rewards_market') or rg.get('country') or 'us').lower(),
        'language': rg.get('language') or 'en',
    }
    token = tok.get('access_token')
    if not token:
        out['error'] = '无 access_token（需先登录该账号）'
        return out

    me = None
    try:
        me = api.get_me(name, token, region, proxy=proxy)
    except Exception as ex1:
        # access 过期：刷新一次（只维护认证，不触任务）
        try:
            token = api.refresh_access(name, config.profile(name), rg, proxy=proxy)
            me = api.get_me(name, token, region, proxy=proxy)
            out['notes'].append('access_token 已过期，使用 refresh_token 续期后读取')
        except Exception as ex2:
            out['error'] = '读取 /dapi/me 失败: %s / %s' % (str(ex1)[:80], str(ex2)[:80])
            return out

    out['balance'] = (me or {}).get('balance')
    prof = (me or {}).get('profile') or {}
    out['ruid'] = (prof.get('ruid') or '')[:12]
    api_raw_all = discovery.discover_api(me, log=log)
    signals = [r for r in api_raw_all if r.get('signal')]
    api_raw = [r for r in api_raw_all if not r.get('signal')]
    out['signals'] = signals

    dom_raw, meta = (None, {})
    if with_dom:
        t0 = time.time()
        dom_raw, meta = discovery.discover_dom(name, config, proxy=proxy, log=log,
                                               extra_wait=extra_wait)
        meta['elapsed_s'] = round(time.time() - t0, 1)
    out['dom_meta'] = meta

    tasks, unknowns, notes = norm.normalize(api_raw, dom_raw, account=name)
    tasks.extend(_static_tasks())
    out['tasks'] = tasks
    out['unknowns'] = unknowns
    out['notes'].extend(notes)
    if meta.get('error'):
        out['notes'].append('DOM 采集失败: %s' % meta['error'])
    out['summary'] = norm.summarize(tasks, unknowns)
    out['summary']['signals'] = [x.get('label') or x.get('id') for x in signals]
    return out


def format_report(res, verbose=True):
    """人读格式；verbose=False 时只打印一行摘要。"""
    if res.get('error'):
        return '[%s] 失败: %s' % (res['account'], res['error'])
    s = res['summary']
    head = '[%s] 余额=%s 任务=%d (完成 %d/待做 %d) 未知=%d 激活待办=%s' % (
        res['account'], res.get('balance'), s['total'], s['done'], s['todo'],
        s['unknown'],
        ','.join([x['id'] for x in res.get('tasks', []) if x.get('needs_activation')]
                 + [x['id'].replace('activation:', '') for x in res.get('signals', [])]) or '-')
    if not verbose:
        return head
    lines = [head]
    for t in sorted(res['tasks'], key=lambda x: (x['kind'], x['id'])):
        lines.append('  ' + task_line(t))
    for u in res['unknowns']:
        lines.append('  UNKNOWN %s' % json.dumps(u, ensure_ascii=False)[:160])
    for sg in res.get('signals') or []:
        lines.append('  SIGNAL %s (%s) %s' % (sg.get('id'), sg.get('reason'),
                                              (sg.get('label') or '')[:50]))
    for n in res['notes']:
        lines.append('  note: %s' % n)
    return '\n'.join(lines)


def cmd_discover(args, config):
    """CLI 入口：msr discover [name|--all] [--json] [--no-dom] [--save PATH]"""
    from msr import api

    names = []
    if getattr(args, 'all', False) or not args.name:
        names = [a['name'] for a in config.accounts() if a.get('enabled', True)]
    else:
        names = [args.name]

    results = []
    for i, name in enumerate(names):
        t0 = time.time()
        res = discover_account(name, config, api, with_dom=not args.no_dom,
                               extra_wait=0)
        res['elapsed_s'] = round(time.time() - t0, 1)
        results.append(res)
        print(format_report(res, verbose=not args.json))
        print()
        if i < len(names) - 1:
            time.sleep(2)

    if args.json:
        blob = json.dumps(results, ensure_ascii=False, indent=1)
        print(blob)
        if args.save:
            with open(args.save, 'w', encoding='utf-8') as f:
                f.write(blob)
            print('已写入 %s' % args.save)

    # 汇总：未知项是 P3 的核心产出
    total_unknown = sum(len(r['unknowns']) for r in results)
    failed = [r['account'] for r in results if r.get('error')]
    print('汇总: 账号 %d | 任务 %d | 未知 %d | 失败 %s' % (
        len(results), sum(r['summary']['total'] for r in results if r.get('summary')),
        total_unknown, failed or '无'))
    return 0
