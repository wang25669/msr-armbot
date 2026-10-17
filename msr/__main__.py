#!/usr/bin/env python3
"""msr - Microsoft Rewards automation for ARM Linux.

Usage:
  msr login <name> [--cookies FILE] [--proxy URL] [--cdp-port PORT]  引导账号登录
  msr account <name> [--proxy URL] [--cdp-port PORT] [--enable/--disable]  查看或更新账号配置
  msr run [name|--all] [--dry-run] [--only PHASES]                   执行账号任务链
  msr report [name]                                                  账号当日报告
  msr status                                                         全部账号状态
"""
import argparse
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


def main():
    ap = argparse.ArgumentParser(prog='msr', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--home', default=None, help='数据目录(默认 ~/.msr)')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('login', help='引导账号登录')
    p.add_argument('name')
    p.add_argument('--cookies', help='浏览器导出的 cookies.json(可选)')
    p.add_argument('--proxy', help='独立代理出口(http://或socks5://)')
    p.add_argument('--cdp-port', type=int, help='外部 Chrome 调试端口(例如 9222)')

    p = sub.add_parser('account', help='查看或修改账号配置(代理、端口、开关)')
    p.add_argument('name')
    p.add_argument('--proxy', help='设置代理 (设为 none 清除)')
    p.add_argument('--cdp-port', type=int, help='设置 CDP 端口 (设为 0 清除)')
    p.add_argument('--enable', action='store_true', help='启用账号')
    p.add_argument('--disable', action='store_true', help='禁用账号')

    p = sub.add_parser('run', help='执行任务链')
    p.add_argument('name', nargs='?', default=None)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--only', default=None, help='只跑指定阶段(逗号分隔)')

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
    elif args.cmd == 'report':
        from msr import report
        report.cmd_report(args)
    elif args.cmd == 'status':
        config.cmd_status()


if __name__ == '__main__':
    main()
