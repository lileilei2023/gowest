"""Per-URL traffic counters and pauses in one nftables table owned by gowest.

Each subscription URL has its own port, so client upload is traffic arriving
at that port and client download is traffic leaving it. The table only counts,
except for a drop rule on the port of a URL that is over its quota or expired.
It never touches other tables, so UFW and administrator rules are unaffected.
Counters restart from zero on reboot or rebuild; the stored totals add the
difference since the last read instead of trusting the raw counter.
"""
import datetime
import json
import os
from pathlib import Path
import subprocess
import time

TABLE = 'la_vpn_acct'
STATE = Path('/var/lib/la-vpn-deploy')


def nft(*args, data=None, check=True):
    p = subprocess.run(['nft', *args], input=data, text=True, capture_output=True, timeout=30)
    if check and p.returncode:
        raise RuntimeError('nft failed: ' + p.stderr.strip()[:300])
    return p


def port_group(value):
    """A URL's ports: its main port first, then the extra protocols' port."""
    return [value] if isinstance(value, int) else list(value)


def ruleset(ports, blocked=()):
    """ports: {name: port or [main, alt]}; counters are named after the main port.

    Hopped Hy2 traffic is counted under the main port: the redirect has already
    happened at prerouting, before this input hook, and replies leave from the
    main port before the kernel restores the hopped one. A paused URL's ports
    are dropped before they are counted."""
    groups = sorted(((name, port_group(p)) for name, p in ports.items()), key=lambda x: x[1][0])
    match = lambda group: str(group[0]) if len(group) == 1 else '{ ' + ', '.join(map(str, group)) + ' }'
    lines = [f'table inet {TABLE} {{']
    for _, group in groups:
        lines += [f'    counter p{group[0]}_in {{ }}', f'    counter p{group[0]}_out {{ }}']
    lines.append('    chain input {\n        type filter hook input priority -150; policy accept;')
    for name, group in groups:
        if name in blocked:
            lines.append(f'        meta l4proto {{ tcp, udp }} th dport {match(group)} drop')
        lines.append(f'        meta l4proto {{ tcp, udp }} th dport {match(group)} counter name "p{group[0]}_in"')
    lines.append('    }\n    chain output {\n        type filter hook output priority -150; policy accept;')
    for _, group in groups:
        lines.append(f'        meta l4proto {{ tcp, udp }} th sport {match(group)} counter name "p{group[0]}_out"')
    lines.append('    }\n}')
    return '\n'.join(lines) + '\n'


def exists():
    return nft('list', 'table', 'inet', TABLE, check=False).returncode == 0


def counters():
    """{port: [in_bytes, out_bytes]} from the live table; {} when it is absent."""
    if not exists():
        return {}
    data = json.loads(nft('-j', 'list', 'counters', 'table', 'inet', TABLE).stdout)
    result = {}
    for item in data.get('nftables', []):
        counter = item.get('counter')
        if not counter or counter.get('table') != TABLE:
            continue
        name = counter['name']
        port, direction = int(name[1:].split('_')[0]), name.rsplit('_', 1)[1]
        result.setdefault(port, [0, 0])[0 if direction == 'in' else 1] = int(counter['bytes'])
    return result


def install(text):
    """Replace the table atomically: create-if-missing, delete, define, in one transaction."""
    nft('-f', '-', data=f'table inet {TABLE}\ndelete table inet {TABLE}\n' + text)


def remove():
    if exists():
        nft('delete', 'table', 'inet', TABLE)


def month_now():
    return time.strftime('%Y-%m')


def expire_epoch(date):
    """'2026-12-31' -> the local midnight that ends that day; '' -> 0."""
    if not date:
        return 0
    day = datetime.date.fromisoformat(date) + datetime.timedelta(days=1)
    return int(time.mktime(day.timetuple()))


def blocked_reason(name, used, s, now):
    quota = s.get('user_quota', {}).get(name, 0)
    if quota and used >= quota:
        return 'quota'
    expire = expire_epoch(s.get('user_expire', {}).get(name, ''))
    if expire and now >= expire:
        return 'expired'
    return ''


def sync(s, ports, path=None, now=None, live=None, apply=None):
    """Fold live counters into the monthly totals, then make the table match.

    Returns the record that is also saved: per user used bytes and pause reason.
    `live`/`apply` are injectable for tests; by default they talk to nftables."""
    path = Path(path or STATE / 'user-traffic.json')
    now = time.time() if now is None else now
    month = time.strftime('%Y-%m', time.localtime(now))
    live = counters() if live is None else live
    apply = apply or install
    old = json.loads(path.read_text()) if path.is_file() else {}
    fresh = old.get('month') != month
    users = {}
    for name, value in ports.items():
        port = port_group(value)[0]
        prev = old.get('users', {}).get(name, {})
        last = prev.get('last', [0, 0]) if prev.get('port') == port else [0, 0]
        current = live.get(port, [0, 0])
        # A counter below its last reading was recreated (reboot or rebuild).
        delta = [c - l if c >= l else c for c, l in zip(current, last)]
        # A new month starts from zero but still measures from the last reading.
        upload = (0 if fresh else prev.get('upload', 0)) + delta[0]
        download = (0 if fresh else prev.get('download', 0)) + delta[1]
        users[name] = {'port': port, 'upload': upload, 'download': download, 'last': current}
    for name, row in users.items():
        row['paused'] = blocked_reason(name, row['upload'] + row['download'], s, now)
    blocked = {name for name, row in users.items() if row['paused']}
    spec = {'ports': ports, 'blocked': sorted(blocked)}
    if not live or old.get('spec') != spec:
        apply(ruleset(ports, blocked))
        for row in users.values():
            row['last'] = [0, 0]
    record = {'month': month, 'spec': spec, 'users': users, 'updated': int(now)}
    tmp = path.with_suffix('.new')
    tmp.write_text(json.dumps(record))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return record
