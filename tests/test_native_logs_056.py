import contextlib
import io
import json
import os
from pathlib import Path
import sys
import signal
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_native
import installlog
import kit_runtime
import launcher
import render
import vpn


class Defaults056Tests(unittest.TestCase):
    def test_init_defaults_http_and_generates_all_clients(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            cfgpath = Path(temporary) / 'settings.json'
            launcher.main(['init', '--server-ip', '8.8.4.4', '--bandwidth', '30Mbps',
                           '--accept-http-risk', '--output', str(cfgpath)])
            cfg = json.loads(cfgpath.read_text())
            self.assertEqual(cfg['subscription']['scheme'], 'http')
            self.assertEqual(cfg['subscription']['port'], 80)
            output = Path(temporary) / 'generated'
            launcher.main(['render', '--settings', str(cfgpath), '--output', str(output)])
            for name in render.SUBSCRIPTION_FILES.values():
                self.assertTrue((output / name).is_file(), name)
            for section in ('Mac / Windows', 'Android', '小火箭'):
                self.assertIn(section, (output / 'client-import.txt').read_text())

    def test_http_risk_missing_stops_before_ip_network_query(self):
        with patch.object(launcher.autoconfig, 'detect_public_ipv4') as query, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                launcher.main(['init', '--bandwidth', '30'])
            query.assert_not_called()

    def test_wizard_enter_defaults_http_without_cert_questions(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()), \
                patch.object(launcher.sys.stdin, 'isatty', return_value=True), \
                patch.object(launcher.autoconfig, 'detect_public_ipv4',
                             return_value={'address': '8.8.4.4', 'source': 'test'}), \
                patch.object(launcher.platform, 'system', return_value='Linux'), \
                patch.object(launcher.os, 'geteuid', return_value=0), \
                patch.object(launcher.subprocess, 'run'), patch.object(launcher, 'dispatch') as dispatch:
            path = Path(temporary) / 'config.json'
            with patch('builtins.input', side_effect=[str(path), '', '', '1Gbps', '', '', '', '', 'HTTP', '', '']) as ask:
                launcher.wizard()
            cfg = json.loads(path.read_text())
            self.assertEqual(cfg['subscription']['scheme'], 'http')
            self.assertEqual(cfg['hy2_down_mbps'], 1000)
            self.assertFalse(any('证书' in call.args[0] for call in ask.call_args_list))
            dispatch.assert_not_called()  # Empty final confirmation is non-mutating.


class Native056Tests(unittest.TestCase):
    def test_native_dispatch_stays_inside_binary(self):
        with patch.object(kit_runtime, 'compiled', return_value=True), \
                patch.object(vpn, 'main') as main, patch.object(launcher.os, 'execv') as execv:
            launcher.dispatch(['plan', '--settings', '/a file.json'])
            main.assert_called_once_with(['plan', '--settings', '/a file.json'])
            execv.assert_not_called()

    def test_native_plan_uses_original_executable_not_unpacked_runtime(self):
        with patch.object(kit_runtime, 'compiled', return_value=True), \
                patch.object(kit_runtime, 'executable', return_value=Path('/tmp/program with spaces')):
            self.assertEqual(kit_runtime.command(['plan']), ['/tmp/program with spaces', 'plan'])

    def test_native_systemd_helpers_do_not_reference_sources(self):
        with patch.object(kit_runtime, 'compiled', return_value=True):
            units = vpn.service_files('ens3')
            self.assertIn('gowest _boot-fq ens3', units['la-vpn-fq.service'])
            self.assertIn('gowest _subinfo', units['la-vpn-subinfo.service'])
            self.assertEqual(vpn.installed_command('rollback', '--automatic'),
                             ['/opt/la-vpn/gowest', 'rollback', '--automatic'])
            self.assertNotIn('.py', ''.join(units.values()))

    def test_native_install_copies_only_executable(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(kit_runtime, 'compiled', return_value=True), \
                patch.object(vpn, 'managed_file') as write:
            binary = Path(temporary) / 'gowest'
            binary.write_bytes(b'native placeholder')
            with patch.object(kit_runtime, 'executable', return_value=binary):
                vpn.install_program({})
            write.assert_called_once_with({}, vpn.PROGRAM / 'gowest', b'native placeholder', 0o755)

    def test_internal_subinfo_dispatch(self):
        import subinfo
        with patch.object(subinfo, 'main') as main:
            launcher.main(['_subinfo'])
            main.assert_called_once_with()
        with self.assertRaises(ValueError):
            launcher.main(['_subinfo', 'unexpected'])

    def test_release_packager_rejects_sources_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'stage'
            directory.mkdir()
            for name in build_native.ALLOWED_RELEASE_FILES:
                (directory / name).write_text('placeholder')
            source = directory / 'launcher.py'
            source.write_text('private code')
            with self.assertRaises(RuntimeError):
                build_native.package(directory, Path(temporary) / 'bad.tar.gz')
            source.unlink()
            (directory / 'gowest').unlink()
            (directory / 'gowest').symlink_to('/usr/bin/true')
            with self.assertRaises(RuntimeError):
                build_native.package(directory, Path(temporary) / 'bad.tar.gz')


class Logging056Tests(unittest.TestCase):
    def test_timeout_kills_process_group_before_rollback_can_start(self):
        with patch.object(vpn.subprocess, 'Popen') as popen, patch.object(vpn.os, 'killpg') as kill, \
                patch.object(vpn, 'log_failure'):
            child = popen.return_value.__enter__.return_value
            child.pid = 12345
            child.communicate.side_effect = subprocess.TimeoutExpired(['tool', 'private-arg'], 10)
            with self.assertRaises(subprocess.TimeoutExpired) as error:
                vpn.run(['tool', 'private-arg'], timeout=10)
            kill.assert_called_once_with(12345, signal.SIGKILL)
            child.wait.assert_called_once_with(timeout=5)
            self.assertNotIn('private-arg', str(error.exception))

    def test_real_command_stdin_output_and_noninteractive_environment(self):
        result = vpn.run([sys.executable, '-c',
                          'import os,sys; print(os.environ["NEEDRESTART_MODE"] + '
                          'os.environ["NEEDRESTART_SUSPEND"] + sys.stdin.read())'], data='test')
        self.assertEqual(result.stdout.strip(), 'l1test')

    def test_private_log_flushes_preflight_and_preserves_paths(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()):
            log = installlog.InstallLog()
            log.emit('开始', '预检')
            path = Path(temporary) / 'install.log'
            log.attach(path)
            log.emit('日志', '/var/lib/la-vpn-deploy/install.log')
            log.close()
            content = path.read_text()
            self.assertIn('预检', content)
            self.assertIn('/var/lib/la-vpn-deploy/install.log', content)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_log_redacts_url_and_common_keys(self):
        content = installlog.redact('http://8.8.4.4/sub/' + 'a' * 48 + '\npassword=abc123 uuid=abc-def')
        self.assertNotIn('/sub/', content)
        self.assertNotIn('abc123', content)
        self.assertNotIn('abc-def', content)

    def test_step_reports_heartbeat_and_failure(self):
        console = io.StringIO()
        with contextlib.redirect_stdout(console):
            log = installlog.InstallLog(interval=0.01)
            with self.assertRaisesRegex(ValueError, 'broken'):
                with log.step(3, '依赖'):
                    threading.Event().wait(0.05)
                    raise ValueError('broken')
            log.close()
        for value in ('开始', '进行中', '失败', '3/8', 'broken'):
            self.assertIn(value, console.getvalue())

    def test_preflight_failure_creates_no_state(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.redirect_stdout(io.StringIO()) as output, \
                patch.object(vpn, 'STATE', Path(temporary) / 'state'), \
                patch.object(vpn, 'preflight', side_effect=RuntimeError('port conflict')), \
                patch.object(vpn, 'rollback') as rollback:
            with self.assertRaises(RuntimeError):
                vpn.install({}, '8.8.4.4')
            self.assertFalse(vpn.STATE.exists())
            self.assertIn('尚未创建安装目录', output.getvalue())
            rollback.assert_not_called()


if __name__ == '__main__':
    unittest.main()
