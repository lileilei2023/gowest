"""Per-URL management after install: add/remove/reset, quotas, expiry, counters."""
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounting  # noqa: E402
import render  # noqa: E402
import subinfo  # noqa: E402
import unlock  # noqa: E402
import vpn  # noqa: E402
import test_kit  # noqa: E402

GB = 10**9


def shared_settings(**extra):
    raw = dict(test_kit.example(), users=['alice', 'bob', 'carol'], **extra)
    raw['subscription'] = {'scheme': 'http', 'host': '192.0.2.10', 'port': 80, 'allow_insecure_http': True}
    raw.pop('fair_share', None)
    return render.settings(raw)


class SettingsTests(unittest.TestCase):
    def test_pinned_ports_survive_a_removal(self):
        s = shared_settings(user_ports={'alice': 20001, 'carol': 20003, 'bob': 20002})
        self.assertEqual(render.proxy_ports(s), [20001, 20002, 20003])
        s = render.settings(dict(s, users=['alice', 'carol'], user_ports={'alice': 20001, 'carol': 20003}))
        self.assertEqual(render.proxy_ports(s), [20001, 20003])
        self.assertEqual(render.free_port(s), 20002)

    def test_pins_must_cover_every_user_and_be_unique(self):
        with self.assertRaises(ValueError):
            shared_settings(user_ports={'alice': 20001})
        with self.assertRaises(ValueError):
            shared_settings(user_ports={'alice': 20001, 'bob': 20001, 'carol': 20003})

    def test_quota_and_expiry_validation(self):
        s = shared_settings(user_quota={'bob': 100 * GB}, user_expire={'carol': '2026-12-31'})
        self.assertEqual(s['user_quota'], {'bob': 100 * GB})
        with self.assertRaises(ValueError):
            shared_settings(user_quota={'dave': GB})
        with self.assertRaises(ValueError):
            shared_settings(user_expire={'bob': 'next year'})
        self.assertEqual(render.parse_date('2026/1/5'.replace('/1/', '/01/').replace('/5', '/05')), '2026-01-05')

    def test_one_named_user_may_keep_fair_share(self):
        s = render.settings(dict(shared_settings(), users=['alice'], fair_share=True))
        self.assertTrue(s['fair_share'])

    def test_each_url_includes_its_own_header(self):
        s = shared_settings()
        keys = {'reality_private_key': 'k', 'reality_public_key': 'p', 'short_id': 'ab', 'obfs_password': 'o',
                'certificate_fingerprint': 'f',
                'users': [dict(render.new_credentials(), name=n) for n in s['users']]}
        text = render.nginx_config(s, keys)
        for name in s['users']:
            self.assertIn(f'include /etc/la-vpn/subinfo.d/{name}.con[f];', text)
        self.assertNotIn('include /etc/la-vpn/subinfo.conf;', text)


class AccountingTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'user-traffic.json'
        self.ports = {'alice': 20001, 'bob': 20002}
        self.applied = []
        self.now = time.mktime((2026, 10, 15, 12, 0, 0, 0, 0, -1))

    def sync(self, live, s=None, now=None):
        s = s or shared_settings()
        return accounting.sync(s, self.ports, self.path, now or self.now, live, self.applied.append)

    def test_ruleset_counts_both_directions_and_drops_before_counting(self):
        text = accounting.ruleset(self.ports, {'bob'})
        self.assertIn('th dport 20001 counter name "p20001_in"', text)
        self.assertIn('th sport 20002 counter name "p20002_out"', text)
        self.assertLess(text.index('th dport 20002 drop'), text.index('th dport 20002 counter'))
        self.assertNotIn('20001 drop', text)
        self.assertIn('policy accept', text)

    def test_counters_accumulate_across_reboots_and_reset_monthly(self):
        first = self.sync({})
        self.assertEqual(len(self.applied), 1, 'missing table is created')
        self.assertEqual(first['users']['alice']['upload'], 0)
        self.sync({20001: [100, 1000], 20002: [5, 50]})
        record = self.sync({20001: [150, 3000], 20002: [5, 50]})
        self.assertEqual((record['users']['alice']['upload'], record['users']['alice']['download']), (150, 3000))
        record = self.sync({20001: [10, 20], 20002: [0, 0]})  # rebooted: counters restarted
        self.assertEqual(record['users']['alice']['download'], 3020)
        self.assertEqual(len(self.applied), 1, 'unchanged spec keeps the live table')
        next_month = time.mktime((2026, 11, 1, 0, 10, 0, 0, 0, -1))
        record = self.sync({20001: [30, 40], 20002: [0, 0]}, now=next_month)
        self.assertEqual((record['users']['alice']['upload'], record['users']['alice']['download']), (20, 20))

    def test_quota_pauses_only_that_url_and_lifts_when_raised(self):
        s = shared_settings(user_quota={'bob': 1000})
        self.sync({}, s)
        record = self.sync({20001: [10**6, 10**6], 20002: [400, 700]}, s)
        self.assertEqual(record['users']['bob']['paused'], 'quota')
        self.assertEqual(record['users']['alice']['paused'], '')
        self.assertIn('20002 drop', self.applied[-1])
        self.assertNotIn('20001 drop', self.applied[-1])
        raised = shared_settings(user_quota={'bob': 10**6})
        record = self.sync({20001: [0, 0], 20002: [0, 0]}, raised)
        self.assertEqual(record['users']['bob']['paused'], '')
        self.assertNotIn('drop', self.applied[-1])

    def test_expiry_ends_with_the_named_day(self):
        s = shared_settings(user_expire={'alice': '2026-10-15'})
        self.assertEqual(self.sync({}, s)['users']['alice']['paused'], '')
        after = time.mktime((2026, 10, 16, 0, 0, 1, 0, 0, -1))
        self.assertEqual(self.sync({}, s, after)['users']['alice']['paused'], 'expired')

    def test_headers_use_each_urls_own_quota(self):
        s = shared_settings(user_quota={'bob': 100 * GB}, user_expire={'bob': '2026-12-31'})
        record = {'users': {'alice': {'upload': 1, 'download': 2}, 'bob': {'upload': 3, 'download': 4},
                            'carol': {'upload': 0, 'download': 0}}}
        headers = subinfo.user_headers(s, record, (500, 600, '2026-10'))
        self.assertIn(f'upload=3; download=4; total={100 * GB}; expire=', headers['bob'])
        self.assertIn('upload=600; download=500', headers['alice'])
        self.assertEqual(subinfo.user_headers(s, record, None).keys(), {'bob'})


class UnlockTests(unittest.TestCase):
    def answers(self, table):
        return patch.object(unlock, 'fetch', side_effect=lambda url, follow=False: table.get(url, (200, url, '')))

    def test_claude_region_and_ip_block(self):
        with self.answers({'https://claude.ai/': (302, 'https://www.anthropic.com/app-unavailable-in-region', '')}):
            self.assertEqual(unlock.claude()[0], 'fail')
        with self.answers({'https://claude.ai/': (403, '', '')}):
            self.assertEqual(unlock.claude()[0], 'warn')
        with self.answers({'https://claude.ai/': (302, 'https://claude.ai/login', '')}):
            self.assertEqual(unlock.claude(), ('ok', '可用'))

    def test_chatgpt_signals(self):
        rules = 'https://api.openai.com/compliance/cookie_requirements'
        with self.answers({rules: (200, rules, '{"cf_details":"unsupported_country"}')}):
            self.assertEqual(unlock.chatgpt()[0], 'fail')
        with self.answers({'https://ios.chat.openai.com/': (403, '', 'blocked: VPN detected')}):
            self.assertEqual(unlock.chatgpt()[0], 'warn')

    def test_tiktok_region(self):
        with self.answers({'https://www.tiktok.com/': (200, '', '..."region":"US"...')}):
            self.assertEqual(unlock.tiktok(), ('ok', '可用，识别地区 US'))
        with self.answers({'https://www.tiktok.com/': (0, '', '')}):
            self.assertEqual(unlock.tiktok()[0], 'fail')


class UsersTransactionTests(unittest.TestCase):
    def setUp(self):
        test_kit.TransactionTests.setUp(self)
        self.stack.enter_context(patch.object(vpn.network, 'missing_modules', return_value=[]))
        self.stack.enter_context(patch.object(vpn.network, 'health', return_value={'fair_share': {'result': 'skipped'}}))
        self.firewall = self.stack.enter_context(patch.object(vpn.hostfirewall, 'add_ports', return_value=False))
        self.s = shared_settings()
        vpn.install(self.s, '192.0.2.10')
        self.commands.reset_mock()

    def served(self, name, file='normal.yaml'):
        return (vpn.CONFIG / 'subscriptions' / name / file)

    def url(self, name):
        return (vpn.STATE / 'generated/users' / name / 'subscription-urls.txt').read_text()

    def restarted(self):
        return [c.args[0] for c in self.commands.call_args_list if c.args[0][:2] == ['systemctl', 'restart']]

    def cfg(self):
        return json.loads((vpn.CONFIG / 'settings.json').read_text())

    def assert_digests_match(self):
        for file, digest in vpn.load_state()['files'].items():
            self.assertEqual(vpn.sha(file), digest, file)

    def test_add_gets_a_new_port_and_leaves_others_untouched(self):
        before = {n: self.served(n).read_bytes() for n in ('alice', 'bob', 'carol')}
        vpn.change_users('192.0.2.10', 'add', 'dave', quota='100', expire='2026-12-31')
        cfg = self.cfg()
        self.assertEqual(cfg['user_ports'], {'alice': 20001, 'bob': 20002, 'carol': 20003, 'dave': 20004})
        self.assertEqual(cfg['user_quota'], {'dave': 100 * GB})
        self.assertTrue(self.served('dave').is_file())
        self.assertIn('/sub/', self.url('dave'))
        for name, data in before.items():
            self.assertEqual(self.served(name).read_bytes(), data)
        self.assertEqual(self.restarted(), [['systemctl', 'restart', 'la-vpn.service', 'la-vpn-subscription.service']])
        self.firewall.assert_called_once()
        self.assert_digests_match()

    def test_remove_keeps_everyone_elses_port_and_url(self):
        carol = (self.url('carol'), self.served('carol').read_bytes())
        vpn.change_users('192.0.2.10', 'remove', 'bob')
        cfg = self.cfg()
        self.assertEqual(cfg['users'], ['alice', 'carol'])
        self.assertEqual(cfg['user_ports'], {'alice': 20001, 'carol': 20003})
        self.assertEqual((self.url('carol'), self.served('carol').read_bytes()), carol)
        self.assertFalse((vpn.CONFIG / 'subscriptions/bob').exists())
        self.assertFalse((vpn.STATE / 'generated/users/bob').exists())
        self.assertNotIn('bob', [u['name'] for u in vpn.load_keys()['users']])
        self.assert_digests_match()
        # The freed port goes to the next new URL.
        vpn.change_users('192.0.2.10', 'add', 'erin')
        self.assertEqual(self.cfg()['user_ports']['erin'], 20002)

    def test_reset_replaces_only_that_link(self):
        alice, bob = self.url('alice'), self.url('bob')
        vpn.change_users('192.0.2.10', 'reset', 'bob')
        self.assertEqual(self.url('alice'), alice)
        self.assertNotEqual(self.url('bob'), bob)
        self.assertEqual(self.cfg()['user_ports']['bob'], 20002)
        self.assert_digests_match()

    def test_quota_and_expiry_change_without_a_proxy_restart(self):
        vpn.change_users('192.0.2.10', 'set', 'bob', quota='50GB', expire='2027-01-31')
        cfg = self.cfg()
        self.assertEqual((cfg['user_quota'], cfg['user_expire']), ({'bob': 50 * GB}, {'bob': '2027-01-31'}))
        self.assertEqual(self.restarted(), [])
        vpn.change_users('192.0.2.10', 'set', 'bob', quota='0', expire='none')
        self.assertEqual((self.cfg()['user_quota'], self.cfg()['user_expire']), ({}, {}))

    def test_failed_start_restores_files_keys_and_urls(self):
        keys = (vpn.STATE / 'generated/secrets.json').read_bytes()
        bob = self.url('bob')
        settings_before = (vpn.CONFIG / 'settings.json').read_bytes()
        self.verification.side_effect = RuntimeError('simulated start failure')
        with self.assertRaisesRegex(RuntimeError, 'simulated'):
            vpn.change_users('192.0.2.10', 'reset', 'bob')
        self.assertEqual((vpn.STATE / 'generated/secrets.json').read_bytes(), keys)
        self.assertEqual(self.url('bob'), bob)
        self.assertEqual((vpn.CONFIG / 'settings.json').read_bytes(), settings_before)
        self.assert_digests_match()
        with self.assertRaisesRegex(RuntimeError, 'simulated'):
            vpn.change_users('192.0.2.10', 'add', 'dave')
        self.assertFalse((vpn.CONFIG / 'subscriptions/dave').exists())
        self.assertFalse((vpn.STATE / 'generated/users/dave').exists())
        self.assert_digests_match()

    def test_refusals(self):
        with self.assertRaisesRegex(RuntimeError, '没有这个订阅用户'):
            vpn.change_users('192.0.2.10', 'remove', 'zed')
        with self.assertRaisesRegex(RuntimeError, '已经有这个名字'):
            vpn.change_users('192.0.2.10', 'add', 'alice')
        with self.assertRaisesRegex(RuntimeError, '名字只能'):
            vpn.change_users('192.0.2.10', 'add', 'Bad Name')
        with self.assertRaisesRegex(RuntimeError, 'confirm-server'):
            vpn.change_users('198.51.100.1', 'add', 'dave')
        target = self.served('alice')
        target.write_text(target.read_text() + '#')
        with self.assertRaisesRegex(RuntimeError, 'changed externally'):
            vpn.change_users('192.0.2.10', 'reset', 'alice')


class SingleUserRefusalTests(unittest.TestCase):
    def setUp(self):
        test_kit.TransactionTests.setUp(self)
        vpn.install(self.s, '192.0.2.10')

    def test_single_install_cannot_add_urls(self):
        with self.assertRaisesRegex(RuntimeError, '单人安装'):
            vpn.change_users('192.0.2.10', 'add', 'dave')


if __name__ == '__main__':
    unittest.main()
