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


init()


def accounts():
    """读账号注册表 [{name, enabled, offset_minutes}]。"""
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    with open(ACCOUNTS_FILE, encoding='utf-8') as f:
        return json.load(f)


_ACCOUNTS_LOCK = None

def _get_accounts_lock():
    global _ACCOUNTS_LOCK
    if _ACCOUNTS_LOCK is None:
        import threading
        _ACCOUNTS_LOCK = threading.Lock()
    return _ACCOUNTS_LOCK

def save_accounts(accs):
    """原子化安全保存账号配置，防止多任务并发读写损坏。"""
    with _get_accounts_lock():
        save_json(ACCOUNTS_FILE, accs)


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
    """账号日志(同时打到 stdout 与本地落盘)。统一格式: [HH:MM:SS] [账号] 消息内容"""
    ts = time.strftime('%H:%M:%S')
    tag = f'[{name}]' if name else '[系统]'
    line = f'[{ts}] {tag} {msg}'
    print(line, flush=True)
    try:
        d = os.path.join(profile(name), 'logs')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, '%s.log' % time.strftime('%F')), 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def get_account(name):
    """根据名字获取单个账号配置字典。"""
    for a in accounts():
        if a.get('name') == name:
            return a
    return None


def credential_file(name):
    """账号专属凭据文件路径 (仅本地安全保存)。"""
    return pfile(name, 'credential.json')


def get_credential(name):
    """读取账号保存的 MSR 账号密码凭据。"""
    return load_json(credential_file(name), None)


def save_credential(name, username, password):
    """安全持久化账号密码凭据并设置 0600 文件权限，同时同步 accounts.json 的 username。"""
    cf = credential_file(name)
    save_json(cf, {
        'username': username,
        'password': password,
        'updated_at': time.strftime('%F %T')
    })
    try:
        os.chmod(cf, 0o600)
    except Exception:
        pass

    accs = accounts()
    changed = False
    for a in accs:
        if a.get('name') == name:
            if a.get('username') != username:
                a['username'] = username
                changed = True
            break
    if changed:
        save_accounts(accs)


def delete_credential(name):
    """删除保存的账号密码凭据。"""
    cf = credential_file(name)
    if os.path.exists(cf):
        try:
            os.remove(cf)
        except Exception:
            pass
    accs = accounts()
    changed = False
    for a in accs:
        if a.get('name') == name:
            if 'username' in a:
                del a['username']
                changed = True
            break
    if changed:
        save_accounts(accs)


def delete_profile(name):
    """彻底清理账号目录，包括 chrome profile、cookie、token、指纹与凭据，防止残留数据。"""
    import shutil
    p = os.path.join(HOME, 'profiles', name)
    if os.path.exists(p):
        try:
            shutil.rmtree(p)
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
        proxy = a.get('proxy') or '直连'
        print('%-12s region=%-4s proxy=%-16s level=%-10s 上次=%s 余额=%s' % (
            name, rg.get('country', '?'), proxy[:16], (rg.get('levelInfo') or {}).get('activeLevel', '?'),
            st.get('last_run', '从未'), st.get('balance', '?')))

