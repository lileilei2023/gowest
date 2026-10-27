"""Port hopping, TUIC / AnyTLS, obfuscation switch and the line check."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounting  # noqa: E402
import check  # noqa: E402
import firewall  # noqa: E402
import gowest_ui as ui  # noqa: E402
import hostfirewall  # noqa: E402
import linecheck  # noqa: E402
import network  # noqa: E402
import render  # noqa: E402
import vpn  # noqa: E402
import test_kit  # noqa: E402


def raw(users=('alice', 'bob', 'carol'), **extra):
    r = dict(test_kit.example(), users=list(users), extra_protocols=['tuic', 'anytls'], port_hopping=True)
    r.update(extra)
    r['subscription'] = {'scheme': 'http', 'host': '192.0.2.10', 'port': 80, 'allow_insecure_http': True}
    r.pop('fair_share', None)
    return r


class LayoutTests(unittest.TestCase):
    def test_every_url_gets_main_alt_and_an_aligned_hop_block(self):
        s = render.settings(raw())
        self.assertEqual(render.port_layout(s)['bob'], {'main': 20002, 'alt': 21002, 'hop': (24640, 24703)})
        self.assertEqual(render.cloud_ports(s), ['TCP/UDP 20001-20003', 'TCP/UDP 21001-21003', 'UDP 24576-24767', 'TCP 80'])
        single = render.settings(raw(users=()))
        self.assertEqual(render.port_layout(single)[''], {'main': 443, 'alt': 2443, 'hop': (24576, 24639)})
        self.assertEqual(render.cloud_ports(single), ['TCP/UDP 443', 'TCP/UDP 2443', 'UDP 24576-24639', 'TCP 80'])

    def test_older_settings_keep_their_ports(self):
        s = render.settings(dict(raw(), extra_protocols=[], port_hopping=False))
        self.assertEqual(render.cloud_ports(s), ['TCP/UDP 20001-20003', 'TCP 80'])
        old = test_kit.example()
        old.pop('fair_share', None)
        s = render.settings(old)
        self.assertEqual((s['extra_protocols'], s['port_hopping'], s['hy2_obfs']), ([], False, True))

    def test_invalid_combinations_are_refused(self):
        with self.assertRaises(ValueError):
            render.settings(raw(extra_protocols=['wireguard']))
        with self.assertRaises(ValueError):  # hop blocks would run past the ephemeral range
            render.settings(raw(user_ports={'alice': 20001, 'bob': 20002, 'carol': 20200}))
        with self.assertRaises(ValueError):  # second ports would land on someone's main port
            render.settings(raw(user_port_base=20000, user_ports={'alice': 20001, 'bob': 21001, 'carol': 20003}))

    def test_hop_redirects_only_in_gowests_own_table(self):
        s = render.settings(raw())
        text = render.hop_ruleset(s)
        self.assertIn('udp dport 24640-24703 redirect to :20002', text)
        self.assertEqual(text.count('table inet la_vpn_hop'), 3)
        self.assertNotIn('drop', text)
        off = render.hop_ruleset(render.settings(dict(raw(), port_hopping=False)))
        self.assertEqual(off, 'table inet la_vpn_hop\ndelete table inet la_vpn_hop\n')


class ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = render.render(raw(), Path(cls.tmp.name) / 'gen')
        cls.bob = yaml.safe_load((cls.out / 'users/bob/normal.yaml').read_text())
        cls.server = json.loads((cls.out / 'server.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_cross_checks_pass_with_everything_on(self):
        self.assertEqual(check.check(self.out)['cross_config_checks'], 'passed')

    def test_four_inbounds_per_url_on_two_ports(self):
        tags = [i['tag'] for i in self.server['inbounds'] if i['tag'].endswith('-bob')]
        self.assertEqual(tags, ['vless-in-bob', 'hy2-in-bob', 'tuic-in-bob', 'anytls-in-bob'])
        ports = {i['tag']: i['listen_port'] for i in self.server['inbounds'] if i['tag'].endswith('-bob')}
        self.assertEqual(ports, {'vless-in-bob': 20002, 'hy2-in-bob': 20002, 'tuic-in-bob': 21002, 'anytls-in-bob': 21002})

    def test_every_tls_node_is_pinned_and_failover_never_goes_direct(self):
        fingerprints = {p['name']: p.get('fingerprint') for p in self.bob['proxies'] if p['type'] != 'vless'}
        self.assertEqual(set(fingerprints), {'LA-Hy2', 'LA-TUIC', 'LA-AnyTLS'})
        self.assertEqual(len(set(fingerprints.values()) - {None}), 1)
        groups = {g['name']: g for g in self.bob['proxy-groups']}
        self.assertEqual(groups[render.AUTO_GROUP]['proxies'], ['LA-Hy2', 'LA-VLESS', 'LA-TUIC', 'LA-AnyTLS'])
        for name, group in groups.items():
            if name != render.SELECT_GROUP:
                self.assertNotIn('DIRECT', group['proxies'], name)

    def test_hy2_hops_inside_its_own_block(self):
        hy = next(p for p in self.bob['proxies'] if p['type'] == 'hysteria2')
        self.assertEqual((hy['port'], hy['ports'], hy['hop-interval']), (20002, '24640-24703', 30))

    def test_unpinned_extra_node_is_caught(self):
        with tempfile.TemporaryDirectory() as directory:
            out = render.render(raw(), Path(directory) / 'gen')
            path = out / 'users/alice/normal.yaml'
            cfg = yaml.safe_load(path.read_text())
            next(p for p in cfg['proxies'] if p['type'] == 'tuic').pop('fingerprint')
            path.write_text(yaml.safe_dump(cfg, allow_unicode=True))
            with self.assertRaises((AssertionError, KeyError)):
                check.check(out)

    def test_shadowrocket_only_gets_pinnable_protocols(self):
        text = (self.out / 'users/bob/shadowrocket.conf').read_text()
        self.assertNotIn('TUIC', text)
        self.assertNotIn('AnyTLS', text)
        nodes = (self.out / 'users/bob/nodes.txt').read_text()
        import base64
        self.assertEqual(len(base64.b64decode(nodes).decode().splitlines()), 2)

    def test_obfs_switch_changes_both_sides(self):
        with tempfile.TemporaryDirectory() as directory:
            out = render.render(raw(hy2_obfs=False), Path(directory) / 'gen')
            self.assertEqual(check.check(out)['cross_config_checks'], 'passed')
            server = json.loads((out / 'server.json').read_text())
            self.assertFalse(any('obfs' in i for i in server['inbounds']))


class NetworkTests(unittest.TestCase):
    def test_share_matches_main_alt_and_hop_block(self):
        plan = render.share_plan(render.settings(raw()))
        bob = plan['users'][1]
        self.assertEqual(network.port_matches(bob), [(20002, 0xffff), (21002, 0xffff), (24640, 0xffc0)])
        calls = []
        with patch.object(network, 'run', side_effect=lambda *a, **k: calls.append(a) or ''):
            network.build_tree('eth0', plan)
        filters = [c for c in calls if c[1] == 'filter']
        self.assertEqual(len(filters), 3 * 3 * 2)  # users x matches x (IPv4, IPv6)
        self.assertIn(('tc', 'filter', 'add', 'dev', 'eth0', 'parent', '1af2:', 'protocol', 'ip', 'prio', '1', 'u32',
                       'match', 'ip', 'sport', '24640', '0xffc0', 'flowid', '1af2:12'), calls)

    def test_misaligned_hop_block_is_refused(self):
        with self.assertRaises(ValueError):
            network.port_matches({'port': 20001, 'hop': [24577, 24640]})

    def test_counters_cover_both_ports_of_a_url(self):
        text = accounting.ruleset({'alice': [20001, 21001], 'bob': [20002, 21002]}, {'bob'})
        self.assertIn('th dport { 20001, 21001 } counter name "p20001_in"', text)
        self.assertIn('th sport { 20002, 21002 } counter name "p20002_out"', text)
        self.assertIn('th dport { 20002, 21002 } drop', text)
        self.assertNotIn('20001, 21001 } drop', text)


class FirewallTests(unittest.TestCase):
    def test_ranges_are_opened_and_overlaps_detected(self):
        s = render.settings(raw())
        pairs = hostfirewall.required_ports(s)
        self.assertIn(('udp', (24640, 24703)), pairs)
        self.assertIn(('tcp', 21002), pairs)
        self.assertEqual(hostfirewall.rule_args('udp', (24640, 24703), 't')[9], '24640:24703')
        with tempfile.TemporaryDirectory() as directory:
            rules = Path(directory) / 'user.rules'
            rules.write_text('### tuple ### allow udp 24700 0.0.0.0/0 any 0.0.0.0/0 in\n')
            with patch.object(hostfirewall, 'UFW_FILES', (rules,)):
                self.assertTrue(hostfirewall.overlaps_existing('udp', (24640, 24703)))
                self.assertFalse(hostfirewall.overlaps_existing('udp', (24576, 24639)))

    def test_cloud_template_lists_ranges(self):
        doc = firewall.generate(raw(), ['198.51.100.1/32'])
        ports = [x['协议及端口'] for x in doc['data']]
        self.assertIn('UDP:24576-24639', ports)
        self.assertIn('TCP:21001', ports)
        self.assertTrue(firewall.check(raw(), doc)['valid'])


class OptionsTransactionTests(unittest.TestCase):
    def setUp(self):
        test_kit.TransactionTests.setUp(self)
        self.stack.enter_context(patch.object(vpn.network, 'missing_modules', return_value=[]))
        self.stack.enter_context(patch.object(vpn.network, 'health', return_value={'fair_share': {'result': 'skipped'}}))
        self.firewall = self.stack.enter_context(patch.object(vpn.hostfirewall, 'add_ports', return_value=True))
        self.stack.enter_context(patch.object(vpn, 'ensure_speedtest'))
        self.s = render.settings(raw())
        vpn.install(self.s, '192.0.2.10')
        self.commands.reset_mock()

    def calls(self):
        return [c.args[0] for c in self.commands.call_args_list]

    def test_install_loads_hop_redirects_before_the_proxy(self):
        self.assertIn('redirect to :20001', (vpn.CONFIG / 'hop.nft').read_text())
        self.assertTrue((vpn.UNITS / 'la-vpn-hop.service').is_file())
        self.assertIn('Before=la-vpn.service', (vpn.UNITS / 'la-vpn-hop.service').read_text())

    def test_turning_hopping_off_reloads_redirects_without_restarting_the_proxy(self):
        vpn.change_options('192.0.2.10', hopping=False)
        self.assertNotIn('redirect', (vpn.CONFIG / 'hop.nft').read_text())
        self.assertIn(['systemctl', 'restart', 'la-vpn-hop.service'], self.calls())
        self.assertNotIn('la-vpn.service', [c[-1] for c in self.calls() if c[:2] == ['systemctl', 'restart']])
        self.firewall.assert_not_called()  # nothing new to open
        for file, digest in vpn.load_state()['files'].items():
            self.assertEqual(vpn.sha(file), digest, file)

    def test_dropping_a_protocol_restarts_the_proxy_and_keeps_urls(self):
        before = (vpn.STATE / 'generated/users/alice/subscription-urls.txt').read_text()
        vpn.change_options('192.0.2.10', protocols=['tuic'])
        server = json.loads((vpn.CONFIG / 'server.json').read_text())
        self.assertFalse(any(i['type'] == 'anytls' for i in server['inbounds']))
        # Only the proxy changed; the subscription server's config did not.
        self.assertIn(['systemctl', 'restart', 'la-vpn.service'], self.calls())
        self.assertEqual((vpn.STATE / 'generated/users/alice/subscription-urls.txt').read_text(), before)

    def test_enabling_opens_only_the_new_ports(self):
        vpn.change_options('192.0.2.10', protocols=[])
        self.firewall.reset_mock()
        vpn.change_options('192.0.2.10', protocols=['anytls'])
        pairs = self.firewall.call_args.args[3]
        self.assertEqual(sorted(pairs), [('tcp', 21001), ('tcp', 21002), ('tcp', 21003),
                                         ('udp', 21001), ('udp', 21002), ('udp', 21003)])


class LineCheckTests(unittest.TestCase):
    def test_route_classification(self):
        self.assertEqual(linecheck.classify(['192.168.1.1', '59.43.1.1', '59.43.2.2'])[0], 'CN2 GIA')
        self.assertEqual(linecheck.classify(['202.97.1.1', '59.43.1.1'])[0], 'CN2 GT')
        self.assertEqual(linecheck.classify(['202.97.33.1', '202.97.90.1'])[0], '163')
        self.assertEqual(linecheck.classify(['219.158.1.1'])[0], '4837')
        self.assertEqual(linecheck.classify(['218.105.1.1'])[0], '9929')
        self.assertEqual(linecheck.classify(['223.120.1.1'])[0], 'CMI')
        self.assertEqual(linecheck.classify(['8.8.8.8', ''])[0], '')

    def test_latency_counts_failures_as_loss(self):
        outcomes = iter([OSError(), None, None, None])

        def connect(*a, **k):
            outcome = next(outcomes)
            if outcome:
                raise outcome
            return type('S', (), {'close': lambda self: None})()
        with patch.object(linecheck.socket, 'create_connection', side_effect=connect), \
                patch.object(linecheck.time, 'sleep'):
            result = linecheck.latency('203.0.113.7', 443, count=4)
        self.assertEqual(result['loss'], 25.0)

    def test_traceroute_output_parsing(self):
        out = (' 1  192.168.1.1  1.2 ms\n 2  *\n 3  59.43.246.1  20.1 ms\n'
               'traceroute to 203.0.113.7 (203.0.113.7), 30 hops max\n')
        with patch.object(linecheck.shutil, 'which', return_value='/usr/sbin/traceroute'), \
                patch.object(linecheck.platform, 'system', return_value='Darwin'), \
                patch.object(linecheck.subprocess, 'run', return_value=type('P', (), {'stdout': out})()):
            self.assertEqual(linecheck.trace('203.0.113.7'), ['192.168.1.1', '', '59.43.246.1'])


class UiTests(unittest.TestCase):
    SERVER = {'id': 'a', 'name': 'LA', 'target': 'root@203.0.113.7', 'ip': '203.0.113.7'}

    def test_options_command(self):
        with patch.object(ui, 'remote', return_value='ok') as remote:
            ui.api_options(self.SERVER, {'hopping': True, 'protocols': ['tuic', 'evil;id']})
        command = remote.call_args.args[1]
        self.assertIn('options --hopping on --protocols tuic --confirm-server 203.0.113.7', command)
        with self.assertRaises(ui.UserError):
            ui.api_options(self.SERVER, {})

    def test_install_can_skip_stability_features(self):
        with patch.object(ui, 'Job') as job:
            ui.api_install(self.SERVER, {'bandwidth': '100Mbps', 'stable': 'off'})
        upload, install = job.return_value.run.call_args.args[0]
        captured = []
        install(type('FakeJob', (), {'stream': lambda self, target, command: captured.append(command)})())
        self.assertIn('quick 100Mbps --no-port-hopping --protocols none', captured[0])


if __name__ == '__main__':
    unittest.main()
