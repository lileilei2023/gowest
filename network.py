"""Conservative tuning. Unknown/custom queue disciplines are left alone."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

PRIOMAP = [1, 2, 2, 2, 1, 2, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1]
FQ_HANDLE = '1af0:'
RESTORED_HANDLE = '1af1:'
STATE_PATH = Path('/var/lib/la-vpn-deploy/network.json')
ARCHIVES = Path('/var/lib/la-vpn-archives')

PARAMETERS = {'net.core.default_qdisc': 'fq', 'net.ipv4.tcp_congestion_control': 'bbr',
              'net.core.rmem_default': '4194304', 'net.core.wmem_default': '4194304',
              'net.core.rmem_max': '33554432', 'net.core.wmem_max': '33554432',
              'net.ipv4.tcp_mtu_probing': '1'}


def run(*args, check=True):
    p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                       start_new_session=True)
    if check and p.returncode:
        raise RuntimeError('Command failed: ' + args[0])
    return p.stdout.strip()


def save(path, value):
    p = Path(path)
    if p.is_symlink():
        raise RuntimeError('Refusing state symlink')
    fd, name = tempfile.mkstemp(prefix=p.name + '.', dir=p.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def identity(q):
    # refcnt/backlog/statistics are transient; options and handle are ownership checks.
    return {k: q.get(k) for k in ('kind', 'handle', 'options')}


def topology(dev):
    rows = json.loads(run('tc', '-j', 'qdisc', 'show', 'dev', dev) or '[]')
    classes = json.loads(run('tc', '-j', 'class', 'show', 'dev', dev) or '[]')
    filters = json.loads(run('tc', '-j', 'filter', 'show', 'dev', dev, 'root') or '[]')
    roots = [q for q in rows if q.get('root')]
    return {'root': roots[0] if len(roots) == 1 else {},
            'isolated': len(rows) == 1 and len(roots) == 1 and not classes and not filters}


def interface():
    routes = json.loads(run('ip', '-j', '-4', 'route', 'get', '1.1.1.1'))
    dev = routes[0]['dev']
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', dev) or dev == 'lo':
        raise RuntimeError('Cannot identify a safe default interface')
    return dev


def root_qdisc(dev):
    rows = json.loads(run('tc', '-j', 'qdisc', 'show', 'dev', dev))
    roots = [x for x in rows if x.get('root')]
    return roots[0] if len(roots) == 1 else {}


def safe_pfifofast(q, allow_nonzero=False):
    # Do not flatten mq, HTB, CAKE, fq_codel or an administratively shaped queue.
    opts = q.get('options', {})
    return (q.get('kind') == 'pfifo_fast' and
            bool(re.fullmatch(r'[0-9a-fA-F]{1,4}:', q.get('handle', ''))) and
            (q['handle'] == '0:' or allow_nonzero) and
            opts.get('bands', 3) == 3 and
            opts.get('priomap', PRIOMAP) == PRIOMAP and
            opts.get('multiqueue', False) is False and
            not set(opts) - {'bands', 'priomap', 'multiqueue'})


def rollback_receipt(dev, q):
    """Only trust an exact root-owned receipt from this boot, not a guessed handle."""
    if not safe_pfifofast(q, allow_nonzero=True):
        return False
    for path in ARCHIVES.glob('rollback-*/var--lib--la-vpn-deploy/network.json'):
        try:
            if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
                continue
            old = json.loads(path.read_text())
            if (old.get('interface') == dev and old.get('restored_boot_id') == boot_id()
                    and identity(old.get('qdisc_restored', {})) == identity(q)):
                return True
        except (OSError, ValueError, TypeError):
            continue
    return False


def set_fq(state_path, state, before):
    actual = topology(state['interface'])
    if not actual['isolated'] or identity(actual['root']) != identity(before):
        raise RuntimeError('Queue/topology changed before FQ application')
    # Write deterministic ownership and intent before mutation (crash recoverable).
    state.update(qdisc_changed=True, qdisc_after=None, fq_handle=FQ_HANDLE,
                 fq_boot_id=boot_id(), qdisc_result='applying')
    save(state_path, state)
    run('tc', 'qdisc', 'replace', 'dev', state['interface'], 'root', 'handle', FQ_HANDLE, 'fq')
    after = topology(state['interface'])
    q = after['root']
    if not after['isolated'] or q.get('kind') != 'fq' or q.get('handle') != FQ_HANDLE:
        raise RuntimeError('FQ did not take effect; snapshot retained for recovery')
    state.update(qdisc_after=q, qdisc_result='applied')
    save(state_path, state)
    return state


def apply(state_path):
    if Path(state_path).exists():
        raise RuntimeError('Network snapshot already exists; refusing repeat tuning')
    dev = interface()
    before = {k: run('sysctl', '-n', k) for k in PARAMETERS}
    chosen = dict(PARAMETERS)
    for key in ('net.core.rmem_default', 'net.core.wmem_default', 'net.core.rmem_max', 'net.core.wmem_max'):
        chosen[key] = str(max(int(chosen[key]), int(before[key])))
    if 'bbr' not in run('sysctl', '-n', 'net.ipv4.tcp_available_congestion_control').split():
        run('modprobe', 'tcp_bbr', check=False)
    if 'bbr' not in run('sysctl', '-n', 'net.ipv4.tcp_available_congestion_control').split():
        chosen.pop('net.ipv4.tcp_congestion_control')
    layout = topology(dev)
    q = layout['root']
    approved = rollback_receipt(dev, q) if q.get('handle') != '0:' else False
    state = {'interface': dev, 'before': before, 'applied': {}, 'qdisc_before': q,
             'qdisc_changed': False, 'qdisc_after': None, 'fifo_approved': approved,
             'qdisc_result': 'already-fq' if q.get('kind') == 'fq' else 'skipped-custom-or-unowned'}
    save(state_path, state)
    for key, value in chosen.items():
        # Record intent before mutation, so a process interruption is recoverable.
        state['applied'][key] = value
        save(state_path, state)
        run('sysctl', '-w', key + '=' + value)
    if layout['isolated'] and safe_pfifofast(q, allow_nonzero=approved):
        set_fq(state_path, state, q)
    return state


def check_restore(state):
    for key, target in state['applied'].items():
        value = run('sysctl', '-n', key)
        if value not in (target, state['before'][key]):
            raise RuntimeError('Network parameter changed outside this installer: ' + key)
    if state['qdisc_changed']:
        if not safe_pfifofast(state['qdisc_before'], allow_nonzero=True):
            raise RuntimeError('Cannot safely restore non-default FIFO')
        layout = topology(state['interface'])
        actual = layout['root']
        if not layout['isolated']:
            raise RuntimeError('Queue topology changed outside installer')
        expected = state.get('qdisc_after')
        if any(identity(actual) == identity(q) for q in
               (state.get('qdisc_before', {}), state.get('qdisc_restored', {}),
                state.get('qdisc_restore_intent', {}))):
            return
        if actual.get('kind') != 'fq' or (expected and identity(actual) != identity(expected)):
            raise RuntimeError('Queue changed outside installer; manual review required')
        if not expected and actual.get('handle') != state.get('fq_handle'):
            raise RuntimeError('FQ ownership unknown after interrupted application')


def restore(state_path):
    state = json.loads(Path(state_path).read_text())
    check_restore(state)
    for key in state['applied']:
        run('sysctl', '-w', key + '=' + state['before'][key])
    if state['qdisc_changed']:
        before = state['qdisc_before']
        if not safe_pfifofast(before, allow_nonzero=True):
            raise RuntimeError('Cannot safely restore non-default FIFO')
        actual = root_qdisc(state['interface'])
        if identity(actual) not in (identity(before), identity(state.get('qdisc_restored', {})),
                                   identity(state.get('qdisc_restore_intent', {}))):
            # Kernel-assigned 0: cannot be requested via tc. Record a stable receipt
            # for its default-options replacement; preserve explicit original handles.
            handle = before['handle'] if before['handle'] != '0:' else RESTORED_HANDLE
            state['qdisc_restore_intent'] = dict(before, handle=handle)
            save(state_path, state)
            run('tc', 'qdisc', 'replace', 'dev', state['interface'], 'root', 'handle', handle, 'pfifo_fast')
            actual = root_qdisc(state['interface'])
            if actual.get('handle') != handle or not safe_pfifofast(actual, allow_nonzero=True):
                raise RuntimeError('FIFO restoration verification failed')
        state.update(qdisc_restored=actual, restored_boot_id=boot_id(), qdisc_result='restored')
        save(state_path, state)


def repair_fq(state_path, adopt_default_fifo=False):
    """Explicit repair of a skipped legacy FIFO; no sysctls/subscriptions changed."""
    state = json.loads(Path(state_path).read_text())
    layout = topology(state['interface'])
    q = layout['root']
    if state.get('qdisc_changed'):
        verify_health(state_path)
        return state
    if q.get('kind') == 'fq':
        return state  # Existing FQ belongs to its administrator; do not take ownership.
    if not layout['isolated'] or identity(q) != identity(state['qdisc_before']):
        raise RuntimeError('Queue changed since deployment or has classes/filters; refusing repair')
    approved = adopt_default_fifo or rollback_receipt(state['interface'], q)
    if not safe_pfifofast(q, allow_nonzero=approved):
        raise RuntimeError('Unowned/non-default FIFO; review then explicitly --adopt-default-fifo')
    state['fifo_approved'] = bool(approved)
    return set_fq(state_path, state, q)


def boot_fq(dev, state_path=STATE_PATH):
    state = json.loads(Path(state_path).read_text())
    if state['interface'] != dev:
        raise RuntimeError('Interface does not match network snapshot')
    layout = topology(dev)
    q = layout['root']
    if q.get('kind') == 'fq':
        # On a new boot the kernel may create default FQ (handle 0:) before
        # this unit runs. Accept only matching options for a previously owned
        # queue; never adopt changed/custom FQ or hide same-boot drift.
        expected = state.get('qdisc_after')
        if (state.get('qdisc_changed') and expected and layout['isolated']
                and state.get('fq_boot_id') != boot_id() and q.get('handle') == '0:'
                and q.get('options') == expected.get('options')):
            state.update(qdisc_after=q, fq_boot_id=boot_id(), qdisc_result='kernel-fq-after-reboot')
            save(state_path, state)
        return health(state_path)
    approved = state.get('fifo_approved', False) and identity(q) == identity(state['qdisc_before'])
    if layout['isolated'] and safe_pfifofast(q, allow_nonzero=approved):
        # A previously protected custom baseline must never be silently adopted.
        if safe_pfifofast(state['qdisc_before'], allow_nonzero=state.get('fifo_approved', False)):
            set_fq(state_path, state, q)
    return health(state_path)


def health(state_path):
    state = json.loads(Path(state_path).read_text())
    layout = topology(state['interface'])
    q = layout['root']
    cc = run('sysctl', '-n', 'net.ipv4.tcp_congestion_control', check=False) or 'unavailable'
    default = run('sysctl', '-n', 'net.core.default_qdisc', check=False) or 'unavailable'
    fq = q.get('kind') == 'fq'
    warnings = []
    if not fq:
        warnings.append('Actual root is not FQ; sysctl/default_qdisc or active service does not prove FQ')
    if cc != 'bbr':
        warnings.append('Linux TCP BBR is not active (Hy2 has independent QUIC congestion control)')
    if default == 'unavailable':
        warnings.append('default_qdisc sysctl unavailable in this network namespace; actual tc state reported')
    expected = state.get('qdisc_after')
    owned_ok = (fq and layout['isolated'] and
                (identity(q) == identity(expected) if expected else q.get('handle') == state.get('fq_handle')))
    if state.get('qdisc_changed') and not owned_ok:
        warnings.append('Managed FQ missing or modified; repair requires review')
    return {'interface': state['interface'], 'actual_root_qdisc': identity(q),
            'isolated_root': layout['isolated'], 'fq_effective': fq,
            'managed_fq_matches': owned_ok if state.get('qdisc_changed') else None,
            'tcp_congestion_control': cc, 'default_qdisc': default,
            'last_action': state.get('qdisc_result', 'legacy-snapshot'), 'warnings': warnings}


def verify_health(state_path):
    report = health(state_path)
    if report['managed_fq_matches'] is False:
        raise RuntimeError('Managed FQ verification failed; inspect network status')
    return report


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('interface')
    p.add_argument('--state', default=str(STATE_PATH))
    args = p.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', args.interface):
        p.error('Invalid interface')
    print(json.dumps(boot_fq(args.interface, args.state), indent=2))


if __name__ == '__main__':
    main()
