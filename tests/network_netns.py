"""Real tc lifecycle test; MUST run under unshare --net as root. No host sysctl writes."""
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import network


def main():
    if os.geteuid() != 0 or os.readlink('/proc/self/ns/net') == os.readlink('/proc/1/ns/net'):
        raise RuntimeError('Refusing host network namespace; use sudo unshare --net python3 -B tests/network_netns.py')
    dev = 'kitsim0'
    network.run('ip', 'link', 'add', dev, 'type', 'dummy')
    network.run('tc', 'qdisc', 'replace', 'dev', dev, 'root', 'handle', '8002:', 'pfifo_fast')
    before = network.root_qdisc(dev)
    with tempfile.TemporaryDirectory(prefix='la-vpn-netns-') as directory:
        state_path = Path(directory) / 'network.json'
        state = {'interface': dev, 'applied': {}, 'before': {}, 'qdisc_before': before,
                 'qdisc_changed': False, 'qdisc_after': None}
        network.save(state_path, state)
        try:
            network.repair_fq(state_path)
        except RuntimeError:
            pass
        else:
            raise AssertionError('Unowned FIFO was silently adopted')
        network.repair_fq(state_path, True)
        assert network.verify_health(state_path)['managed_fq_matches']
        network.boot_fq(dev, state_path)
        network.run('tc', 'qdisc', 'change', 'dev', dev, 'root', 'fq', 'limit', '20000')
        try:
            network.restore(state_path)
        except RuntimeError:
            pass
        else:
            raise AssertionError('Custom FQ was overwritten')
        network.run('tc', 'qdisc', 'change', 'dev', dev, 'root', 'fq', 'limit', '10000')
        network.restore(state_path)
        assert network.identity(network.root_qdisc(dev)) == network.identity(before)
        network.restore(state_path)  # idempotent
        archive = Path(directory) / 'archives/rollback-test/var--lib--la-vpn-deploy/network.json'
        archive.parent.mkdir(parents=True)
        state_path.rename(archive)
        network.ARCHIVES = archive.parents[2]
        assert network.rollback_receipt(dev, network.root_qdisc(dev))
    print(json.dumps({'real_tc_lifecycle': 'passed', 'namespace': 'isolated',
                      'host_interfaces_changed': False, 'host_sysctls_changed': False}))


if __name__ == '__main__':
    main()
