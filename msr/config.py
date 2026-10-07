"""配置与数据目录管理。所有状态都相对 MSR_HOME(默认 ~/.msr)。"""
import json
import os
import time

HOME = None
ACCOUNTS_FILE = None


def init(home=None):
    global HOME, ACCOUNTS_FILE
    HOME = os.path.abspath(home or os.environ.get('MSR_HOME')
                           or os.path.expanduser('~/.msr'))
    ACCOUNTS_FILE = os.path.join(HOME, 'accounts.json')
    os.makedirs(HOME, exist_ok=True)


def accounts():
    """读账号注册表 [{name, enabled, offset_minutes}]。"""
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    with open(ACCOUNTS_FILE, encoding='utf-8') as f:
        return json.load(f)


def save_accounts(accs):
    with open(ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(accs, f, ensure_ascii=False, indent=1)


def profile(name, create=True):
    """账号 profile 目录: token/region/cookie/状态文件都在这里。"""
    p = os.path.join(HOME, 'profiles', name)
    if create:
        for sub in ('', 'logs', 'chrome'):
            os.makedirs(os.path.join(p, sub) if sub else p, exist_ok=True)
    return p


def pfile(name, fname):
    """账号 profile 内文件路径。"""
    return os.path.join(profile(name), fname)


def load_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def log(name, msg):
    """账号日志(同时打到 stdout)。"""
    line = '%s %s' % (time.strftime('%F %T'), msg)
    print(line, flush=True)
    try:
        d = os.path.join(profile(name), 'logs')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, '%s.log' % time.strftime('%F')), 'a') as f:
            f.write(line + '\n')
    except Exception:
        pass


def cmd_status():
    accs = accounts()
    if not accs:
        print('无账号。先运行: msr login <name>')
        return
    for a in accs:
        name = a['name']
        st = load_json(pfile(name, 'state.json'), {}) or {}
        rg = load_json(pfile(name, 'region.json'), {}) or {}
        print('%-12s region=%-6s level=%-10s 上次=%s 余额=%s' % (
            name, rg.get('country', '?'), (rg.get('levelInfo') or {}).get('activeLevel', '?'),
            st.get('last_run', '从未'), st.get('balance', '?')))
