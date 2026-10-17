"""账号登录引导: device-code(必做) + cookie 导入(浏览器任务可选) + 代理配置。"""
import json
import os


def cmd_login(args):
    from msr import config, api, probe

    name = args.name
    proxy = getattr(args, 'proxy', None)
    cdp_port = getattr(args, 'cdp_port', None)
    if cdp_port:
        cdp_port = int(cdp_port)

    p = config.profile(name)
    config.log(name, '=== 登录引导开始 (proxy=%s) ===' % (proxy or '直连'))

    # 第1步: device-code (API 任务 + region 探测)
    print('\n[1/2] 设备码授权(纯 HTTP, 手机/任意浏览器完成):')
    dc = api.device_code_login(name, proxy=proxy)
    print('  打开: %s' % dc.get('verification_uri'))
    print('  输入代码: %s' % dc.get('user_code'))
    print('  等待授权', flush=True)
    t = api.poll_device_code(name, dc['device_code'],
                             interval=int(dc.get('interval', 5)),
                             proxy=proxy)
    api.save_token(name, t, p)
    config.log(name, 'device-code 授权成功, token 已存')

    # 第2步: 探测区域/任务集
    rg_path = config.pfile(name, 'region.json')
    tok = config.load_json(config.pfile(name, 'token.json'))
    pr = probe.probe_account(name, tok['access_token'], proxy=proxy)
    config.save_json(rg_path, pr)
    print('\n[2/2] 账号探测结果:')
    print('  country : %s' % pr.get('country'))
    print('  language: %s' % pr.get('language'))
    print('  等级    : %s' % (pr.get('levelInfo') or {}).get('activeLevel'))
    print('  可做任务 : %s' % ', '.join(pr.get('task_names', [])))

    # 可选: cookie 导入
    if getattr(args, 'cookies', None):
        from msr import cookies
        cookies.import_file(name, args.cookies)
        print('\ncookie 已导入(浏览器任务可用)')
    else:
        print('\n(未导入浏览器 cookie -- 如该账号有浏览器侧任务, 建议补:')
        print(' msr login %s --cookies <导出的cookies.json>)' % name)

    # 注册进 accounts.json
    accs = config.accounts()
    found = False
    for a in accs:
        if a['name'] == name:
            found = True
            if proxy is not None:
                a['proxy'] = proxy
            if cdp_port is not None:
                a['cdp_port'] = cdp_port
            break
    if not found:
        accs.append({
            'name': name,
            'enabled': True,
            'proxy': proxy,
            'cdp_port': cdp_port,
            'offset_minutes': 0
        })
    config.save_accounts(accs)
    config.log(name, '=== 登录引导完成 ===')
