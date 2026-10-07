"""report 子命令: 账号当日状态(对齐现网 bing_report 输出习惯)。"""
import time


def cmd_report(args):
    from msr import config
    accs = [a['name'] for a in config.accounts()]
    if args.name:
        accs = [args.name]
    for name in accs:
        st = config.load_json(config.pfile(name, 'state.json'), {}) or {}
        rg = config.load_json(config.pfile(name, 'region.json'), {}) or {}
        if not st:
            print('%s: 从未运行' % name)
            continue
        print('== %s ==' % name)
        print('  区域: %s/%s  余额: %s' % (rg.get('country'), rg.get('language'), st.get('balance')))
        print('  上次运行: %s' % st.get('last_run'))
        for r in st.get('last_results', []):
            print('  %s' % r)
