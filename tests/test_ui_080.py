"""Local web UI: input checks, SSH command building, local-only access, JSON outputs."""
import io
import json
import shlex
import sys
import tarfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gowest_ui as ui  # noqa: E402
import localcheck  # noqa: E402
import vpn  # noqa: E402
import test_users_070  # noqa: E402

SERVER = {'id': 'a1', 'name': 'LA', 'target': 'root@203.0.113.7', 'ip': '203.0.113.7'}


class CommandTests(unittest.TestCase):
    def sent(self, body):
        with patch.object(ui, 'remote', return_value='ok') as remote:
            ui.api_users(SERVER, body)
        return shlex.split(remote.call_args.args[1].split('; then ', 1)[1].split('; else')[0])

    def test_users_command_is_quoted_and_confirms_the_saved_ip(self):
        args = self.sent({'action': 'set', 'name': 'bob', 'cap': '20Mbps', 'quota': '', 'expire': '2026-12-31'})
        self.assertEqual(args[3:6], ['users', 'set', 'bob'])
        self.assertIn('--confirm-server', args)
        self.assertEqual(args[args.index('--confirm-server') + 1], '203.0.113.7')
        self.assertEqual(args[args.index('--quota') + 1], '0', 'empty quota clears it')
        self.assertEqual(args[args.index('--cap') + 1], '20Mbps')

    def test_shell_metacharacters_are_rejected(self):
        for body in ({'action': 'add', 'name': 'x;reboot'}, {'action': 'add', 'name': 'bob', 'quota': '1TB; id'},
                     {'action': 'add', 'name': 'bob', 'expire': '$(id)'}, {'action': 'drop', 'name': 'bob'}):
            with self.assertRaises(ui.UserError):
                self.sent(body)

    def test_server_targets_and_ips(self):
        for bad in ('root@1.2.3.4 -oProxyCommand=x', 'a;b', '', '-oX'):
            with self.assertRaises(ui.UserError):
                ui.api_add_server({'target': bad})
        with patch.object(ui, 'remote'):
            with self.assertRaisesRegex(ui.UserError, '公网 IPv4'):
                ui.api_add_server({'target': 'my-alias'})

    def test_ssh_failures_become_actionable(self):
        self.assertIn('ssh-copy-id', ui.explain('root@x: Permission denied (publickey).'))
        self.assertIn('sudo', ui.explain('sudo: a password is required'))
        self.assertIn('连不上', ui.explain('ssh: connect to host x port 22: Connection timed out'))
        self.assertIn('还没有安装', ui.explain('bash: /opt/la-vpn/deploy.sh: No such file or directory'))
        self.assertEqual(ui.explain('noise\nERROR: 没有这个订阅用户：zed'), '没有这个订阅用户：zed')

    def test_upload_carries_only_what_the_server_runs(self):
        names = tarfile.open(fileobj=io.BytesIO(ui.kit_archive())).getnames()
        for needed in ('deploy.sh', 'vpn.py', 'accounting.py', 'unlock.py', 'render.py'):
            self.assertIn(needed, names)
        for skipped in ('tests', 'ui', 'docs', '.git'):
            self.assertFalse(any(n == skipped or n.startswith(skipped + '/') for n in names), skipped)
        self.assertFalse(any('__pycache__' in n for n in names))


class AccessTests(unittest.TestCase):
    def handler(self, host, origin=None):
        h = SimpleNamespace(headers={'Host': host, **({'Origin': origin} if origin else {})}, port=8123)
        return ui.Handler.trusted(h)

    def test_only_this_computer_may_call(self):
        self.assertTrue(self.handler('127.0.0.1:8123'))
        self.assertTrue(self.handler('localhost:8123', 'http://localhost:8123'))
        self.assertFalse(self.handler('evil.example:8123'))  # DNS rebinding
        self.assertFalse(self.handler('127.0.0.1:8123', 'https://evil.example'))


class LocalCheckTests(unittest.TestCase):
    def test_reports_each_port_and_never_claims_udp(self):
        info = {'server_ip': '203.0.113.7', 'proxy_ports': [20001], 'subscription_ports': [80],
                'subscription_url': 'http://203.0.113.7/sub/x'}
        with patch.object(localcheck, 'tcp', side_effect=lambda ip, port: port == 80), \
                patch.object(localcheck.urllib.request, 'build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value.status = 200
            rows = localcheck.check(info)
        self.assertEqual([r['ok'] for r in rows], [False, True, True, None])
        self.assertIn('安全组', rows[0]['text'])


class ServerJsonTests(test_users_070.UsersTransactionTests):
    """Reuse the three-user simulated install; only the JSON views are new here."""

    def test_status_report_has_what_the_page_needs(self):
        with patch.object(vpn.network, 'health', return_value={'fair_share': {'result': 'applied'}, 'warnings': []}):
            report = json.loads(json.dumps(vpn.status_report(), ensure_ascii=False))
        self.assertEqual([u['name'] for u in report['users']], ['alice', 'bob', 'carol'])
        self.assertEqual(report['users'][1]['port'], 20002)
        for key in ('kit_version', 'services', 'traffic', 'bandwidth_mbps', 'fair_share_result', 'single'):
            self.assertIn(key, report)

    def test_url_report_matches_the_manifest(self):
        report = vpn.url_report('bob')
        self.assertTrue(self.url('bob').startswith('normal: ' + report['base']))
        self.assertEqual(report['files']['nodes.txt'], report['base'] + '-nodes')
        self.assertTrue(report['clashmeta']['normal'].startswith('clashmeta://install-config?name='))
        with self.assertRaisesRegex(RuntimeError, '指定其中一人'):
            vpn.url_report('')


for name in [n for n in dir(test_users_070.UsersTransactionTests) if n.startswith('test_')]:
    setattr(ServerJsonTests, name, None)  # inherited transaction tests already run in their own module


if __name__ == '__main__':
    unittest.main()
