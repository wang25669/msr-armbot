"""执行编排: 账号串行, 账号间随机等待, 支持多账号独立代理与区域自适配。"""
import random
import time


def cmd_run(args):
    from msr import config, planner, probe, api, executor

    accs = [a for a in config.accounts() if a.get('enabled', True)]
    if not accs:
        print('无账号。先 msr login <name>')
        return 1
    if args.name and args.name != '--all':
        accs = [a for a in accs if a['name'] == args.name]
        if not accs:
            print('账号不存在: %s' % args.name)
            return 1

    rc = 0
    for i, a in enumerate(accs):
        name = a['name']
        if i > 0:
            wait = random.uniform(15 * 60, 40 * 60)
            config.log(name, '账号间随机等待 %.0f 分钟' % (wait / 60))
            time.sleep(wait)
        try:
            run_one(name, args, config, planner, probe, api, executor)
        except Exception as ex:
            config.log(name, '!!! 运行失败: %s' % str(ex)[:150])
            rc = 1
    return rc


def run_one(name, args, config, planner, probe, api, executor):
    p = config.profile(name)
    tok = config.load_json(config.pfile(name, 'token.json')) or {}
    rg = config.load_json(config.pfile(name, 'region.json'))
    if not tok.get('refresh_token'):
        raise RuntimeError('无 token, 先 msr login %s' % name)

    acc_cfg = config.get_account(name) or {}
    proxy = acc_cfg.get('proxy')
    cdp_port = acc_cfg.get('cdp_port')

    config.log(name, '=== 运行开始 (proxy=%s cdp_port=%s) ===' % (proxy or '直连', cdp_port or '内置'))
    token = api.refresh_access(name, p, rg, proxy=proxy)

    pr = probe.probe_account(name, token, proxy=proxy)
    pr['probed_at'] = time.strftime('%F %T')
    config.save_json(config.pfile(name, 'region.json'), pr)
    config.log(name, 'region=%s balance=%s' % (pr['country'], pr.get('balance')))

    plan = planner.plan(pr, force_phases=args.only)
    if args.dry_run:
        print('%s 执行计划: %s' % (name, ' -> '.join(w['phase'] for w in plan)))
        return

    st = config.load_json(config.pfile(name, 'state.json'), {}) or {}
    results = []
    for w in plan:
        phase, needs = w['phase'], w['needs']
        try:
            if needs == 'api':
                r = executor.run_api_phase(name, phase, token, pr, p, config, api, proxy=proxy)
            else:
                r = executor.run_browser_phase(name, phase, token, pr, p, config, proxy=proxy, cdp_port=cdp_port)
            results.append('%s:%s' % (phase, r))
        except Exception as ex:
            results.append('%s:失败(%s)' % (phase, str(ex)[:60]))
            config.log(name, '!!! %s 异常(其余阶段不受影响): %s' % (phase, str(ex)[:120]))

    # 自动核查 + 自动修复
    bad = []
    for r in results:
        ph, _, val = r.partition(':')
        if val.startswith('失败') or '未complete' in val or '未出现' in val:
            bad.append(ph)
    if bad and not args.dry_run:
        config.log(name, '自动修复: 等待 4 分钟后重跑 %s' % bad)
        time.sleep(240)
        for ph in bad:
            needs = next((t['needs'] for t in planner.TASK_TABLE.values()
                          if t['phase'] == ph), 'api')
            try:
                if needs == 'api':
                    r = executor.run_api_phase(name, ph, token, pr, p, config, api, proxy=proxy)
                else:
                    r = executor.run_browser_phase(name, ph, token, pr, p, config, proxy=proxy, cdp_port=cdp_port)
                results.append('%s:修复(%s)' % (ph, r))
                config.log(name, '自动修复 %s: %s' % (ph, r))
            except Exception as ex:
                results.append('%s:修复失败(%s)' % (ph, str(ex)[:50]))
                config.log(name, '自动修复 %s 仍失败: %s' % (ph, str(ex)[:100]))

    st['last_run'] = time.strftime('%F %T')
    st['last_results'] = results
    st['balance'] = pr.get('balance')
    config.save_json(config.pfile(name, 'state.json'), st)
    config.log(name, '=== 运行结束: %s ===' % ' | '.join(results))
