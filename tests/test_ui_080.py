"""Local web UI: input checks, SSH command building, local-only access, JSON outputs."""
import io
import errno
import json
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
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

    def test_rollback_needs_the_typed_ip(self):
        with patch.object(ui, 'remote', return_value='archived') as remote:
            with self.assertRaises(ui.UserError):
                ui.api_rollback(SERVER, {'confirm': '198.51.100.1'})
            remote.assert_not_called()
            ui.api_rollback(SERVER, {'confirm': '203.0.113.7'})
        self.assertIn('rollback', remote.call_args.args[1])

    def test_traffic_passes_metering(self):
        with patch.object(ui, 'remote', return_value='ok') as remote:
            ui.api_traffic(SERVER, {'quota': '1TB', 'on_exceed': 'stop', 'metering': 'outbound'})
        command = remote.call_args.args[1]
        self.assertIn('--metering outbound', command)
        self.assertIn('--confirm-server 203.0.113.7', command)

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


class OutputTests(unittest.TestCase):
    def test_messages_before_exec_survive_a_pipe(self):
        """Over SSH stdout is a pipe; exec must not drop what quick printed first."""
        import subprocess
        kit = Path(__file__).resolve().parents[1]
        p = subprocess.run([sys.executable, '-B', '-c',
                            "import launcher; print('before-exec'); launcher.dispatch(['plan', '--help'])"],
                           cwd=kit, capture_output=True, text=True, timeout=30)
        self.assertIn('before-exec', p.stdout)
        self.assertIn('--settings', p.stdout)


class CompatibilityTests(unittest.TestCase):
    def inspect(self, state=None, version='0.5.0', status_text='', conflicts=(),
                status_exit=2, source=True):
        """Execute the exact stdin probe in a filesystem sandbox; no real SSH/systemctl."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def path(value):
                return root / str(value).lstrip('/')
            if state is not None:
                p = path('/var/lib/la-vpn-deploy/state.json')
                p.parent.mkdir(parents=True)
                p.write_text(state if isinstance(state, str) else json.dumps(state))
            if source:
                p = path('/opt/la-vpn/render.py')
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("KIT_VERSION = '" + version + "'\n")
                path('/opt/la-vpn/deploy.sh').touch()
            for item in conflicts:
                path(item).mkdir(parents=True, exist_ok=True)
            output = io.StringIO()
            with patch('pathlib.Path', side_effect=path), patch('shutil.which', return_value=None), \
                    patch('subprocess.run', side_effect=[
                        SimpleNamespace(returncode=status_exit, stdout=status_text,
                                        stderr='error: unrecognized arguments: --json' if status_exit == 2 else ''),
                        SimpleNamespace(returncode=0, stdout='active\nactive\nactive\n')]) as commands, \
                    redirect_stdout(output):
                exec(compile(ui.INSPECT_SCRIPT, '<server-inspection>', 'exec'), {})
            return json.loads(output.getvalue()), commands

    def state(self, **extra):
        return dict(status='installed', server_ip=SERVER['ip'], files={}, **extra)

    def test_legacy_version_is_read_without_import_and_unknown_json_is_handled(self):
        data, commands = self.inspect(self.state())
        self.assertTrue(data['legacy'])
        self.assertTrue(data['can_upgrade'])
        self.assertEqual(data['kit_version'], '0.5.0')
        self.assertEqual(data['services']['la-vpn.service'], 'active')
        self.assertEqual(commands.call_args_list[0].args[0][-2:], ['status', '--json'])
        self.assertEqual(commands.call_args_list[1].args[0][:2], ['systemctl', 'is-active'])

    def test_modern_install_uses_json_capability_not_version_guess(self):
        info, commands = self.inspect(self.state(kit_version='0.9.0'), status_exit=0,
                                      status_text='{"kit_version":"0.9.0","services":{}}')
        self.assertFalse(info['legacy'])
        commands.assert_called_once()
        self.assertEqual(commands.call_args.kwargs['env']['LC_ALL'], 'C')
        with patch.object(ui, 'inspect_server', return_value=info), \
                patch.object(ui, 'remote_json', return_value={'kit_version': '0.9.0', 'services': {}}) as read:
            data = ui.api_status(SERVER)
        self.assertTrue(data['can_upgrade'])
        read.assert_not_called()  # The probe already fetched the modern status.

    def test_legacy_status_never_calls_unsupported_json(self):
        info, _ = self.inspect(self.state())
        with patch.object(ui, 'inspect_server', return_value=info), patch.object(ui, 'remote_json') as read:
            self.assertEqual(ui.api_status(SERVER), info)
        read.assert_not_called()

    def test_unmanaged_proxy_is_not_a_fresh_server(self):
        info, commands = self.inspect(source=False, conflicts=('/etc/sing-box', '/etc/nginx'))
        self.assertEqual(info['management'], 'unmanaged')
        self.assertFalse(info['can_upgrade'])
        self.assertIn('/etc/sing-box', info['conflicts'])
        commands.assert_not_called()

    def test_fresh_server_is_distinct_from_partial_install(self):
        info, _ = self.inspect(source=False)
        self.assertEqual(info['management'], 'fresh')
        info, commands = self.inspect(self.state(), source=False)
        self.assertEqual(info['management'], 'incomplete')
        self.assertFalse(info['can_upgrade'])
        commands.assert_not_called()

    def test_corrupt_or_noninstalled_state_never_enables_mutations(self):
        for state in ('broken-json', '[]', '{}'):
            info, commands = self.inspect(state)
            self.assertIn('inspection_error', info)
            commands.assert_not_called()
        state = self.state()
        state['status'] = 'installing'
        info, commands = self.inspect(state)
        self.assertEqual(info['management'], 'incomplete')
        self.assertFalse(info['can_upgrade'])
        commands.assert_not_called()

    def test_broken_status_is_not_misclassified_as_legacy(self):
        info, _ = self.inspect(self.state(), status_exit=1)
        self.assertIn('inspection_error', info)

    def test_invalid_json_status_never_enables_upgrade(self):
        for text in ('', 'not-json', '[]', '{}', '{"services":[]}'):
            with self.subTest(text=text):
                info, _ = self.inspect(self.state(), status_exit=0, status_text=text)
                self.assertIn('inspection_error', info)

    def test_sudo_failure_propagates_and_ip_mismatch_blocks_upgrade(self):
        with patch.object(ui, 'remote', side_effect=ui.UserError('sudo 需要密码')):
            with self.assertRaisesRegex(ui.UserError, 'sudo'):
                ui.inspect_server(SERVER)
        info, _ = self.inspect(self.state())
        info['server_ip'] = '198.51.100.1'
        with patch.object(ui, 'remote', return_value=json.dumps(info)) as remote:
            data = ui.inspect_server(SERVER)
        self.assertFalse(data['can_upgrade'])
        self.assertIn('IP', data['message'])
        self.assertEqual(remote.call_args.args[1], 'sudo -n python3 -B -')
        self.assertEqual(remote.call_args.kwargs['data'], ui.INSPECT_SCRIPT.encode())

    def test_install_and_upgrade_refuse_unmanaged_before_upload(self):
        with patch.object(ui, 'inspect_server', return_value={'management': 'unmanaged', 'can_upgrade': False}), \
                patch.object(ui, 'upload') as upload, patch.object(ui.Job, 'run') as run:
            with self.assertRaises(ui.UserError):
                ui.api_install(SERVER, {'bandwidth': '30Mbps'})
            with self.assertRaises(ui.UserError):
                ui.api_upgrade(SERVER)
        upload.assert_not_called()
        run.assert_not_called()

    def test_legacy_upgrade_still_requires_explicit_job_and_ip_confirmation(self):
        info, _ = self.inspect(self.state())
        steps = []
        with patch.object(ui, 'inspect_server', return_value=info), \
                patch.object(ui.Job, 'run', side_effect=lambda items: steps.extend(items)), \
                patch.object(ui, 'upload') as upload:
            ui.api_upgrade(SERVER)
            upload.assert_not_called()  # Creating the job does not run it in this test.
            job = SimpleNamespace(stream=unittest.mock.Mock())
            steps[1](job)
        self.assertIn('upgrade --confirm-server ' + SERVER['ip'], job.stream.call_args.args[1])


class LocalReadTests(unittest.TestCase):
    def test_transient_shared_drive_error_is_retried(self):
        with patch.object(Path, 'read_bytes', side_effect=[OSError(errno.EIO, 'I/O error'), b'ok']) as read, \
                patch.object(ui.time, 'sleep'):
            self.assertEqual(ui.local_read(Path('/shared/file')), b'ok')
        self.assertEqual(read.call_count, 2)

    def test_server_list_failure_is_not_an_empty_list(self):
        for error in (OSError(errno.EIO, 'I/O error'), PermissionError(errno.EACCES, 'denied')):
            with patch.object(Path, 'read_bytes', side_effect=error), patch.object(ui.time, 'sleep'):
                with self.assertRaisesRegex(ui.UserError, '本地文件'):
                    ui.load_servers()
        with patch.object(Path, 'read_bytes', return_value=b'not-json'):
            with self.assertRaisesRegex(ui.UserError, '保留原文件'):
                ui.load_servers()
        with patch.object(Path, 'read_bytes', side_effect=FileNotFoundError):
            self.assertEqual(ui.load_servers(), [])

    def test_cached_page_and_version_do_not_read_the_share(self):
        with patch.object(ui, 'ASSETS', None), patch.object(ui, 'UI_VERSION', None):
            ui.prepare_ui()
            handler = SimpleNamespace(send=unittest.mock.Mock())
            with patch.object(Path, 'read_bytes', side_effect=OSError(errno.EIO, 'offline')):
                self.assertTrue(ui.kit_version())
                ui.Handler.static(handler, ['app.js'])
                self.assertEqual(handler.send.call_args.args[0], 200)
                ui.Handler.static(handler, ['..', 'render.py'])
                self.assertEqual(handler.send.call_args.args[0], 404)


@unittest.skipUnless(shutil.which('node'), 'Node.js needed for frontend view regression tests')
class FrontendCompatibilityTests(unittest.TestCase):
    def test_legacy_can_upgrade_but_errors_and_unmanaged_cannot(self):
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('ui/app.js', 'utf8').split("document.addEventListener('click'")[0];
const context = vm.createContext({});
vm.runInContext(source, context);
vm.runInContext(`
S.version = '0.9.0'; S.current = 'wsd';
const fixtureServer = {id:'wsd', name:'WSD', ip:'203.0.113.7'};
S.status.wsd = {installed:true, legacy:true, management:'managed', kit_version:'0.5.0', can_upgrade:true, services:{}};
`, context);
let html = vm.runInContext('serverView(fixtureServer)', context);
assert(html.includes('data-act="upgrade"'));
assert(html.includes('旧版待升级'));
assert(!html.includes('data-act="check"'));
assert(!html.includes('data-act="rollback"'));
vm.runInContext('S.status.wsd.can_upgrade = false', context);
assert(!vm.runInContext('serverView(fixtureServer)', context).includes('data-act="upgrade"'));
vm.runInContext("S.status.wsd = {installed:false, management:'unmanaged', conflicts:['/etc/sing-box']}", context);
html = vm.runInContext('serverView(fixtureServer)', context);
assert(html.includes('未接管'));
assert(!html.includes('install-form'));
assert(!html.includes('data-act="upgrade"'));
vm.runInContext("S.status.wsd = {error:'permission denied'}", context);
html = vm.runInContext('serverView(fixtureServer)', context);
assert(!html.includes('data-act="upgrade"'));
assert(html.includes('读取失败'));
vm.runInContext("S.status.wsd = {installed:false, management:'fresh'}", context);
assert(vm.runInContext('serverView(fixtureServer)', context).includes('install-form'));
'''
        p = subprocess.run(['node', '-e', script], capture_output=True, text=True,
                           cwd=ui.KIT, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)

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
