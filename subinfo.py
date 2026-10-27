#!/usr/bin/env python3
"""Monthly traffic display and optional quota pause; no subscription secrets logged."""
import fcntl
import json
import os
from pathlib import Path
import subprocess

ROOT = Path('/etc/la-vpn')
STATE = Path('/var/lib/la-vpn-deploy')


def used_bytes(rx, tx, metering='both'):
    """What the cloud plan counts: rx+tx (conservative) or outbound only."""
    return int(tx) if metering == 'outbound' else int(rx) + int(tx)


def header(rx, tx, total=0, expire=0, metering='both'):
    # Whole-NIC counters, deliberately not per-user traffic: a relay sends and
    # receives every proxied byte twice (client<->server and server<->site), so
    # upload+download is roughly 2x what clients used. That is the figure cloud
    # quotas are normally metered on, hence it is compared with total_bytes.
    # upload=server tx, download=server rx (server-side directions). Plans that
    # bill only outbound traffic report rx as 0 so clients show the billed figure.
    if metering == 'outbound':
        rx = 0
    values = [f'upload={int(tx)}', f'download={int(rx)}']
    if total:
        values.append(f'total={int(total)}')
    if expire:
        values.append(f'expire={int(expire)}')
    return 'add_header subscription-userinfo "' + '; '.join(values) + '" always;\n'


def server_usage(s):
    """(rx, tx, month) of the whole NIC from vnStat, or None before its DB is ready."""
    net = json.loads((STATE / 'network.json').read_text())
    result = subprocess.run(['vnstat', '-i', net['interface'], '--json', 'm', '1'],
                            check=True, capture_output=True, text=True, timeout=20)
    rows = json.loads(result.stdout)['interfaces'][0]['traffic'].get('month', [])
    if not rows:
        return None  # Do not publish invented zero usage when the DB isn't ready.
    usage = rows[-1]
    return usage['rx'], usage['tx'], '{year:04d}-{month:02d}'.format(**usage['date'])


def user_headers(s, record, server):
    """name -> header. A URL with its own quota shows its own usage; others
    show the server's figures. A URL's own expiry wins over the server's."""
    import accounting
    result = {}
    for name in s['users']:
        row = record['users'][name]
        quota = s.get('user_quota', {}).get(name, 0)
        expire = accounting.expire_epoch(s.get('user_expire', {}).get(name, '')) or s['traffic']['expire_epoch']
        if quota:
            # Client view: upload = into the server's port, download = out of it.
            values = [f"upload={row['upload']}", f"download={row['download']}", f'total={quota}']
            if expire:
                values.append(f'expire={expire}')
            result[name] = 'add_header subscription-userinfo "' + '; '.join(values) + '" always;\n'
        elif server:
            result[name] = header(server[0], server[1], s['traffic']['total_bytes'], expire,
                                  s['traffic'].get('metering', 'both'))
    return result


def publish(files):
    """Write changed header files, validate Nginx, reload; restore all on failure."""
    old = {path: path.read_text() if path.is_file() else None for path in files}
    changed = {path: text for path, text in files.items() if old[path] != text}
    if not changed:
        return
    for path, text in changed.items():
        path.parent.mkdir(mode=0o755, exist_ok=True)
        tmp = path.with_name(path.name + '.new')
        tmp.write_text(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    test = subprocess.run(['nginx', '-t', '-c', str(ROOT / 'nginx.conf')], capture_output=True)
    if test.returncode:
        for path in changed:
            if old[path] is None:
                path.unlink()
            else:
                path.write_text(old[path])
        raise RuntimeError('Nginx validation failed; statistics header restored')
    subprocess.run(['systemctl', 'reload', 'la-vpn-subscription.service'], check=True, timeout=20)


def main():
    with (STATE / 'subinfo.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if json.loads((STATE / 'state.json').read_text())['status'] != 'installed':
            return
        s = json.loads((ROOT / 'settings.json').read_text())
        s.setdefault('user_ports', {})
        record = None
        if s.get('users'):
            import accounting
            import render
            s = render.settings(s)
            layout = render.port_layout(s)
            ports = {name: [x['main']] + ([x['alt']] if s['extra_protocols'] else [])
                     for name, x in layout.items()}
            try:
                record = accounting.sync(s, ports)
            except (OSError, RuntimeError) as error:
                # Without nftables only per-URL figures are lost; keep the server header.
                print('Per-URL accounting unavailable: ' + str(error)[:200])
        server = server_usage(s)
        files = {}
        if server:
            traffic = s['traffic']
            metering = traffic.get('metering', 'both')
            enforce(traffic, used_bytes(server[0], server[1], metering), server[2])
            files[ROOT / 'subinfo.conf'] = header(server[0], server[1], traffic['total_bytes'],
                                                  traffic['expire_epoch'], metering)
        if record:
            for name, text in user_headers(s, record, server).items():
                files[ROOT / 'subinfo.d' / (name + '.conf')] = text
        publish(files)


def enforce(traffic, used, month, systemctl=None):
    """Record usage; with on_exceed=stop pause the proxy at the quota.

    Only a pause made here is undone here (new month or a raised quota), so an
    administrator's own manual stop is never reversed by this timer."""
    systemctl = systemctl or (lambda *a: subprocess.run(['systemctl', *a], check=True, timeout=30))
    path = STATE / 'traffic.json'
    previous = json.loads(path.read_text()) if path.is_file() else {}
    quota = traffic['total_bytes']
    over = bool(quota) and used >= quota
    paused = previous.get('paused_month')
    if over and traffic.get('on_exceed') == 'stop' and not paused:
        systemctl('stop', 'la-vpn.service')
        paused = month
    elif paused and not (over and traffic.get('on_exceed') == 'stop' and paused == month):
        systemctl('start', 'la-vpn.service')
        paused = None
    record = {'month': month, 'used_bytes': used, 'quota_bytes': quota,
              'percent': round(used * 100 / quota, 1) if quota else None,
              'metering': traffic.get('metering', 'both'), 'on_exceed': traffic.get('on_exceed', 'warn'),
              'paused_month': paused}
    tmp = path.with_suffix('.new')
    tmp.write_text(json.dumps(record))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return record


if __name__ == '__main__':
    main()
