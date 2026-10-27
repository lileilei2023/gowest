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
SHARE_HANDLE = '1af2:'
SHARE_MODULES = ('sch_htb', 'sch_fq_codel', 'cls_u32')
QUANTUM = '70000'  # > 64 KiB GSO; HTB accepts up to 200000
# Kernel-created defaults only (handle 0:). Deleting such a root lets the kernel
# recreate exactly this default, which is what makes the share tree reversible.
DEFAULT_ROOTS = {'pfifo_fast', 'fq_codel', 'fq', 'mq', 'noqueue'}
DEFAULT_CHILDREN = {'pfifo_fast', 'fq_codel', 'fq'}
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
    try:
        classes = json.loads(run('tc', '-j', 'class', 'show', 'dev', dev) or '[]')
    except ValueError:
        # iproute2 prints plain text (HTB) or malformed JSON (CAKE) for classes;
        # unknown classes are treated as present, never as an isolated root.
        classes = [{'unparsed': True}]
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


def pristine(dev):
    """Root and every child are untouched kernel defaults, with no filters."""
    rows = json.loads(run('tc', '-j', 'qdisc', 'show', 'dev', dev) or '[]')
    filters = json.loads(run('tc', '-j', 'filter', 'show', 'dev', dev, 'root') or '[]')
    roots = [q for q in rows if q.get('root')]
    if len(roots) != 1 or filters or any(q.get('handle') != '0:' for q in rows):
        return None
    if roots[0].get('kind') not in DEFAULT_ROOTS:
        return None
    if any(q.get('kind') not in DEFAULT_CHILDREN for q in rows if not q.get('root')):
        return None
    return roots[0]


def missing_modules():
    """Kernel modules the share tree needs; modprobe is a no-op when built in."""
    def loads(module):
        try:
            return subprocess.run(['modprobe', module], capture_output=True, timeout=30).returncode == 0
        except OSError:
            return False  # No modprobe at all: treat as unavailable, never guess.
    return [m for m in SHARE_MODULES if not loads(m)]


def share_classes(plan):
    """classid -> (rate kbit, ceil kbit, leaf handle, port or None).

    Each subscription URL is one share: guaranteed total/N, may borrow up to its
    cap (default: the whole rate) while others are idle. A small default class
    carries everything else (SSH, subscription downloads, relayed uploads)."""
    total = plan['rate_mbps'] * 1000
    spare = max(1000, total // 50)
    each = (total - spare) // len(plan['users'])
    classes = {'1af2:1': (total, total, None, None), '1af2:ff': (spare, total, '1bff:', None)}
    for i, user in enumerate(plan['users'], 1):
        minor = format(0x10 + i, 'x')
        classes['1af2:' + minor] = (each, min(user['cap_mbps'] * 1000, total),
                                    format(0x1b00 + 0x10 + i, 'x') + ':', user['port'])
    return classes


def build_tree(dev, plan, verb='add'):
    classes = share_classes(plan)
    if verb == 'add':
        run('tc', 'qdisc', 'replace', 'dev', dev, 'root', 'handle', SHARE_HANDLE, 'htb', 'default', 'ff')
    for classid, (rate, ceil, leaf, port) in classes.items():
        parent = SHARE_HANDLE if classid == '1af2:1' else '1af2:1'
        # HTB's round robin adds one quantum per turn yet always sends a whole
        # packet; with TSO/GSO packets up to 64 KiB a small quantum turns it into
        # per-packet fairness (measured: 4 TCP flows got 35 vs 1 flow's 55 Mbps).
        # A quantum above the largest GSO packet keeps the shares byte-fair.
        run('tc', 'class', verb, 'dev', dev, 'parent', parent, 'classid', classid, 'htb',
            'rate', f'{rate}kbit', 'ceil', f'{ceil}kbit', 'quantum', QUANTUM)
        if verb != 'add':
            continue
        if leaf:
            # fq_codel inside each share keeps one user's flows from queueing behind each other.
            run('tc', 'qdisc', 'add', 'dev', dev, 'parent', classid, 'handle', leaf, 'fq_codel')
        if port:
            for proto, prio, match in (('ip', '1', 'ip'), ('ipv6', '2', 'ip6')):
                run('tc', 'filter', 'add', 'dev', dev, 'parent', SHARE_HANDLE, 'protocol', proto,
                    'prio', prio, 'u32', 'match', match, 'sport', str(port), '0xffff', 'flowid', classid)


def to_kbit(text):
    number, unit = re.fullmatch(r'([0-9.]+)([KMG]?)bit', text).groups()
    return float(number) * {'': 0.001, 'K': 1, 'M': 1000, 'G': 1000000}[unit]


def owned_tree(dev, shaping):
    """Root, every class rate/ceil and every port filter match the stored plan."""
    plan = shaping.get('plan')
    q = root_qdisc(dev)
    if not plan or q.get('kind') != 'htb' or q.get('handle') != SHARE_HANDLE:
        return False
    actual = {}
    for line in run('tc', 'class', 'show', 'dev', dev).splitlines():
        m = re.match(r'class htb (\S+) .*?rate (\S+) ceil (\S+)', line)
        if m:
            actual[m.group(1)] = (to_kbit(m.group(2)), to_kbit(m.group(3)))
    expected = share_classes(plan)
    if set(actual) != set(expected):
        return False
    close = lambda a, b: abs(a - b) <= max(2, b * 0.02)  # tc prints rounded units
    if not all(close(actual[c][0], expected[c][0]) and close(actual[c][1], expected[c][1]) for c in expected):
        return False
    filters = json.loads(run('tc', '-j', 'filter', 'show', 'dev', dev, 'parent', SHARE_HANDLE) or '[]')
    flows = sorted(f['options']['flowid'] for f in filters if f.get('options', {}).get('flowid'))
    return flows == sorted(c for c, v in expected.items() if v[3] for _ in range(2))


def shape(state_path, state, plan):
    """Per-URL share tree on a pristine default root; otherwise leave it and report."""
    dev = state['interface']
    root = pristine(dev)
    state['shaping'] = {'plan': plan, 'boot_id': boot_id()}
    if root is None:
        state['shaping']['result'] = 'skipped-custom-queue'
        save(state_path, state)
        return False
    if missing_modules():
        state['shaping']['result'] = 'modules-unavailable'
        save(state_path, state)
        return False
    state['shaping'].update(before=identity(root), result='applying')
    save(state_path, state)
    build_tree(dev, plan)
    if not owned_tree(dev, state['shaping']):
        raise RuntimeError('Share tree did not take effect; snapshot retained for recovery')
    state['shaping']['result'] = 'applied'
    state['qdisc_result'] = 'per-url-fair-share'
    save(state_path, state)
    return True


def set_plan(state_path, plan):
    """Change the total rate or a share's cap in place; flows are kept."""
    state = json.loads(Path(state_path).read_text())
    shaping = state.get('shaping') or {}
    if shaping.get('result') != 'applied':
        raise RuntimeError('Per-URL fair share is not active on this server')
    if not owned_tree(state['interface'], shaping):
        raise RuntimeError('Managed share tree changed outside this installer; refusing change')
    if [u['port'] for u in plan['users']] != [u['port'] for u in shaping['plan']['users']]:
        raise RuntimeError('Users/ports differ from the installed tree; rate/cap changes only')
    build_tree(state['interface'], plan, 'change')
    if not owned_tree(state['interface'], dict(shaping, plan=plan)):
        raise RuntimeError('Share tree change verification failed')
    shaping['plan'] = plan
    save(state_path, state)
    return state


def replace_plan(state_path, plan):
    """URLs were added or removed: rebuild our tree in place (a brief requeue)."""
    state = json.loads(Path(state_path).read_text())
    shaping = state.get('shaping') or {}
    if shaping.get('result') != 'applied':
        raise RuntimeError('Per-URL fair share is not active on this server')
    if [u['port'] for u in plan['users']] == [u['port'] for u in shaping['plan']['users']]:
        return set_plan(state_path, plan)
    dev = state['interface']
    if not owned_tree(dev, shaping):
        raise RuntimeError('Managed share tree changed outside this installer; refusing change')
    shaping.update(plan=plan, result='applying')
    save(state_path, state)
    run('tc', 'qdisc', 'del', 'dev', dev, 'root')
    build_tree(dev, plan)
    if not owned_tree(dev, shaping):
        raise RuntimeError('Rebuilt share tree did not take effect; snapshot retained for recovery')
    shaping['result'] = 'applied'
    save(state_path, state)
    return state


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


def apply(state_path, plan=None):
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
    if plan and shape(state_path, state, plan):
        return state
    if layout['isolated'] and safe_pfifofast(q, allow_nonzero=approved):
        set_fq(state_path, state, q)
    return state


def check_restore(state):
    for key, target in state['applied'].items():
        value = run('sysctl', '-n', key)
        if value not in (target, state['before'][key]):
            raise RuntimeError('Network parameter changed outside this installer: ' + key)
    shaping = state.get('shaping') or {}
    if shaping.get('result') in ('applied', 'applying'):
        q = root_qdisc(state['interface'])
        ours = owned_tree(state['interface'], shaping) or (
            shaping['result'] == 'applying' and q.get('kind') == 'htb' and q.get('handle') == SHARE_HANDLE)
        if not ours and pristine(state['interface']) is None:
            raise RuntimeError('Fair-share queue changed outside installer; manual review required')
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
    shaping = state.get('shaping') or {}
    if shaping.get('result') in ('applied', 'applying'):
        # Sysctls (default_qdisc) are restored first, so deleting the managed root
        # lets the kernel recreate the original default queue.
        q = root_qdisc(state['interface'])
        if q.get('kind') == 'htb' and q.get('handle') == SHARE_HANDLE:
            # Deleting the root removes every class, leaf and filter of the tree.
            run('tc', 'qdisc', 'del', 'dev', state['interface'], 'root')
        if pristine(state['interface']) is None:
            raise RuntimeError('Default queue was not restored after removing the share tree')
        shaping['result'] = 'restored'
        save(state_path, state)
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
    if (state.get('shaping') or {}).get('result') == 'applied':
        raise RuntimeError('Per-URL fair share manages this queue; FQ repair does not apply')
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
    shaping = state.get('shaping') or {}
    if shaping.get('result') == 'applied':
        # A reboot drops tc state; reapply only onto an untouched default root.
        # A kernel update without the modules keeps the default queue (reported).
        if not owned_tree(dev, shaping) and pristine(dev) is not None and not missing_modules():
            build_tree(dev, shaping['plan'])
            shaping['boot_id'] = boot_id()
            save(state_path, state)
        return health(state_path)
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
    shaping = state.get('shaping') or {}
    shaped = shaping.get('result') == 'applied'
    tree_ok = shaped and owned_tree(state['interface'], shaping)
    warnings = []
    if shaped and not tree_ok:
        warnings.append('Managed per-URL share tree missing or modified (kernel modules after an update?); '
                        'URLs are no longer isolated')
    elif shaping.get('result') in ('skipped-custom-queue', 'modules-unavailable'):
        warnings.append('Fair share requested but not applied (' + shaping['result'] + '); flows still share via the default queue')
    if not fq and not tree_ok:
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
            'fair_share': {'result': shaping.get('result', 'not-requested'),
                           'plan': shaping.get('plan'), 'tree_matches': tree_ok if shaped else None},
            'tcp_congestion_control': cc, 'default_qdisc': default,
            'last_action': state.get('qdisc_result', 'legacy-snapshot'), 'warnings': warnings}


def verify_health(state_path):
    report = health(state_path)
    if report['managed_fq_matches'] is False:
        raise RuntimeError('Managed FQ verification failed; inspect network status')
    if report['fair_share']['tree_matches'] is False:
        raise RuntimeError('Managed per-URL share tree verification failed; inspect network status')
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
