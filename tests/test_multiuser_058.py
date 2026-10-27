import contextlib
import copy
import io
import json
from contextlib import ExitStack
from types import SimpleNamespace
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
        self.assertEqual(render.proxy_ports(s), [20001, 20002])

    def test_share_plan_follows_urls_and_caps(self):
        s = render.settings(base_settings(users=['alice', 'bob', 'carol'], user_caps={'bob': 20, 'carol': 500}))
        plan = render.share_plan(s)
        self.assertEqual(plan['rate_mbps'], 95)
        self.assertEqual([(u['name'], u['port'], u['cap_mbps']) for u in plan['users']],
                         [('alice', 20001, 95), ('bob', 20002, 20), ('carol', 20003, 95)])
        self.assertIsNone(render.share_plan(render.settings(base_settings())))

    def test_bad_caps_and_ports_rejected(self):
        for extra in ({'user_caps': {'nobody': 10}}, {'user_caps': {'alice': 0}},
                      {'user_port_base': 32766}, {'user_port_base': 79},
                      {'user_port_base': 440}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                render.settings(base_settings(users=['alice', 'bob', 'carol', 'dave'], **extra))
        with self.assertRaises(ValueError):
            render.settings(base_settings(fair_share=True))

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
        self.assertEqual(render.parse_bytes('800'), 800 * 10**9, 'bare number is GB')
        self.assertEqual(render.parse_bytes('1.5'), 15 * 10**8)
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

    def test_firewall_follows_per_url_ports(self):
        import firewall
        import hostfirewall
        raw = base_settings(users=['alice', 'bob'])
        s = render.settings(raw)
        self.assertEqual(hostfirewall.required_ports(s),
                         [('tcp', 20001), ('udp', 20001), ('tcp', 20002), ('udp', 20002), ('tcp', 80)])
        doc = firewall.generate(raw, ['203.0.113.0/24'])
        notes = {r['协议及端口']: r['备注'] for r in doc['data']}
        self.assertEqual(notes['UDP:20002'], 'Hysteria2 · bob')
        self.assertEqual(notes['TCP:20001'], 'VLESS REALITY · alice')
        self.assertNotIn('TCP:443', notes)
        self.assertTrue(firewall.check(raw, doc)['valid'])

    def test_launcher_user_input(self):
        self.assertEqual(launcher.parse_users(''), ([], {}))
        self.assertEqual(launcher.parse_users('1'), ([], {}))
        self.assertEqual(launcher.parse_users('3'), (['user1', 'user2', 'user3'], {}))
        self.assertEqual(launcher.parse_users('Alice， Bob：20Mbps'), (['alice', 'bob'], {'bob': 20}))
        for bad in ('alice', '0', str(render.MAX_USERS + 1), 'a,b:20MB/s'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                launcher.parse_users(bad)

    def test_build_settings_carries_quota(self):
        s = launcher.build_settings('8.8.8.8', 30, 30, 'http', accept_http_risk=True, node_prefix='DC',
                                    users=['a1', 'b2'], quota='512GB', on_exceed='stop', user_caps={'b2': 10})
        self.assertEqual(s['traffic']['total_bytes'], 512 * 10**9)
        self.assertEqual(s['users'], ['a1', 'b2'])
        self.assertEqual(s['user_caps'], {'b2': 10})


class MultiUserRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.out = render.render(base_settings(users=['alice', 'bob', 'carol']), Path(cls.temp.name) / 'gen')
        cls.server = json.loads((cls.out / 'server.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_independent_credentials_and_port_per_url(self):
        hy = [i for i in self.server['inbounds'] if i['type'] == 'hysteria2']
        vl = [i for i in self.server['inbounds'] if i['type'] == 'vless']
        self.assertEqual([i['users'][0]['name'] for i in hy], ['alice', 'bob', 'carol'])
        self.assertEqual(len({i['users'][0]['password'] for i in hy}), 3)
        self.assertEqual(len({i['users'][0]['uuid'] for i in vl}), 3)
        self.assertEqual([i['listen_port'] for i in hy], [20001, 20002, 20003])
        self.assertEqual([i['listen_port'] for i in vl], [20001, 20002, 20003])
        nodes = (self.out / 'users/bob/nodes.txt').read_text()
        import base64
        self.assertIn(':20002', base64.b64decode(nodes).decode())
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


def plan(rate=95, caps=(95, 95, 95)):
    return {'rate_mbps': rate, 'users': [{'name': n, 'port': 20000 + i, 'cap_mbps': c}
                                         for i, (n, c) in enumerate(zip(('alice', 'bob', 'carol'), caps), 1)]}


class ShareTreeTests(unittest.TestCase):
    """Simulated tc that prints HTB classes as text, like real iproute2.
    The real-kernel run happens in isolated network namespaces."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.path = self.root / 'network.json'
        self.default = {'kind': 'fq_codel', 'handle': '0:', 'root': True, 'options': {'limit': 10240}}
        self.reset()
        self.missing = []
        self.params = dict(network.PARAMETERS, **{'net.core.default_qdisc': 'fq_codel',
                                                  'net.ipv4.tcp_congestion_control': 'cubic'})
        self.commands = []
        self.stack.enter_context(patch.object(network, 'run', side_effect=self.tc))
        self.stack.enter_context(patch.object(network, 'missing_modules', side_effect=lambda: self.missing))
        self.stack.enter_context(patch.object(network, 'interface', return_value='eth0'))
        self.stack.enter_context(patch.object(network, 'boot_id', return_value='boot-one'))
        self.stack.enter_context(patch.object(network, 'ARCHIVES', self.root / 'archives'))

    def reset(self, kind=None):
        self.rows = [dict(self.default, kind=kind or self.default['kind'])]
        self.classes, self.filters = {}, []

    @staticmethod
    def shown(text):
        kbit = int(text[:-4])
        return f'{kbit // 1000}Mbit' if kbit % 1000 == 0 else f'{kbit}Kbit'

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
        if args[:3] == ('tc', '-j', 'qdisc'):
            return json.dumps(self.rows)
        if args[:3] == ('tc', '-j', 'class'):
            return self.class_text()  # iproute2 ignores -j for HTB classes
        if args[:3] == ('tc', 'class', 'show'):
            return self.class_text()
        if args[:3] == ('tc', '-j', 'filter'):
            return json.dumps(self.filters if 'parent' in args else [])
        if args[:3] == ('tc', 'qdisc', 'replace') and 'htb' in args:
            self.rows = [{'kind': 'htb', 'handle': network.SHARE_HANDLE, 'root': True,
                          'options': {'r2q': 10, 'default': '0xff'}}]
            self.classes, self.filters = {}, []
            return ''
        if args[:2] == ('tc', 'class') and args[2] in ('add', 'change'):
            classid = args[args.index('classid') + 1]
            if args[2] == 'change' and classid not in self.classes:
                raise RuntimeError('Command failed: tc')
            self.classes[classid] = (self.shown(args[args.index('rate') + 1]), self.shown(args[args.index('ceil') + 1]))
            return ''
        if args[:3] == ('tc', 'qdisc', 'add'):
            self.rows.append({'kind': 'fq_codel', 'handle': args[args.index('handle') + 1],
                              'parent': args[args.index('parent') + 1], 'options': {}})
            return ''
        if args[:3] == ('tc', 'filter', 'add'):
            self.filters.append({'protocol': args[args.index('protocol') + 1],
                                 'options': {'flowid': args[args.index('flowid') + 1],
                                             'sport': args[args.index('sport') + 1]}})
            return ''
        if args[:3] == ('tc', 'qdisc', 'del'):
            self.reset(self.params['net.core.default_qdisc'])
            return ''
        raise AssertionError(args)

    def class_text(self):
        return '\n'.join(f'class htb {c} parent 1af2:1 prio 0 rate {r} ceil {x} burst 1593b cburst 1579b'
                         for c, (r, x) in self.classes.items())

    def test_one_class_per_url_with_guarantee_and_borrowing(self):
        state = network.apply(self.path, plan=plan(caps=(95, 20, 95)))
        self.assertEqual(state['shaping']['result'], 'applied')
        self.assertEqual(self.rows[0]['kind'], 'htb')
        self.assertEqual(self.classes['1af2:11'], ('31033Kbit', '95Mbit'))  # (95000-1900)/3
        self.assertEqual(self.classes['1af2:12'], ('31033Kbit', '20Mbit'), 'cap follows the URL')
        self.assertEqual(self.classes['1af2:ff'], ('1900Kbit', '95Mbit'))
        ports = sorted((f['options']['sport'], f['options']['flowid']) for f in self.filters)
        self.assertEqual(ports[:2], [('20001', '1af2:11'), ('20001', '1af2:11')], 'IPv4 and IPv6 filters')
        self.assertEqual(len(self.filters), 6)
        self.assertTrue(network.verify_health(self.path)['fair_share']['tree_matches'])
        quanta = {c[c.index('quantum') + 1] for c in self.commands if c[:3] == ('tc', 'class', 'add')}
        self.assertEqual(quanta, {network.QUANTUM}, 'byte-fair round robin under GSO')
        self.assertGreater(int(network.QUANTUM), 65536)

    def test_restore_returns_kernel_default(self):
        network.apply(self.path, plan=plan())
        network.restore(self.path)
        self.assertEqual(self.rows[0]['kind'], 'fq_codel')
        self.assertEqual(json.loads(self.path.read_text())['shaping']['result'], 'restored')

    def test_custom_root_left_alone(self):
        self.rows = [{'kind': 'htb', 'handle': '1:', 'root': True, 'options': {}}]
        state = network.apply(self.path, plan=plan())
        self.assertEqual(state['shaping']['result'], 'skipped-custom-queue')
        self.assertEqual(self.rows[0]['handle'], '1:')
        self.assertTrue(any('not applied' in w for w in network.health(self.path)['warnings']))

    def test_missing_modules_fall_back(self):
        self.missing = ['sch_htb']
        state = network.apply(self.path, plan=plan())
        self.assertEqual(state['shaping']['result'], 'modules-unavailable')
        self.assertEqual(self.rows[0]['kind'], 'fq_codel')

    def test_rate_and_cap_change_in_place(self):
        network.apply(self.path, plan=plan())
        network.set_plan(self.path, plan(rate=190, caps=(190, 50, 190)))
        self.assertEqual(self.classes['1af2:1'], ('190Mbit', '190Mbit'))
        self.assertEqual(self.classes['1af2:12'][1], '50Mbit')
        self.assertTrue(network.verify_health(self.path)['fair_share']['tree_matches'])
        with self.assertRaisesRegex(RuntimeError, 'ports differ'):
            changed = plan()
            changed['users'][0]['port'] = 29999
            network.set_plan(self.path, changed)

    def test_external_change_blocks_change_and_restore(self):
        network.apply(self.path, plan=plan())
        self.classes['1af2:12'] = ('1Mbit', '1Mbit')
        with self.assertRaisesRegex(RuntimeError, 'outside'):
            network.set_plan(self.path, plan(rate=50))
        with self.assertRaisesRegex(RuntimeError, 'tree verification failed'):
            network.verify_health(self.path)
        self.rows = [{'kind': 'htb', 'handle': '1:', 'root': True, 'options': {}}]
        with self.assertRaisesRegex(RuntimeError, 'outside installer'):
            network.check_restore(json.loads(self.path.read_text()))

    def test_boot_reapplies_after_reboot_only_on_default_root(self):
        network.apply(self.path, plan=plan())
        self.reset('fq')  # reboot: tc state gone
        network.boot_fq('eth0', self.path)
        self.assertEqual(self.rows[0]['kind'], 'htb')
        self.assertTrue(network.health(self.path)['fair_share']['tree_matches'])
        self.reset('fq')
        self.missing = ['sch_htb']  # kernel updated without extra modules
        network.boot_fq('eth0', self.path)
        self.assertEqual(self.rows[0]['kind'], 'fq')
        self.rows = [{'kind': 'htb', 'handle': '1:', 'root': True, 'options': {}}]
        self.missing = []
        network.boot_fq('eth0', self.path)
        self.assertEqual(self.rows[0]['handle'], '1:', 'never overwrite an administrator queue')

    def test_fq_repair_refused_while_shaping(self):
        network.apply(self.path, plan=plan())
        with self.assertRaisesRegex(RuntimeError, 'fair share manages'):
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

    def test_status_shows_gb(self):
        self.assertEqual(render.format_gb(10**12), '1000 GB')
        self.assertEqual(render.format_gb(312_400_000_000), '312.4 GB')
        self.assertEqual(render.format_gb(12_340_000_000), '12.3 GB')
        self.assertEqual(render.format_gb(0), '0 GB')
        summary = vpn.traffic_summary({'month': '2026-10', 'used_bytes': 312_400_000_000,
                                       'quota_bytes': 10**12, 'percent': 31.2, 'metering': 'both',
                                       'on_exceed': 'stop', 'paused_month': None})
        self.assertEqual(summary['used'], '312.4 GB / 1000 GB（31.2%）')
        self.assertEqual(summary['paused'], '否')
        self.assertEqual(vpn.traffic_summary({'month': '2026-10', 'used_bytes': 5 * 10**9, 'quota_bytes': 0,
                                              'percent': None})['used'], '5 GB（未设置上限）')

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
        raw.pop('fair_share')  # derive it from the user count, as a fresh settings file would
        self.stack.enter_context(patch.object(vpn.network, 'missing_modules', return_value=[]))
        self.s = render.settings(raw)
        self.assertTrue(self.s['fair_share'])

    def test_each_user_has_served_folder(self):
        vpn.install(self.s, '192.0.2.10')
        for name in ('alice', 'bob', 'carol'):
            folder = vpn.CONFIG / 'subscriptions' / name
            self.assertEqual(folder.stat().st_mode & 0o777, 0o750)
            self.assertTrue((folder / 'normal.yaml').is_file())
        self.assertFalse((vpn.CONFIG / 'subscriptions/normal.yaml').exists())

    def test_missing_modules_install_extra_package_and_official_source(self):
        missing = [['sch_htb'], []]  # before and after installing the extra package
        policy = SimpleNamespace(returncode=0, stdout='linux-modules-extra-x:\n  Candidate: (none)\n')
        def run(args, **kwargs):
            return policy if args[:2] == ['apt-cache', 'policy'] else SimpleNamespace(returncode=0, stdout='')
        self.commands.side_effect = run
        with patch.object(vpn.network, 'missing_modules', side_effect=lambda: missing.pop(0)), \
                patch.object(vpn, 'KERNEL_SOURCE', self.root / 'gowest-ubuntu-kernel.sources'), \
                patch.object(vpn.platform, 'release', return_value='6.8.0-1012-aws'):
            vpn.install(self.s, '192.0.2.10')
            source = (self.root / 'gowest-ubuntu-kernel.sources').read_text()
            self.assertIn('noble-updates', source)
            self.assertIn('ubuntu-archive-keyring.gpg', source)
            calls = [c.args[0] for c in self.commands.call_args_list]
            self.assertIn(['apt-get', 'install', '-y', '--no-install-recommends',
                           'linux-modules-extra-6.8.0-1012-aws'], calls)
            self.assertEqual(vpn.load_state()['share_modules'], 'installed')
            vpn.rollback()
            self.assertFalse((self.root / 'gowest-ubuntu-kernel.sources').exists(), 'source archived on rollback')

    def test_cap_change_updates_settings(self):
        vpn.install(self.s, '192.0.2.10')
        with patch.object(vpn.network, 'health', return_value={'fair_share': {'result': 'applied'}}), \
                patch.object(vpn.network, 'set_plan') as set_plan, \
                contextlib.redirect_stdout(io.StringIO()):
            vpn.change_bandwidth(None, '192.0.2.10', 'bob', '20Mbps')
        self.assertEqual(set_plan.call_args.args[1]['users'][1]['cap_mbps'], 20)
        self.assertEqual(json.loads((vpn.CONFIG / 'settings.json').read_text())['user_caps'], {'bob': 20})
        with self.assertRaisesRegex(RuntimeError, '没有这个订阅用户'):
            vpn.change_bandwidth(None, '192.0.2.10', 'nobody', '5Mbps')

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
