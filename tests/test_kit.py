import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import yaml
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check
import network
import probe
import render
import subinfo
import vpn
import launcher


def example():
    return json.loads((Path(__file__).resolve().parents[1] / 'settings.example.json').read_text())


class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='la-vpn-unit-')
        cls.output = render.render(example(), Path(cls.tmp.name) / 'first')
        cls.keys = json.loads((cls.output / 'secrets.json').read_text())
        cls.normal = yaml.safe_load((cls.output / 'normal.yaml').read_text())
        cls.strict = yaml.safe_load((cls.output / 'strict.yaml').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_cross_config_validation(self):
        self.assertEqual(check.check(self.output)['cross_config_checks'], 'passed')

    def test_private_permissions(self):
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        for name in ('secrets.json', 'server.json', 'normal.yaml', 'strict.yaml', 'hy2.key', 'subscription-urls.txt'):
            self.assertEqual((self.output / name).stat().st_mode & 0o777, 0o600)

    def test_independent_secrets(self):
        second = render.render(example(), Path(self.tmp.name) / 'second')
        keys = json.loads((second / 'secrets.json').read_text())
        for field in self.keys:
            self.assertNotEqual(keys[field], self.keys[field], field)

    def test_no_overwrite(self):
        original = (self.output / 'secrets.json').read_bytes()
        with self.assertRaises(FileExistsError):
            render.render(example(), self.output)
        self.assertEqual((self.output / 'secrets.json').read_bytes(), original)

    def test_reject_output_symlink(self):
        link = Path(self.tmp.name) / 'linked'
        link.symlink_to(self.output, target_is_directory=True)
        with self.assertRaises(FileExistsError):
            render.render(example(), link)

    def test_reality_key_lengths(self):
        for field in ('reality_private_key', 'reality_public_key'):
            self.assertEqual(len(base64.urlsafe_b64decode(self.keys[field] + '=')), 32)

    def test_fallback_never_direct(self):
        for cfg in (self.normal, self.strict):
            for group in cfg['proxy-groups']:
                if group['type'] == 'fallback':
                    self.assertEqual(group['proxies'], ['LA-Hy2', 'LA-VLESS'])
                    self.assertEqual(group['interval'], 60)

    def test_strict_has_no_public_direct_selector(self):
        self.assertNotIn('DIRECT', self.strict['proxy-groups'][0]['proxies'])
        self.assertIn('DIRECT', self.normal['proxy-groups'][0]['proxies'])
        self.assertNotIn('GEOSITE,cn,DIRECT', self.strict['rules'])
        self.assertIn('GEOSITE,cn,DIRECT', self.normal['rules'])

    def test_claude_dns_complete(self):
        for cfg in (self.normal, self.strict):
            for domain in render.DOMAINS:
                self.assertEqual(cfg['dns']['nameserver-policy']['+.' + domain], 'https://1.1.1.1/dns-query#Claude')
                self.assertIn('DOMAIN-SUFFIX,' + domain + ',Claude', cfg['rules'])

    def test_tun_dns_and_route_fix(self):
        for cfg in (self.normal, self.strict):
            self.assertFalse(cfg['tun']['enable'])
            self.assertIn('tcp://any:53', cfg['tun']['dns-hijack'])
            self.assertIn('any:53', cfg['tun']['dns-hijack'])
            self.assertIn('192.0.2.10/32', cfg['tun']['route-exclude-address'])
            self.assertIn('geosite:cn', cfg['dns']['fake-ip-filter'])

    def test_no_current_machine_identities(self):
        text = json.dumps(self.normal) + json.dumps(self.strict)
        for value in ('198.51.100.77', '100.88.12.34', 'tailexample0', 'external-controller'):
            self.assertNotIn(value, text)

    def test_scoped_tailnet(self):
        self.assertIn('+.your-tailnet.ts.net', self.normal['dns']['nameserver-policy'])
        self.assertNotIn('+.ts.net', self.normal['dns']['nameserver-policy'])

    def test_empty_tailnet(self):
        s = example(); s['tailnet'] = ''
        cfg = render.client_config(render.settings(s), self.keys)
        self.assertFalse(any('ts.net' in x for x in cfg['dns']['nameserver-policy']))

    def test_nginx_only_allowlisted_secret_locations(self):
        text = (self.output / 'nginx.conf').read_text()
        self.assertEqual(text.count('location = /sub/'), len(render.SUBSCRIPTION_FILES))
        self.assertIn('location / { return 404; }', text)
        self.assertIn('access_log off', text)
        self.assertNotIn('/secrets.json', text)
        self.assertNotIn('hy2.key', text)

    def test_nginx_temp_paths_stay_in_writable_runtime_directory(self):
        text = (self.output / 'nginx.conf').read_text()
        for directive in ('client_body_temp_path', 'proxy_temp_path', 'fastcgi_temp_path',
                          'uwsgi_temp_path', 'scgi_temp_path'):
            self.assertIn(directive + ' /run/la-vpn-subscription/', text)
        service = vpn.service_files('eth0')['la-vpn-subscription.service']
        self.assertIn('RuntimeDirectory=la-vpn-subscription', service)
        self.assertIn('ProtectSystem=strict', service)
        self.assertNotIn('/var/lib/nginx', text)

    def test_https_no_downgrade(self):
        self.assertIn('listen 8443 ssl;', (self.output / 'nginx.conf').read_text())
        self.assertTrue((self.output / 'subscription-urls.txt').read_text().startswith('normal: https://'))

    def test_http_requires_opt_in(self):
        s = example(); s['subscription'] = {'scheme': 'http', 'host': s['server_ip'], 'port': 80}
        with self.assertRaises(ValueError): render.settings(s)
        s['subscription']['allow_insecure_http'] = True
        self.assertEqual(render.settings(s)['subscription']['port'], 80)

    def test_injection_rejected(self):
        for key, value in [('server_ip', '1.2.3.4; id'), ('node_prefix', 'LA\nMATCH,DIRECT'),
                           ('tailnet', 'evil.example'), ('reality_server_name', 'abc;\ninclude /tmp/x')]:
            s = example(); s[key] = value
            with self.assertRaises(ValueError): render.settings(s)

    def test_bad_bandwidth(self):
        for value in (None, 0, -1, True, '30 Mbps', 100001, float('nan'), float('inf')):
            s = example(); s['hy2_up_mbps'] = value
            with self.assertRaises(ValueError): render.settings(s)

    def test_required_explicit_bandwidth(self):
        s = example(); del s['hy2_down_mbps']
        with self.assertRaises(ValueError): render.settings(s)

    def test_port_conflicts(self):
        for port in (22, 443, 0, -1, 65536, True):
            s = example(); s['subscription']['port'] = port
            with self.assertRaises(ValueError): render.settings(s)

    def test_no_unknown_options(self):
        s = example(); s['tun_enbale'] = True
        with self.assertRaises(ValueError): render.settings(s)

    def test_custom_prefix(self):
        s = example(); s['node_prefix'] = 'US2'
        cfg = render.client_config(render.settings(s), self.keys, strict=True)
        self.assertEqual(cfg['proxy-groups'][0]['name'], '出口选择')
        self.assertEqual(cfg['rules'][-1], 'MATCH,出口选择')
        self.assertEqual({p['name'] for p in cfg['proxies']}, {'US2-Hy2', 'US2-VLESS'})


class SafetyTests(unittest.TestCase):
    def test_dependency_updates_cannot_auto_restart_installer(self):
        with patch.dict(os.environ, {'NEEDRESTART_MODE': 'a'}), \
             patch.object(vpn.subprocess, 'Popen') as command:
            process = command.return_value.__enter__.return_value
            process.communicate.return_value = ('', '')
            process.returncode = 0
            vpn.run(['apt-get', 'install', '-y', 'nginx'])
            self.assertEqual(command.call_args.kwargs['env']['NEEDRESTART_MODE'], 'l')
            self.assertEqual(command.call_args.kwargs['env']['DEBIAN_FRONTEND'], 'noninteractive')
            self.assertEqual(os.environ['NEEDRESTART_MODE'], 'a')

    def test_startup_retry(self):
        with patch.object(vpn, 'verify', side_effect=[RuntimeError('starting'), None]) as verify, \
             patch.object(vpn.time, 'sleep'):
            vpn.verify_started({})
            self.assertEqual(verify.call_count, 2)

    def test_startup_deadline(self):
        with patch.object(vpn, 'verify', side_effect=RuntimeError('still unavailable')), \
             patch.object(vpn.time, 'monotonic', side_effect=[0, 31]), patch.object(vpn.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'still unavailable'):
                vpn.verify_started({}, timeout=30)

    def test_private_command_error_log(self):
        with tempfile.TemporaryDirectory() as t, patch.object(vpn, 'STATE', Path(t)):
            vpn.log_failure('/usr/bin/test-command', 'test failure')
            p = Path(t) / 'command-errors.log'
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertIn('test failure', p.read_text())

    def test_custom_qdisc_untouched(self):
        for kind in ('mq', 'htb', 'cake', 'fq_codel', 'noqueue', 'fq'):
            self.assertFalse(network.safe_pfifofast({'kind': kind, 'handle': '0:'}))

    def test_nondefault_fifo_untouched(self):
        self.assertFalse(network.safe_pfifofast({'kind': 'pfifo_fast', 'handle': '1:'}))
        self.assertFalse(network.safe_pfifofast({'kind': 'pfifo_fast', 'handle': '0:', 'options': {'bands': 4}}))
        self.assertTrue(network.safe_pfifofast({'kind': 'pfifo_fast', 'handle': '0:'}))

    def test_default_operation_is_not_install(self):
        # install cannot be invoked without a target-specific confirmation.
        import subprocess
        p = subprocess.run([sys.executable, str(Path(vpn.__file__)), 'install', '--settings', 'missing.json'], capture_output=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn(b'confirm-new-server', p.stderr)

    def test_darwin_guard_before_mutation(self):
        with patch.object(vpn.platform, 'system', return_value='Darwin'):
            with self.assertRaisesRegex(RuntimeError, 'NEW Ubuntu'):
                vpn.preflight(render.settings(example()), '192.0.2.10')

    def test_changed_network_refuses_restore(self):
        state = {'applied': {'net.core.rmem_max': '33554432'}, 'before': {'net.core.rmem_max': '212992'},
                 'qdisc_changed': False}
        with patch.object(network, 'run', return_value='67108864'):
            with self.assertRaisesRegex(RuntimeError, 'changed outside'):
                network.check_restore(state)

    def test_managed_write_exclusive(self):
        with tempfile.TemporaryDirectory() as t, patch.object(vpn, 'save_state'):
            p = Path(t) / 'owned'; p.write_text('user-data')
            with self.assertRaises(RuntimeError):
                vpn.managed_file({'files': {}}, p, 'new')
            self.assertEqual(p.read_text(), 'user-data')

    def test_managed_symlink_refused(self):
        with tempfile.TemporaryDirectory() as t, patch.object(vpn, 'save_state'):
            p = Path(t) / 'owned'; p.symlink_to(Path(t) / 'absent')
            with self.assertRaises(RuntimeError): vpn.managed_file({'files': {}}, p, 'new')

    def test_successful_watchdog_noop(self):
        with patch.object(vpn.platform, 'system', return_value='Linux'), patch.object(vpn.os, 'geteuid', return_value=0), \
             patch.object(vpn, 'load_state', return_value={'status': 'installed'}), patch.object(vpn, 'run') as commands:
            vpn.rollback(automatic=True)
            commands.assert_not_called()

    def test_service_no_shell_expansion_of_interface(self):
        units = vpn.service_files('ens3')
        self.assertIn('network.py ens3', units['la-vpn-fq.service'])
        self.assertIn('RestartSec=3', units['la-vpn.service'])
        self.assertIn('User=la-vpn', units['la-vpn.service'])

    def test_unknown_quota_is_omitted(self):
        self.assertEqual(subinfo.header(2, 3), 'add_header subscription-userinfo "upload=3; download=2" always;\n')
        self.assertIn('total=100; expire=123', subinfo.header(2, 3, 100, 123))

    def test_stun_xor_and_transaction(self):
        tx = b'abcdefghijkl'
        ip = bytes([192, 0, 2, 42])
        mask = struct.pack('!I', 0x2112A442)
        payload = b'\0\1' + b'\0\0' + bytes(a ^ b for a, b in zip(ip, mask))
        data = struct.pack('!HHI', 0x101, 12, 0x2112A442) + tx + struct.pack('!HH', 0x20, 8) + payload
        self.assertEqual(probe.mapped_address(data, tx), '192.0.2.42')
        with self.assertRaises(ValueError): probe.mapped_address(data, b'xxxxxxxxxxxx')


class TransactionTests(unittest.TestCase):
    """Commands are simulated: these exercise file transactions, NOT an Ubuntu install."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='la-vpn-transaction-')
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(self.tmp.cleanup)
        for name, value in {'CONFIG': 'etc-la', 'PROGRAM': 'opt-la', 'STATE': 'state', 'UNITS': 'units',
                            'RUNTIME': 'runtime', 'LOGS': 'logs', 'ARCHIVES': 'archives', 'SYSCTL': 'sysctl.conf'}.items():
            self.stack.enter_context(patch.object(vpn, name, self.root / value))
        vpn.UNITS.mkdir()
        self.stack.enter_context(patch.object(vpn, 'preflight', return_value='ens3'))
        self.stack.enter_context(patch.object(vpn.platform, 'system', return_value='Linux'))
        self.stack.enter_context(patch.object(vpn.os, 'geteuid', return_value=0))
        self.stack.enter_context(patch.object(vpn.os, 'chown'))
        self.stack.enter_context(patch.object(vpn.os, 'fchown'))
        self.stack.enter_context(patch.object(vpn.grp, 'getgrnam', return_value=SimpleNamespace(gr_gid=0)))
        self.commands = self.stack.enter_context(patch.object(vpn, 'run', return_value=SimpleNamespace(returncode=0, stdout='')))
        self.verification = self.stack.enter_context(patch.object(vpn, 'verify'))
        # Firewall commands must remain simulated even when these tests run on
        # a real Linux host with UFW installed.
        self.stack.enter_context(patch.object(vpn.hostfirewall, 'inspect', return_value={
            'status': 'manual-review', 'required_ports': ['TCP 443', 'UDP 443', 'TCP 80'],
            'cloud_status': 'unverified', 'warnings': ['simulated firewall inspection']}))
        self.stack.enter_context(patch.object(vpn, 'verify_started', side_effect=lambda state: vpn.verify(state)))
        self.stack.enter_context(patch.object(vpn, 'download_binary', side_effect=lambda state:
                                             vpn.managed_file(state, vpn.PROGRAM / 'bin/sing-box', b'fake-executable', 0o755)))
        def fake_network(path, shape_mbps=0):
            snap = {'interface': 'ens3', 'before': {}, 'applied': {}, 'qdisc_before': {}, 'qdisc_changed': False}
            network.save(path, snap)
            return snap
        self.stack.enter_context(patch.object(network, 'apply', side_effect=fake_network))
        self.stack.enter_context(patch.object(network, 'check_restore'))
        self.restore = self.stack.enter_context(patch.object(network, 'restore'))
        self.s = example()
        self.s['subscription'] = {'scheme': 'http', 'host': '192.0.2.10', 'port': 80, 'allow_insecure_http': True}
        self.s = render.settings(self.s)

    def test_install_file_transaction(self):
        vpn.install(self.s, '192.0.2.10')
        state = vpn.load_state()
        self.assertEqual(state['status'], 'installed')
        self.assertFalse((vpn.UNITS / 'nginx.service').exists())
        self.assertEqual((vpn.PROGRAM / 'bin').stat().st_mode & 0o777, 0o755)
        self.assertEqual((vpn.CONFIG / 'subscriptions').stat().st_mode & 0o777, 0o750)
        self.verification.assert_called_once()
        for file, digest in state['files'].items(): self.assertEqual(vpn.sha(file), digest)

    def test_failed_validation_archives_and_restores(self):
        self.verification.side_effect = RuntimeError('simulated validation failure')
        with self.assertRaisesRegex(RuntimeError, 'simulated validation'):
            vpn.install(self.s, '192.0.2.10')
        self.restore.assert_called_once()
        self.assertFalse(vpn.CONFIG.exists())
        self.assertFalse(vpn.PROGRAM.exists())
        self.assertFalse(vpn.STATE.exists())
        archived = list(vpn.ARCHIVES.glob('rollback-*/**/secrets.json'))
        self.assertEqual(len(archived), 1, 'Keys must be recoverable, not deleted')

    def test_changed_file_aborts_rollback_before_commands(self):
        vpn.install(self.s, '192.0.2.10')
        target = vpn.CONFIG / 'server.json'
        target.write_text('user changed this')
        self.commands.reset_mock()
        with self.assertRaisesRegex(RuntimeError, 'changed externally'):
            vpn.rollback()
        self.commands.assert_not_called()
        self.assertEqual(target.read_text(), 'user changed this')

    def test_manual_rollback_preserves_keys(self):
        vpn.install(self.s, '192.0.2.10')
        original = (vpn.STATE / 'generated/secrets.json').read_bytes()
        vpn.rollback()
        self.restore.assert_called_once()
        archived = list(vpn.ARCHIVES.glob('rollback-*/**/secrets.json'))
        self.assertEqual(archived[0].read_bytes(), original)
        self.assertFalse((vpn.UNITS / 'la-vpn.service').exists())

    def test_native_install_transaction_has_no_project_sources(self):
        binary = self.root / 'gowest-input'
        binary.write_bytes(b'native-test-executable')
        with patch.object(vpn.kit_runtime, 'compiled', return_value=True), \
                patch.object(vpn.kit_runtime, 'executable', return_value=binary):
            vpn.install(self.s, '192.0.2.10')
        self.assertEqual((vpn.PROGRAM / 'gowest').read_bytes(), binary.read_bytes())
        self.assertFalse(list(vpn.PROGRAM.rglob('*.py')))
        self.assertFalse(list(vpn.PROGRAM.rglob('*.sh')))
        calls = [call.args[0] for call in self.commands.call_args_list]
        guard = next(call for call in calls if call[0] == 'systemd-run')
        self.assertEqual(guard[-3:], [str(vpn.PROGRAM / 'gowest'), 'rollback', '--automatic'])
        self.assertNotIn('python3-yaml', next(call for call in calls if call[:2] == ['apt-get', 'install']))
        self.assertIn('gowest _subinfo', (vpn.UNITS / 'la-vpn-subinfo.service').read_text())
        self.assertIn('本机安装完成', (vpn.STATE / 'install.log').read_text())

    def test_failure_log_survives_archive_and_records_rollback_result(self):
        self.verification.side_effect = RuntimeError('simulated failure for logs')
        with self.assertRaises(RuntimeError):
            vpn.install(self.s, '192.0.2.10')
        files = list(vpn.ARCHIVES.glob('rollback-*/**/install.log'))
        self.assertEqual(len(files), 1)
        text = files[0].read_text()
        self.assertIn('回滚完成', text)
        self.assertIn('7/8', text)
        self.assertNotIn('本机安装完成', text)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)


class LauncherTests(unittest.TestCase):
    def test_http_needs_acknowledgment(self):
        with self.assertRaises(ValueError): launcher.build_settings('8.8.8.8', 10, 30, 'http')

    def test_http_configuration_only(self):
        cfg = launcher.build_settings('8.8.8.8', 10, 30, 'http', accept_http_risk=True)
        self.assertEqual(cfg['subscription']['host'], '8.8.8.8')
        self.assertEqual(cfg['subscription']['port'], 80)
        self.assertEqual(cfg['hy2_up_mbps'], 10)

    def test_https_settings(self):
        cfg = launcher.build_settings('8.8.8.8', 10, 30, 'https', host='vpn.example.com',
                                      cert='/root/cert.pem', key='/root/key.pem')
        self.assertEqual(cfg['subscription']['port'], 8443)
        self.assertNotIn('allow_insecure_http', cfg['subscription'])

    def test_reject_example_private_or_malicious_ip(self):
        for address in ('192.0.2.10', '127.0.0.1', '10.1.2.3', '::1', '1.2.3.4;id'):
            with self.assertRaises(ValueError):
                launcher.build_settings(address, 10, 30, 'http', accept_http_risk=True)

    def test_port_zero_is_not_silently_replaced(self):
        with self.assertRaises(ValueError):
            launcher.build_settings('8.8.8.8', 10, 30, 'http', accept_http_risk=True, subscription_port=0)

    def test_saved_config_cannot_overwrite(self):
        cfg = launcher.build_settings('8.8.8.8', 10, 30, 'http', accept_http_risk=True)
        with tempfile.TemporaryDirectory() as t:
            target = Path(t) / 'params.json'
            launcher.save_settings(cfg, target)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError): launcher.save_settings(cfg, target)

    def test_dispatch_uses_same_python_and_argument_boundaries(self):
        with patch.object(launcher.os, 'execv') as execute:
            launcher.dispatch(['plan', '--settings', '/path with spaces/settings.json'])
            self.assertEqual(execute.call_args.args[1][-1], '/path with spaces/settings.json')
            self.assertEqual(execute.call_args.args[0], sys.executable)

    def test_noninteractive_default_does_not_install(self):
        with patch.object(launcher.sys.stdin, 'isatty', return_value=False), patch.object(launcher, 'dispatch') as dispatch:
            with self.assertRaises(ValueError): launcher.main([])
            dispatch.assert_not_called()

    def test_shell_help_from_other_directory(self):
        import subprocess
        entry = Path(launcher.__file__).parent / 'deploy.sh'
        with tempfile.TemporaryDirectory() as t:
            p = subprocess.run(['sh', str(entry), '--help'], cwd=t, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0)
            self.assertIn('init', p.stdout)
            self.assertFalse((Path(t) / 'settings.local.json').exists())


if __name__ == '__main__':
    unittest.main()
