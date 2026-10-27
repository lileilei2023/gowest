#!/usr/bin/env python3
"""Display counters only. No quota enforcement, no subscription secrets logged."""
import fcntl
import json
import os
from pathlib import Path
import subprocess

ROOT = Path('/etc/la-vpn')
STATE = Path('/var/lib/la-vpn-deploy')


def header(rx, tx, total=0, expire=0):
    # Whole-NIC counters, deliberately not per-user traffic: a relay sends and
    # receives every proxied byte twice (client<->server and server<->site), so
    # upload+download is roughly 2x what clients used. That is the figure cloud
    # quotas are normally metered on, hence it is compared with total_bytes.
    # upload=server tx, download=server rx (server-side directions).
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
        new = header(
            usage['rx'], usage['tx'], s['traffic']['total_bytes'], s['traffic']['expire_epoch'])
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


if __name__ == '__main__':
    main()
