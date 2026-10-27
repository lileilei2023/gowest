"""In-place upgrade: code and configs change, keys and subscription URLs do not."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import render  # noqa: E402
import vpn  # noqa: E402
import test_kit  # noqa: E402


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        # Reuse the simulated install sandbox without re-running its own tests.
        test_kit.TransactionTests.setUp(self)
        vpn.install(self.s, '192.0.2.10')
        self.commands.reset_mock()
        self.secrets = (vpn.STATE / 'generated/secrets.json').read_bytes()
        self.urls = (vpn.STATE / 'generated/subscription-urls.txt').read_text()

    def calls(self):
        return [c.args[0] for c in self.commands.call_args_list]

    def restarted(self):
        return [c for c in self.calls() if c[:2] == ['systemctl', 'restart']]

    def assert_digests_match(self):
        for file, digest in vpn.load_state()['files'].items():
            self.assertEqual(vpn.sha(file), digest, file)

    def test_same_kit_is_a_no_op(self):
        vpn.upgrade('192.0.2.10')
        self.assertEqual(self.restarted(), [])
        self.assertNotIn(['systemctl', 'daemon-reload'], self.calls())
        self.assertEqual(vpn.load_state()['kit_version'], render.KIT_VERSION)

    def test_requires_the_installed_ip(self):
        with self.assertRaisesRegex(RuntimeError, 'confirm-server'):
            vpn.upgrade('198.51.100.1')

    def test_code_only_change_needs_no_proxy_restart(self):
        files = vpn.program_files()
        files[vpn.PROGRAM / 'vpn.py'] += b'\n# newer\n'
        with patch.object(vpn, 'program_files', return_value=files):
            vpn.upgrade('192.0.2.10')
        self.assertTrue((vpn.PROGRAM / 'vpn.py').read_bytes().endswith(b'# newer\n'))
        self.assertEqual(self.restarted(), [])
        self.assert_digests_match()

    def test_new_server_config_restarts_proxy_and_keeps_keys(self):
        original = vpn.server_config
        def newer(cfg, keys):
            config = original(cfg, keys)
            config['log']['level'] = 'error'
            return config
        with patch.object(vpn, 'server_config', side_effect=newer):
            vpn.upgrade('192.0.2.10')
        self.assertEqual(json.loads((vpn.CONFIG / 'server.json').read_text())['log']['level'], 'error')
        self.assertEqual(self.restarted(), [['systemctl', 'restart', 'la-vpn.service']])
        self.assertEqual((vpn.STATE / 'generated/secrets.json').read_bytes(), self.secrets)
        self.assertEqual((vpn.STATE / 'generated/subscription-urls.txt').read_text(), self.urls)
        self.assert_digests_match()

    def test_external_edit_refuses_before_any_change(self):
        target = vpn.CONFIG / 'server.json'
        target.write_text(target.read_text() + ' ')
        files = vpn.program_files()
        files[vpn.PROGRAM / 'vpn.py'] += b'\n# newer\n'
        with patch.object(vpn, 'program_files', return_value=files):
            original = vpn.server_config
            with patch.object(vpn, 'server_config', side_effect=lambda c, k: dict(original(c, k), log={'level': 'error'})):
                with self.assertRaisesRegex(RuntimeError, 'changed externally'):
                    vpn.upgrade('192.0.2.10')
        self.assertFalse((vpn.PROGRAM / 'vpn.py').read_bytes().endswith(b'# newer\n'))
        self.assertEqual([c for c in self.calls() if c[0] != 'apt-get'], [[str(vpn.PROGRAM / 'bin/sing-box'), 'version']])

    def test_failed_start_restores_previous_files(self):
        before = (vpn.CONFIG / 'server.json').read_bytes()
        original = vpn.server_config
        self.verification.side_effect = RuntimeError('simulated start failure')
        with patch.object(vpn, 'server_config', side_effect=lambda c, k: dict(original(c, k), log={'level': 'error'})):
            with self.assertRaisesRegex(RuntimeError, 'simulated start failure'):
                vpn.upgrade('192.0.2.10')
        self.assertEqual((vpn.CONFIG / 'server.json').read_bytes(), before)
        self.assertEqual(self.restarted(), [['systemctl', 'restart', 'la-vpn.service']] * 2)
        self.assert_digests_match()

    def test_new_sing_box_release_is_fetched(self):
        with patch.object(vpn, 'sing_box_version', return_value='1.12.0'), \
                patch.object(vpn, 'fetch_binary', return_value=b'newer-executable') as fetch:
            vpn.upgrade('192.0.2.10')
        fetch.assert_called_once()
        self.assertEqual((vpn.PROGRAM / 'bin/sing-box').read_bytes(), b'newer-executable')
        self.assertEqual(self.restarted(), [['systemctl', 'restart', 'la-vpn.service']])
        self.assert_digests_match()

    def test_version_parsing(self):
        self.commands.return_value.stdout = 'sing-box version 1.14.3\n\nEnvironment: go1.24\n'
        self.assertEqual(vpn.sing_box_version(), '1.14.3')
        self.commands.return_value.stdout = ''
        self.assertEqual(vpn.sing_box_version(), '')


class MultiUserUpgradeTests(unittest.TestCase):
    def setUp(self):
        test_kit.TransactionTests.setUp(self)
        raw = dict(self.s, users=['alice', 'bob', 'carol'])
        raw.pop('fair_share')
        self.stack.enter_context(patch.object(vpn.network, 'missing_modules', return_value=[]))
        self.stack.enter_context(patch.object(vpn.network, 'health', return_value={'fair_share': {'result': 'skipped'}}))
        self.s = render.settings(raw)
        vpn.install(self.s, '192.0.2.10')
        self.commands.reset_mock()

    def test_same_kit_is_a_no_op(self):
        vpn.upgrade('192.0.2.10')
        self.assertFalse([c for c in self.commands.call_args_list if c.args[0][:2] == ['systemctl', 'restart']])

    def test_client_profile_change_reaches_every_user_without_restart(self):
        original = vpn.client_files
        def newer(cfg, account):
            return dict(original(cfg, account), **{'nodes.txt': 'newer-' + account['name']})
        with patch.object(vpn, 'client_files', side_effect=newer):
            vpn.upgrade('192.0.2.10')
        for name in ('alice', 'bob', 'carol'):
            self.assertEqual((vpn.CONFIG / 'subscriptions' / name / 'nodes.txt').read_text(), 'newer-' + name)
        self.assertFalse([c for c in self.commands.call_args_list if c.args[0][:2] == ['systemctl', 'restart']])


if __name__ == '__main__':
    unittest.main()
