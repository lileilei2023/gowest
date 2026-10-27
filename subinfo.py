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


def main():
    with (STATE / 'subinfo.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if json.loads((STATE / 'state.json').read_text())['status'] != 'installed':
            return
        s = json.loads((ROOT / 'settings.json').read_text())
        net = json.loads((STATE / 'network.json').read_text())
        result = subprocess.run(['vnstat', '-i', net['interface'], '--json', 'm', '1'],
                                check=True, capture_output=True, text=True, timeout=20)
        rows = json.loads(result.stdout)['interfaces'][0]['traffic'].get('month', [])
        if not rows:
            return  # Do not publish invented zero usage when the DB isn't ready.
        usage = rows[-1]
        traffic = s['traffic']
        metering = traffic.get('metering', 'both')
        month = '{year:04d}-{month:02d}'.format(**usage['date'])
        used = used_bytes(usage['rx'], usage['tx'], metering)
        enforce(traffic, used, month)
        new = header(usage['rx'], usage['tx'], traffic['total_bytes'], traffic['expire_epoch'], metering)
        target = ROOT / 'subinfo.conf'
        old = target.read_text()
        if new == old:
            return
        tmp = ROOT / 'subinfo.conf.new'
        tmp.write_text(new)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
        test = subprocess.run(['nginx', '-t', '-c', str(ROOT / 'nginx.conf')], capture_output=True)
        if test.returncode:
            tmp.write_text(old)
            os.replace(tmp, target)
            raise RuntimeError('Nginx validation failed; statistics header restored')
        subprocess.run(['systemctl', 'reload', 'la-vpn-subscription.service'], check=True, timeout=20)


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
