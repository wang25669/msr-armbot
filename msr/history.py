"""账号历史积分与 7 天得分概况管理系统。

记录每日起点积分、终点积分、得分增量以及完成的任务概要。
存储于 profile(name)/history.json，并支持从历史运行日志与快照中自动回填。
"""
import datetime
import glob
import json
import os
import re
import time


def _get_history_file(name):
    from msr import config
    return config.pfile(name, 'history.json')


def load_history(name):
    """加载指定账号的历史记录字典 {YYYY-MM-DD: {...}}。"""
    f = _get_history_file(name)
    if os.path.isfile(f):
        try:
            with open(f, 'r', encoding='utf-8') as fp:
                return json.load(fp)
        except Exception:
            pass
    return {}


def save_history(name, hist):
    """保存账号历史记录。"""
    from msr import config
    f = _get_history_file(name)
    config.save_json(f, hist)


def bootstrap_from_logs(name):
    """从磁盘已有日志与快照中自动扫描恢复历史得分数据。"""
    from msr import config
    data = load_history(name)
    modified = False

    # 1. 扫描项目根目录下历史日志文件夹 logs/2026-xx-xx
    repo_logs = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'logs'))
    if os.path.isdir(repo_logs):
        for p in sorted(glob.glob(os.path.join(repo_logs, '20*'))):
            d = os.path.basename(p)
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
                continue
            entry = data.setdefault(d, {})
            # 尝试读 bing_v2_balance.json
            b_file = os.path.join(p, 'bing_v2_balance.json')
            if os.path.isfile(b_file):
                try:
                    with open(b_file, 'r', encoding='utf-8') as fp:
                        b = json.load(fp)
                        if b.get('b0') and 'start' not in entry:
                            entry['start'] = int(b['b0'])
                            modified = True
                except Exception:
                    pass
            # 尝试读 stage_quick.log 中的起点记录
            q_file = os.path.join(p, 'stage_quick.log')
            if os.path.isfile(q_file):
                try:
                    with open(q_file, 'r', encoding='utf-8', errors='ignore') as fp:
                        content = fp.read()
                        m = re.search(r'起点已有记录:\s*(\d+)', content)
                        if m and 'start' not in entry:
                            entry['start'] = int(m.group(1))
                            modified = True
                except Exception:
                    pass

    # 2. 扫描账号自身 profile 下的 logs/*.log
    p_logs = os.path.join(config.profile(name), 'logs')
    if os.path.isdir(p_logs):
        for p in sorted(glob.glob(os.path.join(p_logs, '20*.log'))):
            d = os.path.basename(p).replace('.log', '')
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
                continue
            entry = data.setdefault(d, {})
            try:
                with open(p, 'r', encoding='utf-8', errors='ignore') as fp:
                    content = fp.read()
                # 提取初始起点 (例如 region=SG balance=11626)
                start_bals = [int(x) for x in re.findall(r'balance=(\d+)', content)]
                # 提取所有运行结束标记: === 运行结束 (余额: 12026, 今日增量: +400): ... ===
                run_ends = re.findall(r'运行结束 \(余额:\s*(\d+),\s*今日增量:\s*\+?(\d+)\)(?::\s*(.*?)\s*===)?', content)
                # 提取所有其他可能的余额标记
                all_bals = [int(x) for x in re.findall(r'(?:balance=|余额[=:]\s*)(\d+)', content)]

                first_b = start_bals[0] if start_bals else (all_bals[0] if all_bals else None)
                if first_b is not None:
                    if 'start' not in entry or not entry['start']:
                        entry['start'] = first_b
                        modified = True
                    else:
                        entry['start'] = min(entry['start'], first_b)

                if run_ends:
                    last_end_bal = int(run_ends[-1][0])
                    entry['end'] = last_end_bal
                    if 'start' in entry and entry['start']:
                        entry['gained'] = max(0, entry['end'] - entry['start'])
                    last_res_str = run_ends[-1][2] if len(run_ends[-1]) > 2 else ''
                    if last_res_str and 'results' not in entry:
                        entry['results'] = [r.strip() for r in last_res_str.split(' | ') if r.strip()]
                    modified = True
                elif all_bals:
                    last_b = all_bals[-1]
                    entry['end'] = last_b
                    if 'start' in entry and entry['start']:
                        entry['gained'] = max(0, entry['end'] - entry['start'])
                    modified = True
            except Exception:
                pass

    # 3. 读取当前 state.json 的最新积分
    st_f = config.pfile(name, 'state.json')
    if os.path.isfile(st_f):
        try:
            with open(st_f, 'r', encoding='utf-8') as fp:
                st = json.load(fp)
                cur_bal = st.get('balance')
                today_str = time.strftime('%Y-%m-%d')
                if cur_bal:
                    entry = data.setdefault(today_str, {})
                    entry['end'] = int(cur_bal)
                    if 'start' in entry and entry['start']:
                        entry['gained'] = max(0, entry['end'] - entry['start'])
                    else:
                        entry['start'] = int(cur_bal)
                        entry['gained'] = 0
                    entry['last_updated'] = st.get('last_run', time.strftime('%F %T'))
                    if st.get('last_results'):
                        entry['results'] = st.get('last_results')
                    modified = True
        except Exception:
            pass

    # 4. 连续日期边界补齐与校准：若前一天的 end 缺失或落后于后一天的 start
    sorted_dates = sorted(data.keys())
    for i in range(len(sorted_dates) - 1):
        cur_d = sorted_dates[i]
        nxt_d = sorted_dates[i + 1]
        cur_entry = data[cur_d]
        nxt_entry = data[nxt_d]
        nxt_start = nxt_entry.get('start')
        if nxt_start:
            if 'end' not in cur_entry or cur_entry['end'] < nxt_start:
                cur_entry['end'] = nxt_start
                if 'start' in cur_entry and cur_entry['start']:
                    cur_entry['gained'] = max(0, cur_entry['end'] - cur_entry['start'])
                modified = True

    if modified:
        save_history(name, data)
    return data


def record_daily_point(name, start_bal=None, end_bal=None, results=None, date_str=None):
    """记录或更新某天的积分统计数据。"""
    if not date_str:
        date_str = time.strftime('%Y-%m-%d')
    hist = load_history(name)
    rec = hist.get(date_str, {})

    if start_bal is not None:
        val = int(start_bal)
        if 'start' not in rec or not rec['start']:
            rec['start'] = val
        else:
            rec['start'] = min(rec['start'], val)

    if end_bal is not None:
        rec['end'] = int(end_bal)
        if 'start' not in rec or not rec['start']:
            rec['start'] = int(end_bal)
        rec['gained'] = max(0, rec['end'] - rec['start'])

    if results:
        rec['results'] = results
    rec['last_updated'] = time.strftime('%F %T')

    hist[date_str] = rec
    save_history(name, hist)
    return rec


def get_7day_history(name, current_balance=0):
    """计算最近 7 天的得分概况列表与累计得分。"""
    hist = bootstrap_from_logs(name)
    today = datetime.date.today()

    days_list = []
    total_gained = 0

    # 倒推计算近 7 天完整日期 (从 6 天前到今天)
    dates = [(today - datetime.timedelta(days=i)).isoformat() for i in range(6, -1, -1)]

    # 寻找最早的已知起点作为更早日期的基准
    earliest_known_start = None
    for d in dates:
        if d in hist and hist[d].get('start'):
            earliest_known_start = hist[d]['start']
            break
    if earliest_known_start is None:
        earliest_known_start = current_balance or 0

    running_balance = earliest_known_start
    for d in dates:
        short_date = d[5:] # 'MM-DD'
        is_today = (d == today.isoformat())
        if d in hist:
            item = hist[d]
            st_b = item.get('start', running_balance)
            ed_b = item.get('end', st_b)
            gained = item.get('gained', max(0, ed_b - st_b))
            running_balance = ed_b
            total_gained += gained
            status = '已完成' if gained > 0 or item.get('results') else ('今日待跑' if is_today else '未运行')
            days_list.append({
                'date': short_date,
                'full_date': d,
                'is_today': is_today,
                'start': st_b,
                'end': ed_b,
                'gained': gained,
                'status': status,
                'results': item.get('results', [])
            })
        else:
            days_list.append({
                'date': short_date,
                'full_date': d,
                'is_today': is_today,
                'start': running_balance,
                'end': running_balance,
                'gained': 0,
                'status': '今日待跑' if is_today else '未运行',
                'results': []
            })

    return {
        'total_gained': total_gained,
        'days': days_list
    }
