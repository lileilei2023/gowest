import copy
import io
import json
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import network
import probe


def fifo(handle='0:'):
    return {'kind': 'pfifo_fast', 'handle': handle, 'root': True,
            'options': {'bands': 3, 'priomap': network.PRIOMAP[:], 'multiqueue': False}}


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.path = self.root / 'network.json'
        self.q = fifo()
        self.extra = []
        self.classes = []
        self.filters = []
        self.params = dict(network.PARAMETERS)
        self.params.update({'net.core.default_qdisc': 'pfifo_fast', 'net.ipv4.tcp_congestion_control': 'cubic'})
        self.commands = []
        self.stack.enter_context(patch.object(network, 'run', side_effect=self.run_command))
        self.stack.enter_context(patch.object(network, 'interface', return_value='eth0'))
        self.stack.enter_context(patch.object(network, 'boot_id', return_value='boot-one'))
        self.stack.enter_context(patch.object(network, 'ARCHIVES', self.root / 'archives'))

    def run_command(self, *args, **kwargs):
        self.commands.append(args)
        if args[0] == 'sysctl':
            if args[1] == '-w':
                k, v = args[2].split('=', 1)
                self.params[k] = v
                return ''
            if args[2] == 'net.ipv4.tcp_available_congestion_control':
                return 'cubic bbr'
            return self.params[args[2]]
        if args[:3] == ('tc', '-j', 'qdisc'):
            return json.dumps([self.q] + self.extra)
        if args[:3] == ('tc', '-j', 'class'):
            return json.dumps(self.classes)
        if args[:3] == ('tc', '-j', 'filter'):
            return json.dumps(self.filters)
        if args[:3] == ('tc', 'qdisc', 'replace'):
            handle = args[args.index('handle') + 1]
            self.q = (fifo(handle) if args[-1] == 'pfifo_fast' else
                      {'kind': 'fq', 'handle': handle, 'root': True, 'options': {'limit': 10000}})
            return ''
        raise AssertionError(args)

    def seed(self, handle='8002:'):
        self.q = fifo(handle)
        state = {'interface': 'eth0', 'before': {}, 'applied': {}, 'qdisc_before': copy.deepcopy(self.q),
                 'qdisc_changed': False, 'qdisc_after': None}
        network.save(self.path, state)
        return state

    def test_full_apply_restore_reinstall_with_receipt(self):
        network.apply(self.path)
        self.assertEqual(self.q['handle'], network.FQ_HANDLE)
        self.assertTrue(network.verify_health(self.path)['managed_fq_matches'])
        network.restore(self.path)
        self.assertEqual(self.q, fifo(network.RESTORED_HANDLE))
        archive = network.ARCHIVES / 'rollback-test/var--lib--la-vpn-deploy/network.json'
        archive.parent.mkdir(parents=True)
        self.path.rename(archive)
        # Test models the root-owned archive used by the real installer.
        with patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=0, st_mode=0o100600)), \
                patch.object(Path, 'is_symlink', return_value=False), \
                patch.object(Path, 'glob', return_value=[archive]):
            approved = network.rollback_receipt('eth0', self.q)
        self.assertTrue(approved)
        with patch.object(network, 'rollback_receipt', return_value=approved):
            network.apply(self.path)
        self.assertEqual(self.q['kind'], 'fq')

    def test_unknown_nonzero_default_fifo_skipped(self):
        self.q = fifo('8002:')
        state = network.apply(self.path)
        self.assertFalse(state['qdisc_changed'])
        self.assertFalse(network.health(self.path)['fq_effective'])
        self.assertTrue(network.health(self.path)['warnings'])

    def test_explicit_legacy_repair_and_exact_restore(self):
        before = self.seed()
        with self.assertRaisesRegex(RuntimeError, 'adopt-default-fifo'):
            network.repair_fq(self.path)
        state = network.repair_fq(self.path, True)
        self.assertEqual(state['qdisc_before'], before['qdisc_before'])
        self.assertTrue(state['fifo_approved'])
        network.repair_fq(self.path, True)  # idempotent
        self.assertFalse(any(c[:2] == ('sysctl', '-w') for c in self.commands))
        network.restore(self.path)
        self.assertEqual(self.q, before['qdisc_before'])
        count = len(self.commands)
        network.restore(self.path)
        self.assertFalse(any(c[:3] == ('tc', 'qdisc', 'replace') for c in self.commands[count:]))

    def test_custom_options_cannot_be_adopted(self):
        for options in ({'bands': 4}, {'priomap': [0] * 16}, {'multiqueue': True}, {'unknown': 1}):
            self.seed()
            self.q['options'].update(options)
            state = json.loads(self.path.read_text())
            state['qdisc_before'] = copy.deepcopy(self.q)
            network.save(self.path, state)
            with self.assertRaises(RuntimeError):
                network.repair_fq(self.path, True)

    def test_custom_kinds_never_adopted(self):
        for kind in ('cake', 'htb', 'mq', 'fq_codel', 'noqueue'):
            state = self.seed()
            self.q['kind'] = kind
            state['qdisc_before'] = copy.deepcopy(self.q)
            network.save(self.path, state)
            with self.assertRaises(RuntimeError):
                network.repair_fq(self.path, True)

    def test_classes_filters_or_child_queue_refuse_repair(self):
        self.seed()
        for name in ('classes', 'filters', 'extra'):
            setattr(self, name, [{'handle': '2:'}])
            with self.assertRaises(RuntimeError):
                network.repair_fq(self.path, True)
            setattr(self, name, [])

    def test_mutated_baseline_refuses_repair(self):
        self.seed()
        self.q['handle'] = '1234:'
        with self.assertRaises(RuntimeError):
            network.repair_fq(self.path, True)

    def test_changed_fq_handle_options_topology_block_restore(self):
        network.apply(self.path)
        original = copy.deepcopy(self.q)
        for change in ('handle', 'options', 'filters'):
            self.q = copy.deepcopy(original)
            if change == 'filters':
                self.filters = [{'kind': 'u32'}]
            else:
                self.q[change] = 'abcd:' if change == 'handle' else {'limit': 9999}
            self.commands.clear()
            with self.assertRaises(RuntimeError):
                network.restore(self.path)
            self.assertFalse(any(c[:2] == ('sysctl', '-w') for c in self.commands))
            with self.assertRaises(RuntimeError):
                network.verify_health(self.path)
            self.filters = []

    def test_boot_reapply_updates_owned_state(self):
        self.seed()
        network.repair_fq(self.path, True)
        self.q = fifo()
        report = network.boot_fq('eth0', self.path)
        self.assertTrue(report['managed_fq_matches'])
        network.restore(self.path)
        self.assertEqual(self.q['handle'], '8002:')

    def test_boot_preserves_changed_custom_queue(self):
        network.apply(self.path)
        self.q = {'kind': 'cake', 'handle': 'a:', 'root': True, 'options': {}}
        report = network.boot_fq('eth0', self.path)
        self.assertFalse(report['fq_effective'])
        self.assertEqual(self.q['kind'], 'cake')

    def test_boot_accepts_matching_kernel_fq_without_reset(self):
        network.apply(self.path)
        self.q['handle'] = '0:'
        self.commands.clear()
        with patch.object(network, 'boot_id', return_value='boot-two'):
            report = network.boot_fq('eth0', self.path)
        self.assertTrue(report['managed_fq_matches'])
        self.assertEqual(report['last_action'], 'kernel-fq-after-reboot')
        self.assertFalse(any(c[:3] == ('tc', 'qdisc', 'replace') for c in self.commands))
        network.restore(self.path)
        self.assertEqual(self.q['kind'], 'pfifo_fast')

    def test_same_boot_zero_handle_change_not_silently_accepted(self):
        network.apply(self.path)
        self.q['handle'] = '0:'
        self.assertFalse(network.boot_fq('eth0', self.path)['managed_fq_matches'])

    def test_boot_does_not_adopt_custom_kernel_fq_options(self):
        network.apply(self.path)
        self.q.update(handle='0:', options={'limit': 20000})
        with patch.object(network, 'boot_id', return_value='boot-two'):
            self.assertFalse(network.boot_fq('eth0', self.path)['managed_fq_matches'])
        self.assertEqual(self.q['options']['limit'], 20000)

    def test_existing_fq_unchanged(self):
        self.q = {'kind': 'fq', 'handle': '123:', 'root': True, 'options': {'limit': 20000}}
        network.apply(self.path)
        network.boot_fq('eth0', self.path)
        network.repair_fq(self.path, True)
        self.assertEqual(self.q['options'], {'limit': 20000})
        self.assertFalse(any(c[:3] == ('tc', 'qdisc', 'replace') for c in self.commands))

    def test_interrupted_apply_recovers_owned_handle_only(self):
        state = self.seed()
        state.update(qdisc_changed=True, fq_handle=network.FQ_HANDLE)
        network.save(self.path, state)
        self.q = {'kind': 'fq', 'handle': '9876:', 'root': True, 'options': {}}
        with self.assertRaises(RuntimeError):
            network.restore(self.path)
        self.q['handle'] = network.FQ_HANDLE
        network.restore(self.path)
        self.assertEqual(self.q['handle'], '8002:')

    def test_interrupted_restore_recovers_recorded_intent(self):
        state = network.apply(self.path)
        state['qdisc_restore_intent'] = fifo(network.RESTORED_HANDLE)
        network.save(self.path, state)
        self.q = fifo(network.RESTORED_HANDLE)
        network.restore(self.path)
        self.assertEqual(json.loads(self.path.read_text())['qdisc_restored'], self.q)

    def test_state_symlink_refused(self):
        target = self.root / 'target'
        target.write_text('user data')
        self.path.symlink_to(target)
        with self.assertRaises(RuntimeError):
            network.save(self.path, {})
        self.assertEqual(target.read_text(), 'user data')


class ExtendedProbeTests(unittest.TestCase):
    def test_first_byte_separated_from_whole_request_speed(self):
        response = SimpleNamespace(stdout='200\t8388608\t4\t2097152\t1.5', returncode=0)
        with patch.object(probe.subprocess, 'run', return_value=response):
            row = probe.download_round(['curl'], 8388608, 1)
        self.assertEqual(row['download_mbps'], 16.78)
        self.assertEqual(row['body_phase_mbps_estimate'], 26.84)

    def test_parallel_uses_common_wall_clock(self):
        with patch.object(probe, 'download_round', return_value={'completed': True, 'download_mbps': 8}), \
                patch.object(probe.time, 'monotonic', side_effect=[10, 12]):
            row = probe.download_batch(['curl'], 1048576, 1, 2)
        self.assertEqual(row['download_mbps'], 8.39)
        self.assertEqual(len(row['streams']), 2)

    def test_parallel_partial_failure_has_no_speed(self):
        with patch.object(probe, 'download_round', side_effect=[{'completed': True}, {'completed': False}]):
            row = probe.download_batch(['curl'], 1048576, 1, 2)
        self.assertFalse(row['completed'])
        self.assertNotIn('download_mbps', row)

    def test_upload_validation(self):
        for data, code, ok in [(b'200\t1048576\t2\t524288', 0, True),
                               (b'200\t1024\t2\t512', 0, False),
                               (b'403\t1048576\t2\t524288', 0, False),
                               (b'200\t1048576\t2\t524288', 28, False),
                               (b'200\t1048576\t2\tNaN', 0, False)]:
            with patch.object(probe.subprocess, 'run', return_value=SimpleNamespace(stdout=data, returncode=code)):
                self.assertEqual(probe.upload_round(['curl'], 1048576, 1)['completed'], ok)

    def test_budget_and_options_rejected_before_network(self):
        for args in (['--download-mib', '8', '--parallel', '4', '--rounds', '9'], ['--parallel', '2'],
                     ['--upload-mib', '9'], ['--target-mbps', '0'],
                     ['--download-mib', '8', '--parallel', '4', '--rounds', '8', '--upload-mib', '1']):
            with redirect_stderr(io.StringIO()), patch.object(probe.subprocess, 'run') as request, \
                    self.assertRaises(SystemExit):
                probe.main(args)
            request.assert_not_called()

    def test_upload_failure_exit_and_utilization(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()), \
                patch.object(probe.subprocess, 'run', return_value=SimpleNamespace(stdout='ip=8.8.8.8', returncode=0)), \
                patch.object(probe, 'upload_round', side_effect=[{'completed': True, 'upload_mbps': 15},
                                                                {'completed': False}]), \
                patch.object(probe.time, 'sleep'):
            code = probe.main(['--upload-mib', '4', '--rounds', '2'])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result['requested_payload_budget_mib'], 8)
        self.assertEqual(result['upload_utilization']['median_percent'], 50)
        self.assertEqual(result['upload_summary']['failed'], 1)


if __name__ == '__main__':
    unittest.main()
