import os
import random
import re
import time
import fcntl

_LOCK_FD = None


def is_runner_process_alive(pid):
    """精确检查指定 PID 进程是否真正存活且属于 msr runner 任务 (防止容器内 PID 复用误判死锁)。"""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            cmd = f.read().decode('utf-8', errors='ignore')
        return 'msr' in cmd and ('run' in cmd or 'python' in cmd)
    except Exception:
        return False


def acquire_runner_lock(config):
    """跨进程内核级文件排他锁 (fcntl.flock)，进程崩溃或容器重启由操作系统内核自动解封，杜绝死锁。"""
    global _LOCK_FD
    lock_path = os.path.join(config.HOME, 'runner.lock')
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            try:
                with open(lock_path, 'r', encoding='utf-8') as f:
                    c = f.read().strip()
                if c.isdigit() and is_runner_process_alive(int(c)):
                    os.close(fd)
                    return None
                # 若持有该文件的 PID 并非活跃的 msr 任务，视作残留锁尝试抢锁
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except Exception:
                os.close(fd)
                return None

        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode('utf-8'))
        _LOCK_FD = fd
        return lock_path
    except Exception:
        return None


def release_runner_lock(lock_path):
    """释放内核排他锁并清理锁文件。"""
    global _LOCK_FD
    if _LOCK_FD is not None:
        try:
            fcntl.flock(_LOCK_FD, fcntl.LOCK_UN)
            os.close(_LOCK_FD)
        except Exception:
            pass
        _LOCK_FD = None
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
                notify.notify(f'MSR 异常 [{name}]', err_msg, level='error')
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

    # 1. 前置 Token 获取与验证 (OAuth 2.0 REST 凭据体系)
    token = None
    pr = None
    has_token = bool(tok.get('refresh_token') or tok.get('access_token'))

    if has_token:
        try:
            token = api.refresh_access(name, p, rg, proxy=proxy)
            pr = probe.probe_account(name, token, proxy=proxy)
            pr['probed_at'] = time.strftime('%F %T')
            config.save_json(config.pfile(name, 'region.json'), pr)
            config.log(name, 'region=%s balance=%s' % (pr['country'], pr.get('balance')))
        except Exception as ex_tok:
            config.log(name, f'Token 刷新/探测失败: {ex_tok}')
            token = None
            has_token = False

    # 若初始无 Token 或 Token 刷新失败，尝试自愈：优先 Cookie 静默换取，次选账号密码登录
    if not token:
        if has_cookie:
            config.log(name, 'Token 失效，优先尝试使用现有 Cookie 静默换取新 Token...')
            try:
                new_tok = auth.exchange_token_from_cookies(name, proxy=proxy)
                if new_tok and (new_tok.get('refresh_token') or new_tok.get('access_token')):
                    tok = new_tok
                    rg = config.load_json(config.pfile(name, 'region.json'), {}) or {}
                    token = api.refresh_access(name, p, rg, proxy=proxy)
                    pr = probe.probe_account(name, token, proxy=proxy)
                    pr['probed_at'] = time.strftime('%F %T')
                    config.save_json(config.pfile(name, 'region.json'), pr)
                    has_token = True
                    config.log(name, '通过 Cookie 静默换取 Token 自愈成功！重新获取 region=%s balance=%s' % (pr['country'], pr.get('balance')))
                else:
                    config.log(name, '通过 Cookie 静默换取 Token 未能成功 (可能 Cookie 亦已失效或会话过期)')
            except Exception as ex_ck:
                config.log(name, f'通过 Cookie 静默换取 Token 异常: {ex_ck}')

        if not token:
            cred = config.get_credential(name)
            if cred and cred.get('username') and cred.get('password'):
                config.log(name, 'Cookie 亦不可用，正在使用保存的凭据尝试全自动重新登录并自愈...')
                try:
                    res = auth.login_with_credential(name, cred['username'], cred['password'], proxy=proxy,
                                                     log_cb=lambda m: config.log(name, m))
                    if res.get('status') in ('ok', 'success'):
                        tok = config.load_json(config.pfile(name, 'token.json')) or {}
                        rg = config.load_json(config.pfile(name, 'region.json')) or {}
                        has_cookie = os.path.isfile(ck_path) and bool(config.load_json(ck_path))
                        token = api.refresh_access(name, p, rg, proxy=proxy)
                        pr = probe.probe_account(name, token, proxy=proxy)
                        pr['probed_at'] = time.strftime('%F %T')
                        config.save_json(config.pfile(name, 'region.json'), pr)
                        has_token = True
                        config.log(name, '自愈重登成功！重新获取 region=%s balance=%s' % (pr['country'], pr.get('balance')))
                    else:
                        config.log(name, f'后台静默重登未完成: {res.get("error") or res.get("message")}')
                        if notify:
                            notify.notify(f'MSR 凭据需接管 [{name}]', f'账号 {name} 自动重登需用户配合质询: {res.get("error") or res.get("message")}\n请前往 Web 控制台接管登录。', level='warning')
                except Exception as ex2:
                    config.log(name, f'自愈重登尝试未成功: {ex2}')

    if not has_token and not has_cookie:
        raise RuntimeError('无可用登录凭据 (未配置账号密码、Cookie 或 Token)，先 msr login %s' % name)

    config.log(name, '=== 运行开始 (proxy=%s cdp_port=%s) ===' % (proxy or '直连', cdp_port or '内置'))

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

    bsess = None
    degraded_mode = False

    # 浏览器前置测活与自愈门禁 (单进程原地自愈 + 优雅降级)
    if browser_phases:
        try:
            bsess = executor.BrowserSession(name, token, pr or rg, p, config,
                                            proxy=proxy, cdp_port=cdp_port)
            bsess.start()
            is_healthy, bal_or_none, reason = bsess.check_login_state(timeout=25, max_retries=1)
            if is_healthy:
                bsess.is_authenticated = True
                config.log(name, f'浏览器会话确认健康有效 (当前余额: {bal_or_none})')
            elif reason == 'SESSION_EXPIRED':
                config.log(name, '检测到浏览器会话已失效 (Cookie过期或重定向至登录页)，正在执行原地自动登录自愈...')
                cred = config.get_credential(name)
                relogin_ok = False
                if cred and cred.get('username') and cred.get('password'):
                    relogin_ok, relogin_res = bsess.in_situ_relogin(cred['username'], cred['password'], timeout=180)
                    if relogin_ok:
                        config.log(name, '原地自动登录自愈成功！全套凭据已刷新就绪。')
                        try:
                            token = api.refresh_access(name, p, rg, proxy=proxy)
                            pr = probe.probe_account(name, token, proxy=proxy)
                            bsess.token = token
                            bsess.pr = pr
                        except Exception as ex_ref:
                            config.log(name, f'重登后Token刷新容错: {ex_ref}')
                    else:
                        err_detail = (relogin_res.get('error') or relogin_res.get('message') or '未知')[:80]
                        config.log(name, f'原地自愈重登未能完成: {err_detail}')
                        if notify:
                            notify.notify(f'MSR 需接管登录 [{name}]', f'账号 {name} 自动重登需人工质询: {err_detail}\n请前往 Web 控制台接管登录。', level='warning')
                else:
                    config.log(name, '未配置账号密码凭据，无法执行原地全自动重登')
                    if notify:
                        notify.notify(f'MSR 会话过期 [{name}]', f'账号 {name} 浏览器 Cookie 已失效，且未配置账号密码。请前往控制台重新登录。', level='warning')

                if not relogin_ok:
                    if token:
                        config.log(name, 'Cookie 会话失效且重登未成，但 OAuth Token 仍有效：优雅降级仅执行 API 阶段')
                        degraded_mode = True
                        bsess.degraded_mode = True
                        try:
                            bsess.close()
                        except Exception:
                            pass
                        bsess = None
                        for bph in browser_phases:
                            results_map[bph] = '失败(会话失效)'
                        browser_phases = []
                    else:
                        try:
                            bsess.close()
                        except Exception:
                            pass
                        bsess = None
                        raise RuntimeError('无可用登录凭据 (Token 与 Cookie 均已失效，自动重登未成功)')

            elif reason == 'NETWORK_ERROR':
                config.log(name, '前置测活遇到网络/代理超时或连接故障，按网络环境异常容错处理 (不触发重登)')

            if bsess and bsess.is_authenticated:
                try:
                    from msr import tasks_browser as tb
                    bsess.newly_activated = tb.auto_activate_promotions_and_streaks(bsess.cdp, ch=bsess.ch, log=bsess.logger)
                    if any(a in ('double_search', 'search_2x', 'perk') for a in bsess.newly_activated):
                        bsess.logger('检测到已激活搜索翻倍等特权，正在刷新账户促销状态以生效新的搜索上限...')
                        try:
                            from msr import probe
                            pr = probe.probe_account(name, token, proxy=proxy)
                            bsess.pr = pr
                        except Exception as ex_pr:
                            bsess.logger(f'刷新账户促销状态异常(容忍): {ex_pr}')
                except Exception as ex_act:
                    bsess.logger(f'自动激活福利与 Streak 检查异常: {ex_act}')

        except Exception as ex_b_pre:
            config.log(name, f'前置浏览器检测异常: {ex_b_pre}')
            if token and not degraded_mode:
                config.log(name, '浏览器前置异常但 Token 有效，已降级为仅执行 API 阶段')
                degraded_mode = True
                if bsess:
                    try:
                        bsess.close()
                    except Exception:
                        pass
                bsess = None
                for bph in browser_phases:
                    results_map[bph] = '失败(会话失效)'
                browser_phases = []
            elif not token:
                if bsess:
                    try:
                        bsess.close()
                    except Exception:
                        pass
                bsess = None
                raise

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

    try:
        # 3. 前台启动单个浏览器会话，真人流转依次执行该账号的所有浏览器阶段
        if browser_phases and bsess is not None:
            b_results = bsess.run_phases(browser_phases)
            results_map.update(b_results)
        elif browser_phases and bsess is None and not degraded_mode:
            bsess = executor.BrowserSession(name, token, pr, p, config,
                                            proxy=proxy, cdp_port=cdp_port)
            bsess.start()
            try:
                from msr import tasks_browser as tb
                bsess.newly_activated = tb.auto_activate_promotions_and_streaks(bsess.cdp, ch=bsess.ch, log=bsess.logger)
                if any(a in ('double_search', 'search_2x', 'perk') for a in bsess.newly_activated):
                    bsess.logger('检测到已激活搜索翻倍等特权，正在刷新账户促销状态以生效新的搜索上限...')
                    try:
                        from msr import probe
                        pr = probe.probe_account(name, token, proxy=proxy)
                        bsess.pr = pr
                    except Exception as ex_pr:
                        bsess.logger(f'刷新账户促销状态异常(容忍): {ex_pr}')
            except Exception as ex_act:
                bsess.logger(f'自动激活福利与 Streak 检查异常: {ex_act}')
            b_results = bsess.run_phases(browser_phases)
            results_map.update(b_results)

        # 4. 若有后台 Edge 打卡心跳线程，等待其 30 分钟周期结束 (仅在后台线程仍在运行时等待)
        if edge_thread is not None and edge_thread.is_alive():
            config.log(name, '前台浏览器任务已全部就绪，等待后台 Edge 30分钟打卡结束...')
            edge_thread.join()

        # 4.1 新手首次激活联动：若最初未下发跳过，但前台浏览器刚刚完成了 Edge 打卡任务激活，则触发补跑
        newly_activated = b_results.get('_newly_activated', []) if browser_phases else []
        if 'edge' in newly_activated and results_map.get(edge_ph) == '未下发Edge任务(跳过)':
            config.log(name, '检测到浏览器刚刚完成了 Edge 浏览打卡的新手激活，正在启动今日打卡心跳...')
            try:
                r = executor.run_api_phase(name, edge_ph, token, pr, p, config, api, proxy=proxy)
                results_map[edge_ph] = r
                config.log(name, '<<< Edge 30分钟新手初次打卡完成: %s' % r)
            except Exception as ex:
                results_map[edge_ph] = '失败(%s)' % str(ex)[:60]
                config.log(name, '!!! Edge 30分钟打卡异常: %s' % str(ex)[:120])

        # 5. 按照原始 plan 顺序组装结果
        results = ['%s:%s' % (w['phase'], results_map.get(w['phase'], '未执行')) for w in plan]

        # 阶段 10 · 容错核查与智能回补修复 (repair)
        # 深度达标审计：全方位核查未满额(如 9/15, 25/50)、未完成、部分完成或残留未做卡片
        bad = []
        for r in results:
            ph, _, val = r.partition(':')
            is_bad = False
            if val.startswith('失败') or val.startswith('未完成') or val.startswith('未满额') or val.startswith('部分完成'):
                is_bad = True
            elif '未complete' in val or '未出现' in val or '待完成' in val or '待做' in val:
                is_bad = True
            elif ph in ('search', 'pcsearch') and ('/' in val):
                m = re.search(r'(\d+)\s*/\s*(\d+)', val)
                if m and int(m.group(1)) < int(m.group(2)):
                    is_bad = True
            elif ph in ('visual', 'visualsearch') and ('未完成' in val):
                is_bad = True

            if is_bad:
                bad.append(ph)

        # 降级模式下浏览器会话不可用，过滤掉所有浏览器阶段的回补尝试
        if degraded_mode:
            bad = [ph for ph in bad if next((t['needs'] for t in planner.TASK_TABLE.values() if t['phase'] == ph), 'api') == 'api']

        if bad and not args.dry_run:
            from msr import settings as S
            st_runner = S.load_settings()
            fast_mode = bool(st_runner.get('schedule', {}).get('fast_mode', False))
            # 阶段9到阶段10中间的等待时间改成随机1-3分钟
            wait_s = 5 if fast_mode else random.uniform(60, 180)
            config.log(name, '[阶段 10 · 容错核查] 发现未满分/未完成项目: %s, 冷却等待 %.1f 秒后启动同会话精准回补...' % (bad, wait_s))
            time.sleep(wait_s)
            bad_api = [ph for ph in bad if next((t['needs'] for t in planner.TASK_TABLE.values() if t['phase'] == ph), 'api') == 'api']
            bad_browser = [ph for ph in bad if ph not in bad_api]
            for ph in bad_api:
                try:
                    r = executor.run_api_phase(name, ph, token, pr, p, config, api, proxy=proxy)
                    results_map[ph] = '回补成功(%s)' % r
                    config.log(name, '[阶段 10 自动回补] %s: %s' % (ph, r))
                except Exception as ex:
                    results_map[ph] = '回补仍失败(%s)' % str(ex)[:50]
                    config.log(name, '[阶段 10 自动回补] %s 仍失败: %s' % (ph, str(ex)[:100]))
            if bad_browser and not degraded_mode:
                # 若有浏览器任务回补，自动附带 claim 阶段以保证补全后新释放的待存入积分能被立即结算入账
                if 'claim' not in bad_browser:
                    bad_browser.append('claim')
                if bsess is not None and bsess.ch is not None:
                    # 复用当前已开启的浏览器会话，杜绝重新拉起 Chromium 的低效开销
                    b_fixes = bsess.run_phases(bad_browser, is_retry=True)
                else:
                    b_fixes = executor.run_browser_session(name, bad_browser, token, pr, p, config, proxy=proxy, cdp_port=cdp_port)
                for ph, r in b_fixes.items():
                    results_map[ph] = '回补(%s)' % r
                    config.log(name, '[阶段 10 自动回补] %s: %s' % (ph, r))

            # 重新生成更新后的最终结果列表
            results = ['%s:%s' % (w['phase'], results_map.get(w['phase'], '未执行')) for w in plan]
    finally:
        # 在阶段10全部结束（包括回补完成）后再彻底关闭浏览器释放内存
        if bsess is not None:
            bsess.close()

    # 获取运行后最新余额
    bal_after = bal_before
    if token:
        rg_me = {'country': pr.get('country', 'US'), 'language': pr.get('language', 'en-US')}
        for retry_i in range(3):
            try:
                me_resp = api.get_me(name, token, rg_me, proxy=proxy)
                b = me_resp.get('balance') or me_resp.get('profile', {}).get('balance')
                if b is not None and int(b) > 0:
                    bal_after = int(b)
                    break
            except Exception:
                if retry_i < 2:
                    time.sleep(3)

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
        summary_title = f'MSR 运行报告 [{name}] (+{gained}分)'
        summary_lines = [
            f'### 账号: {name} (区域: {pr.get("country")}, 代理: {proxy or "直连"})',
            f'- **当前积分**: {bal_after} (今日变动: +{gained} 分)',
            '- **执行任务清单**:'
        ]
        for r in results:
            summary_lines.append(f'  - `{r}`')
        summary_lines.append(f'- **结束时间**: {time.strftime("%F %T")}')
        notify.notify(summary_title, '\n'.join(summary_lines), level='summary')
