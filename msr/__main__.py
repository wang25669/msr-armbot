#!/usr/bin/env python3
"""msr - Microsoft Rewards automation for ARM Linux & Containers.

Usage:
  msr login <name> [--method credential|cookies|interactive] [--username EMAIL] [--password PWD] [--cookies FILE] [--proxy URL]  引导账号登录
  msr account <name> [--proxy URL] [--enable/--disable]                 查看或更新账号独立配置
  msr run [name|--all] [--dry-run] [--only PHASES] [--fast]            执行账号任务链
  msr settings [--set key=val] [--get key]                             查看或调整全局参数配置
  msr notify-test [title] [text]                                       测试消息通知通道 (Server酱/TG)
  msr web [--host 0.0.0.0] [--port 8080]                               启动内置 Web 管理控制台
  msr report [name]                                                    账号当日报告
  msr status                                                           全部账号状态
"""
import argparse
import json
import sys


def cmd_account(args, config):
    name = args.name
    accs = config.accounts()
    target = next((a for a in accs if a['name'] == name), None)
    if not target:
        if args.proxy or args.cdp_port is not None or args.enable or args.disable:
            target = {'name': name, 'enabled': True, 'offset_minutes': 0}
            accs.append(target)
        else:
            print('账号不存在: %s' % name)
            return 1

    changed = False
    if args.proxy is not None:
        target['proxy'] = args.proxy if args.proxy != 'none' else None
        changed = True
    if args.cdp_port is not None:
        target['cdp_port'] = int(args.cdp_port) if args.cdp_port > 0 else None
        changed = True
    if args.enable:
        target['enabled'] = True
        changed = True
    elif args.disable:
        target['enabled'] = False
        changed = True

    if changed:
        config.save_accounts(accs)
        print('已更新账号配置: %s -> %s' % (name, target))
    else:
        print('账号配置 [%s]:' % name)
        for k, v in target.items():
            print('  %-15s: %s' % (k, v))
    return 0


def cmd_settings(args):
    from msr import settings
    if args.set:
        pair = args.set
        if '=' not in pair:
            print('格式错误，请使用: --set key=value (例如: --set search.dwell_min_seconds=120)')
            return 1
        k, v = pair.split('=', 1)
        settings.set_val(k.strip(), v.strip())
        print(f'已更新配置: {k.strip()} = {v.strip()}')
    elif args.get:
        val = settings.get(args.get.strip())
        print(f'{args.get.strip()} = {val}')
    else:
        st = settings.load_settings()
        print('=== 当前全局配置 (~/.msr/settings.json) ===')
        print(json.dumps(st, indent=2, ensure_ascii=False))
    return 0


def cmd_notify_test(args):
    from msr import notify
    title = args.title or 'MSR-ArmBot 推送连通性测试'
    text = args.text or '这是一条来自 MSR-ArmBot 的测试通知。如果您看到此消息，说明 Server酱 / Webhook 通道配置正确！'
    print(f'正在触发测试推送: {title} ...')
    notify.notify(title, text, level='info')


def main():
    ap = argparse.ArgumentParser(prog='msr', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--home', default=None, help='数据目录(默认 ~/.msr)')
    sub = ap.add_subparsers(dest='cmd', required=True)

    # login
    p = sub.add_parser('login', help='引导账号登录 (支持多种策略)')
    p.add_argument('name')
    p.add_argument('--method', choices=['credential', 'cookies', 'interactive'],
                   default='credential', help='登录方式: credential(账号密码推荐)|cookies(导入)|interactive(交互式)')
    p.add_argument('--cookies', help='浏览器导出的 cookies.json 文件路径')
    p.add_argument('--username', help='微软账号邮箱 (用于 credential 登录)')
    p.add_argument('--password', help='微软账号密码 (用于 credential 登录)')
    p.add_argument('--proxy', help='独立出口代理 (http://或socks5://)')
    p.add_argument('--cdp-port', type=int, help='可选外部 Chrome 调试端口')

    # account
    p = sub.add_parser('account', help='查看或修改账号配置(代理、开关)')
    p.add_argument('name')
    p.add_argument('--proxy', help='设置代理 (设为 none 清除)')
    p.add_argument('--cdp-port', type=int, help='设置 CDP 端口 (设为 0 清除)')
    p.add_argument('--enable', action='store_true', help='启用账号')
    p.add_argument('--disable', action='store_true', help='禁用账号')

    # run
    p = sub.add_parser('run', help='执行任务链')
    p.add_argument('name', nargs='?', default=None)
    p.add_argument('--all', action='store_true', help='执行全部有效账号')
    p.add_argument('--dry-run', action='store_true', help='仅打印排期计划，不执行')
    p.add_argument('--only', default=None, help='只跑指定阶段 (例如: dailyset,moreactivities,search)')
    p.add_argument('--fast', action='store_true', help='极速调试模式 (所有停留等待压缩至 3-6 秒)')

    # settings
    p = sub.add_parser('settings', help='查看或调整集中风控与节奏参数')
    p.add_argument('--set', help='修改配置项 (例如: search.dwell_min_seconds=120)')
    p.add_argument('--get', help='查询单个配置项 (例如: search.stall_max_retries)')

    # notify-test
    p = sub.add_parser('notify-test', help='测试消息通知推送通道')
    p.add_argument('title', nargs='?', default=None)
    p.add_argument('text', nargs='?', default=None)

    # web
    p = sub.add_parser('web', help='启动内置 Web 控制台')
    p.add_argument('--host', default='0.0.0.0', help='监听地址 (默认 0.0.0.0)')
    p.add_argument('--port', type=int, default=8080, help='监听端口 (默认 8080)')

    # report & status
    p = sub.add_parser('report', help='账号当日报告')
    p.add_argument('name', nargs='?', default=None)

    sub.add_parser('status', help='全部账号状态')

    args = ap.parse_args()

    from msr import config
    config.init(args.home)

    if args.cmd == 'login':
        from msr import auth
        auth.cmd_login(args)
    elif args.cmd == 'account':
        cmd_account(args, config)
    elif args.cmd == 'run':
        from msr import runner
        runner.cmd_run(args)
    elif args.cmd == 'settings':
        cmd_settings(args)
    elif args.cmd == 'notify-test':
        cmd_notify_test(args)
    elif args.cmd == 'web':
        from msr import web
        web.start_server(host=args.host, port=args.port)
    elif args.cmd == 'report':
        from msr import report
        report.cmd_report(args)
    elif args.cmd == 'status':
        config.cmd_status()


if __name__ == '__main__':
    main()
