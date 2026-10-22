import os
import random
import time


def acquire_runner_lock(config):
    """跨进程文件锁，防止定时任务与手动触发并发冲突。"""
    lock_path = os.path.join(config.HOME, 'runner.lock')
    try:
        if os.path.isfile(lock_path):
            with open(lock_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            if content.isdigit():
                pid = int(content)
                try:
                    os.kill(pid, 0)
                    return None
                except OSError:
                    pass
        with open(lock_path, 'w', encoding='utf-8') as f:
            f.write(str(os.getpid()))
        return lock_path
    except Exception:
        return None


def release_runner_lock(lock_path):
    if lock_path and os.path.isfile(lock_path):
        try:
            os.remove(lock_path)
        except Exception:
            pass


def cmd_run(args):
    from msr import config, planner, probe, api, executor, notify, settings

    st_cfg = settings.load_settings()
    sch_cfg = st_cfg.get('schedule', {})
    if getattr(args, 'fast', False):
        sch_cfg['fast_mode'] = True

    all_accs = config.accounts()
    if not all_accs:
        print('无账号。先 msr login <name>')
        return 1

    if args.name and args.name != '--all' and not getattr(args, 'all', False):
        target_acc = next((a for a in all_accs if a['name'] == args.name), None)
        if not target_acc:
            print('账号不存在: %s' % args.name)
            return 1
        if not target_acc.get('enabled', True) and not getattr(args, 'force', False):
            print(f'账号 [{args.name}] 当前处于停用状态。如需手动执行，请先在控制台启用该账号。')
            return 0
        accs = [target_acc]
    else:
        accs = [a for a in all_accs if a.get('enabled', True)]
        if not accs:
            print('当前所有账号均处于停用状态，跳过本次批量执行。')
            return 0

    if not getattr(args, 'dry_run', False):
        lock = acquire_runner_lock(config)
        if not lock:
            print('已有自动化任务正在运行中，跳过本次并发执行以保护 Profile 与系统资源。')
            return 0
    else:
        lock = None

    try:
        rc = 0
        w_min = 10 if sch_cfg.get('fast_mode') else int(sch_cfg.get('inter_account_wait_min', 15)) * 60
        w_max = 20 if sch_cfg.get('fast_mode') else int(sch_cfg.get('inter_account_wait_max', 40)) * 60

        for i, a in enumerate(accs):
            name = a['name']
            if i > 0:
                wait = random.uniform(w_min, w_max)
                config.log(name, '账号间随机等待 %.1f 分钟' % (wait / 60))
                time.sleep(wait)
            try:
                run_one(name, args, config, planner, probe, api, executor, notify, settings)
            except Exception as ex:
                err_msg = '运行失败: %s' % str(ex)[:150]
                config.log(name, '!!! %s' % err_msg)
                notify.notify(f'Rewards 异常 [{name}]', err_msg, level='error')
                rc = 1
        return rc
    finally:
        if lock:
            release_runner_lock(lock)


def run_one(name, args, config, planner, probe, api, executor, notify=None, settings=None):
    from msr import auth
    p = config.profile(name)
    tok = config.load_json(config.pfile(name, 'token.json')) or {}
    rg = config.load_json(config.pfile(name, 'region.json')) or {}
    ck_path = config.pfile(name, 'cookie.json')
    has_cookie = os.path.isfile(ck_path) and bool(config.load_json(ck_path))

    acc_cfg = config.get_account(name) or {}
    proxy = acc_cfg.get('proxy')
    cdp_port = acc_cfg.get('cdp_port')

    # 1. 自动凭据自愈：若无 Token 但有 Cookie，自动尝试静默换取并固化
    if not tok.get('refresh_token') and has_cookie:
        config.log(name, '检测到缺少 OAuth Token，正在利用 Cookie 静默自动生成...')
        new_tok = auth.exchange_token_from_cookies(name, proxy=proxy)
        if new_tok:
            tok = new_tok
            rg = config.load_json(config.pfile(name, 'region.json')) or {}

    has_token = bool(tok.get('refresh_token') or tok.get('access_token'))
    if not has_token and not has_cookie:
        # 尝试检查是否有已保存的微软账号密码进行后台静默重登自愈
        cred = config.get_credential(name)
        if cred and cred.get('username') and cred.get('password'):
            config.log(name, '检测到 Token 与 Cookie 均不可用，正在使用保存的微软凭据尝试后台全自动登录自愈...')
            try:
                res = auth.login_with_credential(name, cred['username'], cred['password'], proxy=proxy,
                                                 log_cb=lambda m: config.log(name, m))
                if res.get('status') == 'success':
                    config.log(name, '全自动登录自愈成功！凭据与 Cookie 已刷新就绪。')
                    tok = config.load_json(config.pfile(name, 'token.json')) or {}
                    rg = config.load_json(config.pfile(name, 'region.json')) or {}
                    has_cookie = os.path.isfile(ck_path) and bool(config.load_json(ck_path))
                    has_token = bool(tok.get('refresh_token') or tok.get('access_token'))
                else:
                    config.log(name, f'后台静默重登未完成: {res.get("error")}')
                    if notify:
                        notify.notify(f'Rewards 凭据需接管 [{name}]', f'账号 {name} 自动重登需用户配合质询: {res.get("error")}\n请前往 Web 控制台接管登录。', level='warning')
            except Exception as ex_relogin:
                config.log(name, f'后台自愈重登异常: {ex_relogin}')

    if not has_token and not has_cookie:
        raise RuntimeError('无可用登录凭据 (未配置账号密码、Cookie 或 Token)，先 msr login %s' % name)

    config.log(name, '=== 运行开始 (proxy=%s cdp_port=%s) ===' % (proxy or '直连', cdp_port or '内置'))

    token = None
    pr = None
    if has_token:
        try:
            token = api.refresh_access(name, p, rg, proxy=proxy)
            pr = probe.probe_account(name, token, proxy=proxy)
            pr['probed_at'] = time.strftime('%F %T')
            config.save_json(config.pfile(name, 'region.json'), pr)
            config.log(name, 'region=%s balance=%s' % (pr['country'], pr.get('balance')))
        except Exception as ex:
            config.log(name, f'Token 刷新/探测失败: {ex}')
            cred = config.get_credential(name)
            if cred and cred.get('username') and cred.get('password'):
                config.log(name, 'Token 刷新异常，正在使用保存的凭据尝试全自动重新登录并自愈...')
                try:
                    res = auth.login_with_credential(name, cred['username'], cred['password'], proxy=proxy,
                                                     log_cb=lambda m: config.log(name, m))
                    if res.get('status') == 'success':
                        tok = config.load_json(config.pfile(name, 'token.json')) or {}
                        rg = config.load_json(config.pfile(name, 'region.json')) or {}
                        has_cookie = os.path.isfile(ck_path) and bool(config.load_json(ck_path))
                        token = api.refresh_access(name, p, rg, proxy=proxy)
                        pr = probe.probe_account(name, token, proxy=proxy)
                        pr['probed_at'] = time.strftime('%F %T')
                        config.save_json(config.pfile(name, 'region.json'), pr)
                        config.log(name, '自愈重登成功！重新获取 region=%s balance=%s' % (pr['country'], pr.get('balance')))
                except Exception as ex2:
                    config.log(name, f'自愈重登尝试未成功: {ex2}')
            if not has_cookie and not token:
                raise

    if not pr:
        # 纯 Cookie 模式：跳过 API 阶段，全额执行浏览器搜索与卡片活动
        config.log(name, '当前以纯 Cookie 模式运行: 跳过 API 阶段，全额执行浏览器搜索与活动打卡')
        pr = rg or {'country': 'SG', 'language': 'zh-hans', 'balance': 0, 'task_names': []}
        plan = [{'phase': ph, 'needs': 'browser'} for ph in planner.BROWSER_ALWAYS]
    else:
        plan = planner.plan(pr, force_phases=args.only)
        if not has_cookie:
            config.log(name, '未检测到浏览器 Cookie，跳过浏览器端任务；仅执行 API 阶段')
            plan = [w for w in plan if w['needs'] == 'api']

    bal_before = pr.get('balance') or 0
    if args.dry_run:
        print('%s 执行计划: %s' % (name, ' -> '.join(w['phase'] for w in plan)))
        return

    st = config.load_json(config.pfile(name, 'state.json'), {}) or {}
    results_map = {}

    api_phases = [w['phase'] for w in plan if w['needs'] == 'api']
    browser_phases = [w['phase'] for w in plan if w['needs'] == 'browser']

    # 1. 优先串行执行极速 API 任务 (appcheckin, appread)
    fast_api = [ph for ph in api_phases if ph not in ('edge30m', 'edge_api', 'edge')]
    for ph in fast_api:
        try:
            r = executor.run_api_phase(name, ph, token, pr, p, config, api, proxy=proxy)
            results_map[ph] = r
            config.log(name, 'API阶段 %s 完成: %s' % (ph, r))
        except Exception as ex:
            results_map[ph] = '失败(%s)' % str(ex)[:60]
            config.log(name, '!!! API阶段 %s 异常: %s' % (ph, str(ex)[:120]))

    # 2. 长耗时 Edge 30分钟浏览心跳任务 (若与浏览器任务共存则后台并行，节省 ~30 分钟)
    has_edge = any(ph in ('edge30m', 'edge_api', 'edge') for ph in api_phases)
    edge_ph = next((ph for ph in api_phases if ph in ('edge30m', 'edge_api', 'edge')), None)
    edge_thread = None

    if has_edge:
        def edge_worker():
            try:
                config.log(name, '>>> [后台并行] 启动 Edge 30分钟浏览打卡心跳...')
                r = executor.run_api_phase(name, edge_ph, token, pr, p, config, api, proxy=proxy)
                results_map[edge_ph] = r
                config.log(name, '<<< [后台并行] Edge 30分钟打卡完成: %s' % r)
            except Exception as ex:
                results_map[edge_ph] = '失败(%s)' % str(ex)[:60]
                config.log(name, '!!! [后台并行] Edge 30分钟打卡异常: %s' % str(ex)[:120])

        if browser_phases:
            import threading
            edge_thread = threading.Thread(target=edge_worker, name=f'edge30m-{name}')
            edge_thread.start()
        else:
            edge_worker()

    # 3. 前台启动单个浏览器会话，真人流转依次执行该账号的所有浏览器阶段
    if browser_phases:
        b_results = executor.run_browser_session(name, browser_phases, token, pr, p, config,
                                                 proxy=proxy, cdp_port=cdp_port)
        results_map.update(b_results)

    # 4. 若有后台 Edge 打卡心跳线程，等待其 30 分钟周期结束
    if edge_thread is not None:
        config.log(name, '前台浏览器任务已全部就绪，等待后台 Edge 30分钟打卡结束...')
        edge_thread.join()

    # 5. 按照原始 plan 顺序组装结果
    results = ['%s:%s' % (w['phase'], results_map.get(w['phase'], '未执行')) for w in plan]

    # 自动核查 + 自动修复
    bad = []
    for r in results:
        ph, _, val = r.partition(':')
        if val.startswith('失败') or '未complete' in val or '未出现' in val or '未发现每日活动' in val:
            bad.append(ph)
    if bad and not args.dry_run:
        from msr import settings as S
        st_runner = S.load_settings()
        retry_mins = int(st_runner.get('schedule', {}).get('retry_wait_minutes', 4))
        fast_mode = bool(st_runner.get('schedule', {}).get('fast_mode', False))
        wait_s = 5 if fast_mode else retry_mins * 60
        config.log(name, '自动修复: 等待 %.1f 分钟后重跑 %s' % (wait_s / 60, bad))
        time.sleep(wait_s)
        bad_api = [ph for ph in bad if next((t['needs'] for t in planner.TASK_TABLE.values() if t['phase'] == ph), 'api') == 'api']
        bad_browser = [ph for ph in bad if ph not in bad_api]
        for ph in bad_api:
            try:
                r = executor.run_api_phase(name, ph, token, pr, p, config, api, proxy=proxy)
                results.append('%s:修复(%s)' % (ph, r))
                config.log(name, '自动修复 %s: %s' % (ph, r))
            except Exception as ex:
                results.append('%s:修复失败(%s)' % (ph, str(ex)[:50]))
                config.log(name, '自动修复 %s 仍失败: %s' % (ph, str(ex)[:100]))
        if bad_browser:
            b_fixes = executor.run_browser_session(name, bad_browser, token, pr, p, config, proxy=proxy, cdp_port=cdp_port)
            for ph, r in b_fixes.items():
                results.append('%s:修复(%s)' % (ph, r))
                config.log(name, '自动修复 %s: %s' % (ph, r))

    # 获取运行后最新余额
    bal_after = bal_before
    if token:
        try:
            rg_me = {'country': pr['country'], 'language': pr['language']}
            me_resp = api.get_me(name, token, rg_me, proxy=proxy)
            bal_after = me_resp.get('balance') or bal_before
        except Exception:
            pass

    gained = max(0, (bal_after or 0) - (bal_before or 0))

    st['last_run'] = time.strftime('%F %T')
    st['last_results'] = results
    st['balance'] = bal_after
    config.save_json(config.pfile(name, 'state.json'), st)

    try:
        from msr import history
        history.record_daily_point(name, start_bal=bal_before, end_bal=bal_after, results=results)
    except Exception:
        pass

    config.log(name, '=== 运行结束 (余额: %s, 今日增量: +%d): %s ===' % (bal_after, gained, ' | '.join(results)))

    if notify:
        summary_title = f'Rewards 报告 [{name}] (+{gained}分)'
        summary_lines = [
            f'### 账号: {name} (区域: {pr.get("country")}, 代理: {proxy or "直连"})',
            f'- **当前积分**: {bal_after} (今日变动: +{gained} 分)',
            '- **执行任务清单**:'
        ]
        for r in results:
            summary_lines.append(f'  - `{r}`')
        summary_lines.append(f'- **结束时间**: {time.strftime("%F %T")}')
        notify.notify(summary_title, '\n'.join(summary_lines), level='summary')
