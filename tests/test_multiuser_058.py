import copy
import json
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check
import launcher
import network
import render
import subinfo
import vpn
import test_kit


def base_settings(**extra):
    raw = {'server_ip': '192.0.2.10', 'node_prefix': 'DC', 'hy2_up_mbps': 100, 'hy2_down_mbps': 100,
           'subscription': {'scheme': 'http', 'host': '192.0.2.10', 'port': 80, 'allow_insecure_http': True}}
    raw.update(extra)
    return raw


class SettingsTests(unittest.TestCase):
    def test_single_user_defaults_unchanged(self):
        s = render.settings(base_settings())
        self.assertEqual(s['users'], [])
        self.assertFalse(s['fair_share'])
        self.assertEqual(s['traffic'], {'total_bytes': 0, 'expire_epoch': 0, 'metering': 'both', 'on_exceed': 'warn'})
        self.assertEqual(render.shape_mbps(s), 0)

    def test_several_users_enable_fair_share(self):
        s = render.settings(base_settings(users=['alice', 'bob']))
        self.assertTrue(s['fair_share'])
        self.assertEqual(render.shape_mbps(s), 95)

    def test_invalid_users_rejected(self):
        for users in (['Alice', 'bob'], ['a', 'a'], ['bad name'], 'alice', ['x' * 17],
                      ['u%d' % i for i in range(render.MAX_USERS + 1)]):
            with self.subTest(users=users), self.assertRaises(ValueError):
                render.settings(base_settings(users=users))

    def test_quota_parsing(self):
        self.assertEqual(render.parse_bytes('1TB'), 10**12)
        self.assertEqual(render.parse_bytes('512GB'), 512 * 10**9)
        self.assertEqual(render.parse_bytes('1.5 TB'), 15 * 10**11)
        self.assertEqual(render.parse_bytes('1TiB'), 2**40)
        self.assertEqual(render.parse_bytes(''), 0)
        for bad in ('1T', '512MB', '-1TB', 'lots', '0.0GB'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                render.parse_bytes(bad)

    def test_stop_requires_quota_and_known_policies(self):
        with self.assertRaises(ValueError):
            render.settings(base_settings(traffic={'on_exceed': 'stop'}))
        with self.assertRaises(ValueError):
            render.settings(base_settings(traffic={'metering': 'inbound'}))
        s = render.settings(base_settings(traffic={'total_bytes': 10**12, 'on_exceed': 'stop', 'metering': 'outbound'}))
        self.assertEqual(s['traffic']['on_exceed'], 'stop')

    def test_launcher_user_input(self):
        self.assertEqual(launcher.parse_users(''), [])
        self.assertEqual(launcher.parse_users('1'), [])
        self.assertEqual(launcher.parse_users('3'), ['user1', 'user2', 'user3'])
        self.assertEqual(launcher.parse_users('Alice， Bob'), ['alice', 'bob'])
        for bad in ('alice', '0', str(render.MAX_USERS + 1)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                launcher.parse_users(bad)

    def test_build_settings_carries_quota(self):
        s = launcher.build_settings('8.8.8.8', 30, 30, 'http', accept_http_risk=True, node_prefix='DC',
                                    users=['a1', 'b2'], quota='512GB', on_exceed='stop')
        self.assertEqual(s['traffic']['total_bytes'], 512 * 10**9)
        self.assertEqual(s['users'], ['a1', 'b2'])


class MultiUserRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.out = render.render(base_settings(users=['alice', 'bob', 'carol']), Path(cls.temp.name) / 'gen')
        cls.server = json.loads((cls.out / 'server.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_independent_credentials_per_user(self):
        users = self.server['inbounds'][1]['users']
        self.assertEqual([u['name'] for u in users], ['alice', 'bob', 'carol'])
        self.assertEqual(len({u['password'] for u in users}), 3)
        self.assertEqual(len({u['uuid'] for u in self.server['inbounds'][0]['users']}), 3)
        tokens = {(self.out / 'users' / n / 'subscription-urls.txt').read_text() for n in ('alice', 'bob', 'carol')}
        self.assertEqual(len(tokens), 3)

    def test_bbr_instead_of_brutal(self):
        import yaml
        self.assertTrue(self.server['inbounds'][1]['ignore_client_bandwidth'])
        hy = next(p for p in yaml.safe_load((self.out / 'users/bob/normal.yaml').read_text())['proxies']
                  if p['type'] == 'hysteria2')
        self.assertNotIn('up', hy)
        self.assertNotIn('down', hy)

    def test_nginx_serves_each_user_folder(self):
        text = (self.out / 'nginx.conf').read_text()
        self.assertEqual(text.count('location = /sub/'), 3 * len(render.SUBSCRIPTION_FILES))
        self.assertIn('alias /etc/la-vpn/subscriptions/carol/strict.yaml;', text)
        self.assertNotIn('alias /etc/la-vpn/subscriptions/normal.yaml;', text)

    def test_guides_name_the_user(self):
        text = (self.out / 'users/alice/client-import.txt').read_text()
        self.assertIn('用户 alice', text)
        self.assertIn('gowest-DC-alice-%E6%97%A5%E5%B8%B8', text)

    def test_cross_config_check(self):
        result = check.check(self.out)
        self.assertEqual(result['users'], 3)
        self.assertTrue(result['fair_share'])


class CakeTests(unittest.TestCase):
    """Simulated tc; the real-kernel test runs in isolated network namespaces."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.path = self.root / 'network.json'
        self.default = {'kind': 'fq_codel', 'handle': '0:', 'root': True, 'options': {'limit': 10240}}
        self.rows = [copy.deepcopy(self.default)]
        self.cake_available = True
        self.params = dict(network.PARAMETERS, **{'net.core.default_qdisc': 'fq_codel',
                                                  'net.ipv4.tcp_congestion_control': 'cubic'})
        self.commands = []
        self.stack.enter_context(patch.object(network, 'run', side_effect=self.tc))
        self.stack.enter_context(patch.object(network, 'interface', return_value='eth0'))
        self.stack.enter_context(patch.object(network, 'boot_id', return_value='boot-one'))
        self.stack.enter_context(patch.object(network, 'ARCHIVES', self.root / 'archives'))

    def tc(self, *args, check=True):
        self.commands.append(args)
        if args[0] == 'sysctl':
            if args[1] == '-w':
                key, value = args[2].split('=', 1)
                self.params[key] = value
                return ''
            if args[2] == 'net.ipv4.tcp_available_congestion_control':
                return 'cubic bbr'
            return self.params[args[2]]
        if args[0] == 'modprobe':
            return ''
        if args[:3] == ('tc', '-j', 'qdisc'):
            return json.dumps(self.rows)
        if args[:3] == ('tc', '-j', 'class'):
            # Real iproute2 prints malformed JSON for CAKE flow classes under load.
            return '[{"class":"cake",' if self.rows[0]['kind'] == 'cake' else '[]'
        if args[:3] == ('tc', '-j', 'filter'):
            return '[]'
        if args[:3] in (('tc', 'qdisc', 'replace'), ('tc', 'qdisc', 'change')) and 'cake' in args:
            if not self.cake_available:
                raise RuntimeError('Command failed: tc')
            rate = args[args.index('bandwidth') + 1]
            self.rows = [{'kind': 'cake', 'handle': network.CAKE_HANDLE, 'root': True,
                          'options': {'bandwidth': rate, 'flowmode': 'dual-dsthost'}}]
            return ''
        if args[:3] == ('tc', 'qdisc', 'del'):
            self.rows = [dict(self.default, kind=self.params['net.core.default_qdisc'])]
            return ''
        raise AssertionError(args)

    def test_apply_shapes_and_restore_returns_default(self):
        state = network.apply(self.path, shape_mbps=95)
        self.assertEqual(state['shaping']['result'], 'applied')
        self.assertEqual(self.rows[0]['kind'], 'cake')
        self.assertIn('dual-dsthost', self.commands[-2] if 'cake' in self.commands[-2] else
                      next(c for c in self.commands if 'cake' in c))
        self.assertTrue(network.verify_health(self.path)['fair_share']['cake_matches'])
        network.restore(self.path)
        self.assertEqual(self.rows[0]['kind'], 'fq_codel', 'kernel default restored after sysctl reset')
        self.assertEqual(json.loads(self.path.read_text())['shaping']['result'], 'restored')

    def test_custom_root_left_alone(self):
        self.rows = [{'kind': 'htb', 'handle': '1:', 'root': True, 'options': {}}]
        state = network.apply(self.path, shape_mbps=95)
        self.assertEqual(state['shaping']['result'], 'skipped-custom-queue')
        self.assertEqual(self.rows[0]['kind'], 'htb')
        self.assertTrue(any('not applied' in w for w in network.health(self.path)['warnings']))

    def test_missing_cake_module_falls_back(self):
        self.cake_available = False
        state = network.apply(self.path, shape_mbps=95)
        self.assertEqual(state['shaping']['result'], 'cake-unavailable')
        self.assertEqual(self.rows[0]['kind'], 'fq_codel')

    def test_rate_change_in_place(self):
        network.apply(self.path, shape_mbps=95)
        network.set_rate(self.path, 190)
        self.assertEqual(self.rows[0]['options']['bandwidth'], '190mbit')
        self.assertTrue(network.verify_health(self.path)['fair_share']['cake_matches'])

    def test_external_change_blocks_rate_change_and_restore(self):
        network.apply(self.path, shape_mbps=95)
        self.rows = [{'kind': 'htb', 'handle': '1:', 'root': True, 'options': {}}]
        with self.assertRaisesRegex(RuntimeError, 'outside'):
            network.set_rate(self.path, 50)
        with self.assertRaisesRegex(RuntimeError, 'outside installer'):
            network.check_restore(json.loads(self.path.read_text()))
        with self.assertRaisesRegex(RuntimeError, 'CAKE verification failed'):
            network.verify_health(self.path)

    def test_boot_reapplies_after_reboot_only_on_default_root(self):
        network.apply(self.path, shape_mbps=95)
        self.rows = [dict(self.default, kind='fq')]  # reboot: tc state gone
        network.boot_fq('eth0', self.path)
        self.assertEqual(self.rows[0]['kind'], 'cake')
        self.rows = [{'kind': 'htb', 'handle': '1:', 'root': True, 'options': {}}]
        network.boot_fq('eth0', self.path)
        self.assertEqual(self.rows[0]['kind'], 'htb', 'never overwrite an administrator queue')

    def test_fq_repair_refused_while_shaping(self):
        network.apply(self.path, shape_mbps=95)
        with self.assertRaisesRegex(RuntimeError, 'CAKE manages'):
            network.repair_fq(self.path)


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(subinfo, 'STATE', Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []

    def enforce(self, used, month='2026-10', **traffic):
        traffic = dict({'total_bytes': 1000, 'on_exceed': 'stop', 'metering': 'both'}, **traffic)
        return subinfo.enforce(traffic, used, month, systemctl=lambda *a: self.calls.append(a))

    def test_outbound_metering(self):
        self.assertEqual(subinfo.used_bytes(300, 500, 'outbound'), 500)
        self.assertEqual(subinfo.used_bytes(300, 500, 'both'), 800)
        self.assertIn('download=0', subinfo.header(300, 500, 1000, 0, 'outbound'))

    def test_pause_once_then_resume_next_month(self):
        self.assertEqual(self.enforce(999)['percent'], 99.9)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.enforce(1000)['paused_month'], '2026-10')
        self.enforce(1200)
        self.assertEqual(self.calls, [('stop', 'la-vpn.service')], 'stop only once')
        self.assertIsNone(self.enforce(10, month='2026-11')['paused_month'])
        self.assertEqual(self.calls[-1], ('start', 'la-vpn.service'))

    def test_raised_quota_or_warn_policy_resumes(self):
        self.enforce(1000)
        self.enforce(1000, total_bytes=5000)
        self.assertEqual(self.calls[-1], ('start', 'la-vpn.service'))
        self.enforce(6000, total_bytes=5000)
        self.enforce(6000, total_bytes=5000, on_exceed='warn')
        self.assertEqual(self.calls[-1], ('start', 'la-vpn.service'))

    def test_warn_never_stops_and_manual_stop_is_not_undone(self):
        self.enforce(5000, on_exceed='warn')
        self.enforce(10)
        self.assertEqual(self.calls, [])


class MultiUserTransactionTests(test_kit.TransactionTests):
    """Re-run the whole install/rollback transaction suite with three users."""

    def setUp(self):
        super().setUp()
        raw = dict(self.s, users=['alice', 'bob', 'carol'])
        self.s = render.settings(raw)

    def test_each_user_has_served_folder(self):
        vpn.install(self.s, '192.0.2.10')
        for name in ('alice', 'bob', 'carol'):
            folder = vpn.CONFIG / 'subscriptions' / name
            self.assertEqual(folder.stat().st_mode & 0o777, 0o750)
            self.assertTrue((folder / 'normal.yaml').is_file())
        self.assertFalse((vpn.CONFIG / 'subscriptions/normal.yaml').exists())

    def test_quota_change_updates_managed_settings(self):
        vpn.install(self.s, '192.0.2.10')
        vpn.change_traffic('192.0.2.10', quota='1TB', on_exceed='stop')
        saved = json.loads((vpn.CONFIG / 'settings.json').read_text())
        self.assertEqual(saved['traffic']['total_bytes'], 10**12)
        state = vpn.load_state()
        self.assertEqual(vpn.sha(vpn.CONFIG / 'settings.json'), state['files'][str(vpn.CONFIG / 'settings.json')])
        with self.assertRaisesRegex(RuntimeError, 'Confirm'):
            vpn.change_traffic('198.51.100.1', quota='2TB')

    def test_externally_edited_settings_block_changes(self):
        vpn.install(self.s, '192.0.2.10')
        (vpn.CONFIG / 'settings.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'changed externally'):
            vpn.change_traffic('192.0.2.10', quota='1TB')


class SingleUserBandwidthTests(test_kit.TransactionTests):
    def test_brutal_bandwidth_change_rewrites_profiles_not_urls(self):
        vpn.install(self.s, '192.0.2.10')
        before = (vpn.STATE / 'generated/subscription-urls.txt').read_text()
        vpn.change_bandwidth('50Mbps', '192.0.2.10')
        self.assertIn('up: 50 Mbps', (vpn.CONFIG / 'subscriptions/normal.yaml').read_text())
        self.assertEqual((vpn.STATE / 'generated/subscription-urls.txt').read_text(), before)
        state = vpn.load_state()
        for file, digest in state['files'].items():
            self.assertEqual(vpn.sha(file), digest)


if __name__ == '__main__':
    unittest.main()
