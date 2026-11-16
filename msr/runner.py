import os
import random
import re
import time
import fcntl

_LOCK_FDS = {}


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


def _edge_already_activated(pr):
    """Edge 30 分钟打卡是否已激活(= /dapi/me 下发 edge_browsing_streak_flight)。

    2026-11-11 用户口径: 激活是每个账号一次性的事, 激活过就不该每天再点。
    拿不到促销快照时返回 None(=未知), 由调用方按旧行为处理, 不擅自判定"已激活"。
    """
    try:
        if not isinstance(pr, dict) or 'promotions' not in pr:
            return None          # 没有促销快照 -> 未知, 交给调用方按旧行为处理
        promos = pr.get('promotions') or []
        return any((p or {}).get('name') == 'edge_browsing_streak_flight' for p in promos)
    except Exception:
        return None


def _streak_diag(name, proxy, log):
    """只做诊断: 打一行 /earn payload 的 partner 列表, **不参与任何计划决策**。

    2026-11-13 事故: `_prune_by_streaks` 曾用"payload 缺目标 partner"判断"账号未下发",
    把 dailyset / visual 从计划里摘掉。实测同一账号 12 分钟内前后两次读取结论相反
    (某US号 23:36 读到 dailyset 0/3, 23:48 又读不到该对象; 事后连读 10 次全部 5 个 partner 齐全),
    某新号2 20:47 更是 dailyset 与 visualsearch 同时"消失"。
    payload 的 RSC 片段会随页面渲染状态变, **不稳定, 不能拿来做"跳过任务"的判据** ——
    宁可多跑一次浏览器(页面自己会认卡片状态), 也不能漏做。
    现在只保留这行诊断, 决策全部交给浏览器阶段。
    """
    try:
        from msr.tasks import streaks
        diag = {}
        st = streaks.fetch_streaks(name, proxy=proxy, stats=diag)
        if st is None:
            log('[状态诊断] /earn 读取失败(status=%s), 不影响计划' % diag.get('status'))
        else:
            log('[状态诊断] /earn payload %d 字节, partner=[%s]'
                % (diag.get('bytes') or 0, ','.join(sorted(st.keys()))))
    except Exception as ex:
        log('[状态诊断] 读取异常(忽略): %s' % str(ex)[:60])


def acquire_runner_lock(config, name=None):
    """跨进程内核级文件排他锁 (fcntl.flock)，支持细粒度单账号锁隔离与全局锁向下兼容。"""
    global _LOCK_FDS
    if name:
        p = config.profile(name)
        lock_path = os.path.join(p, 'runner.lock')
    else:
        lock_path = os.path.join(config.HOME, 'runner.lock')
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            holder = ''
            try:
                with open(lock_path, 'r', encoding='utf-8') as f:
                    holder = f.read().strip()
            except Exception:
                pass
            hp = int(holder) if holder.isdigit() else 0

            # 1. 本进程自己确实持有(在 _LOCK_FDS 里): 正常返回占用
            if lock_path in _LOCK_FDS:
                os.close(fd)
                return None
            # 2. 真有一个别的 msr run 进程活着: 正常返回占用
            if hp > 1 and hp != os.getpid() and is_runner_process_alive(hp):
                os.close(fd)
                return None

            # 3. 其余都是残留锁: 容器里 PID 恒为 1(init=本进程)、或进程已消失、或本进程历史泄漏的 fd。
            #    解除旧 fd 的占用 + 删文件换 inode, 再重新加锁。
            os.close(fd)
            old_fd = _LOCK_FDS.pop(lock_path, None)
            if old_fd is not None:
                try:
                    fcntl.flock(old_fd, fcntl.LOCK_UN)
                    os.close(old_fd)
                except Exception:
                    pass
            try:
                os.remove(lock_path)
            except Exception:
                pass
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                os.close(fd)
                return None

        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode('utf-8'))
        _LOCK_FDS[lock_path] = fd
        return lock_path
    except Exception:
        return None


def release_runner_lock(lock_path):
    """释放内核排他锁并清理锁文件。"""
    global _LOCK_FDS
    if not lock_path:
        return
    fd = _LOCK_FDS.pop(lock_path, None)
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except Exception:
            pass
    if lock_path and os.path.isfile(lock_path):
        try:
            os.remove(lock_path)
        except Exception:
            pass


def _save_state_merged(config, name, st):
    """落 `state.json` 前必须重新读盘再 merge, **不能**用开跑时的快照整体覆写。

    2026-11-12 实测事故: Edge 30 分钟打卡是后台并行线程, 它按奖励日写
    `edge_last_report_day`; 主流程结束时用开局快照整体覆写, 把这个标记清掉了
    (6 个账号里 5 个丢失)。后果是同一奖励日再重跑会重复上报 6 次心跳 ——
    正是该标记要防的事。凡"运行中可能有别的线程/进程写 state"的场景, 一律走这里。
    """
    fresh = config.load_json(config.pfile(name, 'state.json'), {}) or {}
    if not isinstance(fresh, dict):
        fresh = {}
    fresh.update(st or {})
    config.save_json(config.pfile(name, 'state.json'), fresh)
    return fresh


def cmd_run(args):
    from msr import config, planner, probe, api, executor, notify, settings

    st_cfg = settings.load_settings()
    sch_cfg = st_cfg.get('schedule', {})

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
        name = target_acc['name']
        if not getattr(args, 'dry_run', False):
            lock = acquire_runner_lock(config, name=name)
            if not lock:
                print(f'账号 [{name}] 已有任务正在运行中，跳过本次执行。')
                return 0
        else:
            lock = None

        try:
            return run_one(name, args, config, planner, probe, api, executor, notify, settings) or 0
        except Exception as ex:
            err_msg = '运行失败: %s' % str(ex)[:150]
            config.log(name, '!!! %s' % err_msg)
            notify.notify(f'MSR 异常 [{name}]', err_msg, level='error')
            return 1
        finally:
            if lock:
                release_runner_lock(lock)
    else:
        accs = [a for a in all_accs if a.get('enabled', True)]
        if not accs:
            print('当前所有账号均处于停用状态，跳过本次批量执行。')
            return 0

        max_conc = int(sch_cfg.get('max_concurrency', 2))
        max_conc = max(1, min(max_conc, 10))

        from concurrent.futures import ThreadPoolExecutor, as_completed
        rc = 0

        def _execute_account(a):
            nonlocal rc
            name = a['name']
            if not getattr(args, 'dry_run', False):
                lock = acquire_runner_lock(config, name=name)
                if not lock:
                    config.log(name, f'账号 [{name}] 已有任务正在运行中，跳过。')
                    return
            else:
                lock = None

            try:
                run_one(name, args, config, planner, probe, api, executor, notify, settings)
            except Exception as ex:
                err_msg = '运行失败: %s' % str(ex)[:150]
                config.log(name, '!!! %s' % err_msg)
                notify.notify(f'MSR 异常 [{name}]', err_msg, level='error')
                rc = 1
            finally:
                if lock:
                    release_runner_lock(lock)

        with ThreadPoolExecutor(max_workers=max_conc) as executor_pool:
            futures = [executor_pool.submit(_execute_account, a) for a in accs]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as ex:
                    print(f'批量任务异常: {ex}')
                    rc = 1
        return rc


def _has_login_domain_session(cookie_list):
    """cookie.json 里是否还留有登录域会话 (C 分支 cookie->token 的硬前提, 见设计文档 §10.3-5)。"""
    for c in cookie_list or []:
        d = str(c.get('domain') or '').lower()
        if 'live.com' in d or 'microsoftonline' in d:
            return True
    return False


def _c_branch_precheck(name, config):
    """C 分支只读预检 (不抢锁): 该账号是否已有存活 runner 或该 profile 已有 Chromium。命中返回原因。"""
    p = config.profile(name)
    lock_path = os.path.join(p, 'runner.lock')
    try:
        if os.path.isfile(lock_path):
            with open(lock_path, 'r', encoding='utf-8') as f:
                holder = f.read().strip()
            if holder.isdigit() and int(holder) > 1 and int(holder) != os.getpid() \
                    and is_runner_process_alive(int(holder)):
                return f'runner.lock 被存活进程 {holder} 持有'
    except Exception:
        pass
    try:
        for pid in os.listdir('/proc'):
            if not pid.isdigit():
                continue
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as f:
                    cl = f.read().decode('utf-8', 'ignore')
            except Exception:
                continue
            if 'chrom' in cl and p in cl:
                return f'profile 已有 Chromium 进程 (pid {pid})'
    except Exception:
        pass
    return None


def _proxy_precheck(proxy, target='https://www.bing.com/favicon.ico', timeout=20):
    """P2: 代理链路前置探测。仅链路层失败算失败; 目标站返回任何 HTTP 状态码都算通。"""
    import urllib.error
    import urllib.request
    url = str(proxy) if '://' in str(proxy) else 'http://' + str(proxy)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': url, 'https': url}))
    detail = ''
    for i, delay in enumerate((0, 15, 30, 60)):
        if delay:
            time.sleep(delay)
        t0 = time.time()
        try:
            resp = opener.open(target, timeout=timeout)
            code = getattr(resp, 'status', 200)
            resp.close()
            return True, '第%d次探测链路通 (HTTP %s, %.1fs, proxy=%s)' % (i + 1, code, time.time() - t0, url)
        except urllib.error.HTTPError as ex:
            return True, '第%d次探测链路通 (目标站 HTTP %s, 按契约仍算通, %.1fs)' % (i + 1, ex.code, time.time() - t0)
        except Exception as ex:
            reason = getattr(ex, 'reason', ex)
            detail = '第%d次探测失败: %s: %s' % (i + 1, type(reason).__name__, str(reason)[:100])
    return False, detail + ' (首次+3次重试全败, 放弃发车)'


def _authenticate(name, config, api, auth, probe, proxy, p, ck_path, notify=None):
    """P0 认证状态机 (设计文档 §10.4): 0预检 → B refresh → C cookie换token → D 密码重登 → E 降级。

    返回 (token, pr, method, notes)。method ∈ refresh / refresh(old_access) / cookie / password / cookie_only / none
    关键纪律: 网络失败不许判凭据失效, 也不许降级到重登; 只有 HTTP 400/401 才走 C 分支。
    """
    from msr import cookies as cookies_mod
    notes = []
    method = 'none'
    token = None
    pr = None
    rg = config.load_json(config.pfile(name, 'region.json')) or {}

    # 0 预检: cookie 只读判定 (不打网络)
    ck_list = config.load_json(ck_path) or []
    if isinstance(ck_list, dict):
        ck_list = ck_list.get('cookies') or []
    ck_state = cookies_mod.validate_cookies(ck_list)
    login_domain = _has_login_domain_session(ck_list)
    notes.append('预检 cookie: %s (共%s枚, 登录域=%s)' % (
        ck_state.get('reason'), ck_state.get('count'), '有' if login_domain else '无'))

    tok = config.load_json(config.pfile(name, 'token.json')) or {}

    # B 分支: 开头直接 refresh (access 只活 1 小时, 任务一天一次)
    from msr.api import AuthError as _AuthErr, NetworkAuthError as _NetErr
    network_blocked = False
    if tok.get('refresh_token'):
        old_access = tok.get('access_token')
        try:
            token = api.refresh_access(name, p, rg, proxy=proxy)
            method = 'refresh(old_access)' if token and token == old_access else 'refresh'
            notes.append('B分支 refresh 成功')
        except Exception as ex:
            if isinstance(ex, _NetErr):
                network_blocked = True
                notes.append('B分支 网络失败, 不判失效也不降级: %s' % str(ex)[:90])
            elif isinstance(ex, _AuthErr):
                notes.append('B分支 凭据被拒(400/401): %s' % str(ex)[:90])
            else:
                # 未知异常: 保守按"凭据可能失效"继续走 C 分支 (与历史行为一致, 不丢自愈能力)
                notes.append('B分支 未知异常, 继续尝试 C 分支: %s' % str(ex)[:90])
    else:
        notes.append('B分支 跳过(无 refresh_token)')

    if not token and not network_blocked:
        # C 分支: cookie 静默换 token (只读预检, 不抢锁)
        if ck_state.get('valid') and login_domain:
            blocked = _c_branch_precheck(name, config)
            if blocked:
                notes.append('C分支 跳过(预检命中): %s' % blocked)
            else:
                try:
                    new_tok = auth.exchange_token_from_cookies(name, proxy=proxy)
                    if new_tok and (new_tok.get('refresh_token') or new_tok.get('access_token')):
                        token = api.refresh_access(name, p, rg, proxy=proxy)
                        method = 'cookie'
                        notes.append('C分支 cookie 静默换 token 成功')
                    else:
                        notes.append('C分支 未能静默获取凭据 (cookie 亦失效?)')
                except Exception as ex_ck:
                    notes.append('C分支 异常: %s' % str(ex_ck)[:90])
        else:
            notes.append('C分支 跳过(缺 _U 或登录域会话, 省掉注定失败的 20 秒)')

        # D 分支: 账号密码重登
        if not token:
            cred = config.get_credential(name)
            if cred and cred.get('username') and cred.get('password'):
                try:
                    res = auth.login_with_credential(name, cred['username'], cred['password'], proxy=proxy,
                                                     log_cb=lambda m: config.log(name, m))
                    if res.get('status') in ('ok', 'success'):
                        token = api.refresh_access(name, p, rg, proxy=proxy)
                        method = 'password'
                        notes.append('D分支 密码重登成功')
                    else:
                        detail = (res.get('error') or res.get('message') or '未知')[:80]
                        notes.append('D分支 重登未完成: %s' % detail)
                        if notify:
                            notify.notify(f'MSR 凭据需接管 [{name}]',
                                          f'账号 {name} 自动重登需用户配合质询: {detail}\n请前往 Web 控制台接管登录。',
                                          level='warning')
                except Exception as ex2:
                    notes.append('D分支 异常: %s' % str(ex2)[:90])
            else:
                notes.append('D分支 跳过(未保存密码)')

    # probe: 拿到 token 立刻验一次身份与余额
    if token:
        try:
            pr = probe.probe_account(name, token, proxy=proxy)
            pr['probed_at'] = time.strftime('%F %T')
            config.save_json(config.pfile(name, 'region.json'), pr)
            notes.append('probe 成功: region=%s balance=%s' % (pr.get('country'), pr.get('balance')))
        except Exception as ex_pr:
            notes.append('probe 失败(token 不可用): %s' % str(ex_pr)[:90])
            pr = None
            token = None
            if method in ('refresh', 'refresh(old_access)', 'cookie', 'password'):
                method = 'none'

    if not token and not network_blocked and method == 'none':
        if ck_state.get('valid'):
            method = 'cookie_only'
            notes.append('E分支 降级: 纯浏览器模式 (无 API token, cookie 可用)')
        elif not (ck_list or tok.get('refresh_token') or tok.get('access_token')):
            raise RuntimeError('无可用登录凭据 (未配置账号密码、Cookie 或 Token)，先 msr login %s' % name)
        else:
            method = 'cookie_only'
            notes.append('E分支 降级: 纯浏览器模式 (cookie 无 _U, 大概率失败)')

    if network_blocked and not token:
        method = 'cookie_only'
        notes.append('E分支 网络失败: 进入纯浏览器模式, API 阶段不可用')

    return token, pr, method, notes


def clean_result_for_report(r):
    """阶段结果串 -> 汇报用文本: 去掉中途余额噪音, 余额只在汇报结尾报一次。

    覆盖 executor('Edge完成(6次), 余额=8945')、('... 余额=8945(计分可能延迟)')
    与 claim('成功领取5分(余额:9115)') 三种写法。
    """
    if not r:
        return ''
    s = str(r)
    def _drop(m):
        # 后面紧跟括号时留一个空格, 避免 'complete=True(计分可能延迟)' 粘连
        return ' ' if m.string[m.end():m.end() + 1] in '(（' else ''

    s = re.sub(r'[,，]?\s*余额\s*=\s*\d+', _drop, s)
    s = re.sub(r'[(（]\s*余额\s*[:：]\s*\d+\s*[)）]', '', s)
    s = re.sub(r'\s{2,}', ' ', s).strip(' ,，')
    return s


def build_summary_lines(name, pr, proxy, results, bal_before, bal_after, gained, now=None):
    """运行汇报正文(用户 2026-11-10 定的口径)。

    开头只报"任务开始前从 API 拿到的实时积分"; 各阶段结果串里的中途余额全部去掉;
    结尾只报一次"完成后积分"(否则开头与结尾的余额重复)。
    """
    lines = [
        f'### 账号: {name} (区域: {pr.get("country")}, 代理: {proxy or "直连"})',
        f'- **任务开始前积分(API实时)**: {bal_before}',
        '- **执行任务清单**:',
    ]
    for r in results:
        lines.append(f'  - `{clean_result_for_report(r)}`')
    lines.append(f'- **完成后积分**: {bal_after} (今日变动: +{gained} 分)')
    lines.append(f'- **结束时间**: {now or time.strftime("%F %T")}')
    return lines


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
    run_started_at = time.strftime('%F %T')

    # P2-①: 代理前置探测 —— 链路不通不发车 (仅"连不上代理/隧道失败/超时/DNS失败"算失败,
    # 目标站返回任何 HTTP 状态码都算通)。首次 + 3 次重试, 退避 15/30/60 秒。
    if proxy:
        ok, detail = _proxy_precheck(proxy)
        config.log(name, '[代理前置探测] ' + detail)
        if not ok:
            st_blk = config.load_json(config.pfile(name, 'state.json'), {}) or {}
            st_blk.setdefault('daily', {})['status'] = 'blocked'
            st_blk['daily']['blocked_at'] = time.strftime('%F %T')
            st_blk['daily']['blocked_reason'] = detail[:200]
            config.save_json(config.pfile(name, 'state.json'), st_blk)
            if notify:
                notify.notify(f'MSR 代理不通 [{name}]', detail, level='error')
            raise RuntimeError('代理前置探测失败, 未发车: ' + detail)

    # P1: 每轮把本次生效参数打到日志头 (§7.3 契约5)
    try:
        from msr import settings as _S
        _cfg = _S.load_settings()
        config.log(name, '=== 本次生效参数 ===')
        for line in _S.effective_banner(_cfg):
            config.log(name, '  · ' + line)
    except Exception as ex_eff:
        config.log(name, f'生效参数横幅打印失败(容忍): {ex_eff}')

    # 1. 前置认证状态机 (P0: 0预检 → B refresh → C cookie换token → D 密码重登 → E 降级)
    token, pr, auth_method, auth_notes = _authenticate(name, config, api, auth, probe, proxy, p, ck_path, notify=notify)
    for n in auth_notes:
        config.log(name, '[认证] ' + n)
    config.log(name, '[认证] 采用方式: %s' % auth_method)

    try:
        from msr import browser as _br_r
        _br_r.nav_reset()          # 埋点: 整轮 navigate 计数从 0 起算
    except Exception:
        pass
    config.log(name, '=== 运行开始 (proxy=%s cdp_port=%s) ===' % (proxy or '直连', cdp_port or '内置'))

    if not pr:
        # 纯 Cookie 模式：跳过 API 阶段，全额执行浏览器搜索与卡片活动
        config.log(name, '当前以纯 Cookie 模式运行: 跳过 API 阶段，全额执行浏览器搜索与活动打卡')
        pr = rg or {'country': 'SG', 'language': 'zh-hans', 'balance': 0, 'task_names': []}
        plan = [{'phase': ph, 'needs': 'browser'} for ph in planner.BROWSER_ALWAYS]
    else:
        plan = planner.plan(pr, force_phases=args.only)
        plan_full = list(plan)        # 完整计划, 汇报按它排序
        # 只打一行状态诊断; 计划不再按 payload 摘除阶段(2026-11-13 事故: payload 缺对象被当成
        # "账号未下发", 直接把 dailyset/visual 摘掉 -> 当天漏做。详见 _streak_diag 注释)
        _streak_diag(name, proxy, lambda m: config.log(name, m))
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
                    bsess.newly_activated = tb.auto_activate_promotions_and_streaks(
                        bsess.cdp, ch=bsess.ch, log=bsess.logger,
                        edge_activated=_edge_already_activated(pr))
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
                bsess.newly_activated = tb.auto_activate_promotions_and_streaks(
                        bsess.cdp, ch=bsess.ch, log=bsess.logger,
                        edge_activated=_edge_already_activated(pr))
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

        # 4.1 新手首次激活联动：若最初未下发跳过，但前台浏览器交互激活或服务端下发了 flight，则触发补跑
        newly_activated = b_results.get('_newly_activated', []) if browser_phases else []
        edge_needs_retry = False
        if results_map.get(edge_ph) == '未下发Edge任务(跳过)':
            if 'edge' in newly_activated:
                edge_needs_retry = True
            else:
                try:
                    pr_check = api.get_me(name, token, {'country': pr.get('country', 'US'), 'language': pr.get('language', 'en')}, proxy=proxy)
                    if api.find_promo(pr_check, 'edge_browsing_streak_flight'):
                        edge_needs_retry = True
                except Exception:
                    pass

        if edge_needs_retry:
            config.log(name, '检测到已具备 Edge 浏览打卡资格/任务，正在启动今日打卡心跳...')
            try:
                r = executor.run_api_phase(name, edge_ph, token, pr, p, config, api, proxy=proxy)
                results_map[edge_ph] = r
                config.log(name, '<<< Edge 30分钟新手打卡完成: %s' % r)
            except Exception as ex:
                results_map[edge_ph] = '失败(%s)' % str(ex)[:60]
                config.log(name, '!!! Edge 30分钟打卡异常: %s' % str(ex)[:120])

        # 5. 按照原始 plan 顺序组装结果
        results = ['%s:%s' % (w['phase'], results_map.get(w['phase'], '未执行')) for w in plan_full]

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
            elif ph == 'claim' and ('结算中' in val or '待存入' in val):
                is_bad = True

            if is_bad:
                bad.append(ph)

        # 降级模式下浏览器会话不可用，过滤掉所有浏览器阶段的回补尝试
        if degraded_mode:
            bad = [ph for ph in bad if next((t['needs'] for t in planner.TASK_TABLE.values() if t['phase'] == ph), 'api') == 'api']

        if bad and not args.dry_run:
            from msr import settings as S
            st_runner = S.load_settings()
            # P1: retry_wait_minutes 接上 (历史硬编码 60-180 秒, 参数改了不生效)
            rw_min = st_runner.get('schedule', {}).get('retry_wait_minutes', 4)
            try:
                rw_min = float(rw_min)
            except (TypeError, ValueError):
                rw_min = 4.0
            if rw_min <= 0:
                rw_min = 4.0
            wait_s = random.uniform(rw_min * 60 * 0.85, rw_min * 60 * 1.15)
            config.log(name, '[阶段 10 · 参数] schedule.retry_wait_minutes=%g -> 实际冷却 %.0f 秒' % (rw_min, wait_s))
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
                # P4 安全网: 新路由(moreactivities/explore/dailyset)失败时, 本次回补强制走 legacy 老路径,
                # 不改全局配置, 避免新版 handler 一旦出问题把当天卡片报废。
                # 2026-11-11: 路由已固化为 new (executor.URL_OPEN_ROUTE), 回补无条件强制 legacy
                route_ov = 'legacy' if any(
                    ph in ('moreactivities', 'explore', 'dailyset') for ph in bad_browser) else None
                if route_ov:
                    config.log(name, '[P4 安全网] 新路由阶段回补自动切 legacy: %s'
                               % [ph for ph in bad_browser if ph in ('moreactivities', 'explore', 'dailyset')])
                # 若有可产生待存入积分的浏览器任务(dailyset/moreactivities/quests)回补，或 claim 本身待回补，自动附带 claim 阶段
                claim_needed = ('claim' in bad) or any(t in bad_browser for t in ('dailyset', 'moreactivities', 'quests'))
                if claim_needed and 'claim' not in bad_browser:
                    bad_browser.append('claim')
                if bsess is not None and bsess.ch is not None:
                    # 复用当前已开启的浏览器会话，杜绝重新拉起 Chromium 的低效开销
                    b_fixes = bsess.run_phases(bad_browser, is_retry=True, route_override=route_ov)
                else:
                    b_fixes = executor.run_browser_session(name, bad_browser, token, pr, p, config, proxy=proxy,
                                                          cdp_port=cdp_port, route_override=route_ov)

                # 终极靶向自愈：若同会话回补后仍有卡片任务(如 visual/dailyset)未完成，说明原会话经历长时间运行后可能存在连接衰减或必应频控，
                # 立即销毁旧会话并重启全新独立干净会话做终极靶向兜底
                still_bad = [ph for ph, r in b_fixes.items() if ('未完成' in r or '失败' in r) and ph != 'search']
                if still_bad and bsess is not None:
                    config.log(name, '[阶段 10 终极兜底] 复用会话中仍有未完成项目 %s，正在重启独立干净会话兜底...' % still_bad)
                    try:
                        bsess.close()
                    except Exception:
                        pass
                    bsess = None
                    fresh_fixes = executor.run_browser_session(name, still_bad, token, pr, p, config, proxy=proxy,
                                                              cdp_port=cdp_port, route_override=route_ov)
                    b_fixes.update(fresh_fixes)
                for ph, r in b_fixes.items():
                    if ph == 'claim':
                        prev_claim = results_map.get('claim', '')
                        if prev_claim.startswith('成功领取'):
                            if r == '无可领取积分(跳过)':
                                config.log(name, '[阶段 10 自动回补] claim: 前序已成功领取，回补无可增量积分，保留原成功状态 (%s)' % prev_claim)
                                continue
                            elif r.startswith('成功领取'):
                                results_map[ph] = '%s + 回补(%s)' % (prev_claim, r)
                                config.log(name, '[阶段 10 自动回补] claim 增量领取成功: %s' % r)
                                continue
                        elif prev_claim == '无可领取积分(跳过)' and r == '无可领取积分(跳过)':
                            results_map[ph] = '无可领取积分(跳过)'
                            config.log(name, '[阶段 10 自动回补] claim: 无可领取积分(跳过)')
                            continue
                        elif r.startswith('成功领取'):
                            results_map[ph] = '回补成功(%s)' % r
                            config.log(name, '[阶段 10 自动回补] claim 回补成功: %s' % r)
                            continue

                    results_map[ph] = '回补(%s)' % r
                    config.log(name, '[阶段 10 自动回补] %s: %s' % (ph, r))

            # 重新生成更新后的最终结果列表
            results = ['%s:%s' % (w['phase'], results_map.get(w['phase'], '未执行')) for w in plan_full]
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
    # P0: 认证结果落盘 (控制台/日志一眼看出今天靠哪条路活的)
    st['auth_method'] = auth_method
    st['auth_at'] = time.strftime('%F %T')
    # P2: 当日运行记录 (只记录, 不做补偿调度)
    try:
        from msr import browser as _br_n
        _navn = _br_n.nav_count()
    except Exception:
        _navn = None
    st['last_run_record'] = {
        'start': run_started_at,
        'navigate_count': _navn,
        'end': st['last_run'],
        'gained': gained,
        'balance_before': bal_before,
        'balance_after': bal_after,
        'auth_method': auth_method,
        'degraded': bool(degraded_mode),
        'results': results,
    }
    st.setdefault('daily', {})['status'] = 'ok'
    st['daily']['last_date'] = time.strftime('%F')
    # 不能整体覆写: 运行期间后台并行的 Edge 心跳线程会写 edge_last_report_day(见函数注释)
    _save_state_merged(config, name, st)

    try:
        from msr import history
        history.record_daily_point(name, start_bal=bal_before, end_bal=bal_after, results=results)
    except Exception:
        pass

    try:
        from msr import browser as _br_e
        _ns = _br_e.nav_stats()
        config.log(name, '[埋点] 整轮 navigate 共 %d 次(P6 目标 <=3): %s'
                   % (_ns['count'], ' -> '.join(u.split('?')[0][:60] for u in _ns['urls'][:12])))
    except Exception as _ex:
        config.log(name, '[埋点] navigate 统计异常: %s' % str(_ex)[:60])

    config.log(name, '=== 运行结束 (余额: %s, 今日增量: +%d): %s ===' % (bal_after, gained, ' | '.join(results)))

    if notify:
        summary_title = f'MSR 运行报告 [{name}] (+{gained}分)'
        summary_lines = build_summary_lines(name, pr, proxy, results, bal_before, bal_after, gained)
        notify.notify(summary_title, '\n'.join(summary_lines), level='summary')
