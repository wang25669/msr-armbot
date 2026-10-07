#!/usr/bin/env python3
"""msr - Microsoft Rewards automation for ARM Linux.

Usage:
  msr login <name> [--cookies FILE]   引导账号登录(device-code, 可选导入浏览器 cookie)
  msr run [name|--all] [--dry-run]    执行账号任务链(缺省=accounts.json 全部串行)
  msr report [name]                   账号当日报告
  msr status                          全部账号状态
"""
import argparse
import sys


def main():
    ap = argparse.ArgumentParser(prog='msr', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--home', default=None, help='数据目录(默认 ~/.msr)')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('login', help='引导账号登录')
    p.add_argument('name')
    p.add_argument('--cookies', help='浏览器导出的 cookies.json(可选)')

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
