import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout, ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import autoconfig
import check
import firewall
import hostfirewall
import launcher
import render
import vpn


def config(both=False):
    return launcher.build_settings('8.8.4.4', 10, 30, 'both' if both else 'http',
        host='sub.example.com' if both else '', cert='/root/fullchain.pem', key='/root/key.pem',
        accept_http_risk=True)


class DiscoveryTests(unittest.TestCase):
    def test_two_sources_must_agree(self):
        with patch.object(autoconfig.platform, 'system', return_value='Linux'), \
                patch.object(autoconfig, 'query_ip', side_effect=['8.8.8.8', '8.8.8.8', '1.1.1.1']):
            self.assertEqual(autoconfig.detect_public_ipv4()['address'], '8.8.8.8')

    def test_timeout_can_use_unique_local_public_address(self):
        with patch.object(autoconfig.platform, 'system', return_value='Linux'), \
                patch.object(autoconfig, 'query_ip', side_effect=TimeoutError), \
                patch.object(autoconfig, 'local_public_ips', return_value={'8.8.4.4'}):
            self.assertEqual(autoconfig.detect_public_ipv4()['address'], '8.8.4.4')

    def test_conflict_not_guessed(self):
        with patch.object(autoconfig.platform, 'system', return_value='Linux'), \
                patch.object(autoconfig, 'query_ip', side_effect=['8.8.8.8', '8.8.4.4', '1.1.1.1']), \
                patch.object(autoconfig, 'local_public_ips', return_value={'8.8.4.4'}):
            with self.assertRaises(ValueError):
                autoconfig.detect_public_ipv4()

    def test_non_linux_does_not_query_local_machine_exit(self):
        with patch.object(autoconfig.platform, 'system', return_value='Darwin'), \
                patch.object(autoconfig, 'query_ip') as query:
            with self.assertRaises(ValueError):
                autoconfig.detect_public_ipv4()
            query.assert_not_called()

    def test_reject_private_invalid_or_ipv6(self):
        for value in ('10.0.0.1', '127.0.0.1', '192.0.2.1', '::1', '8.8.8.8 junk'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                autoconfig.public_ipv4(value)

    def test_query_disables_proxy_redirect_and_bounds_response(self):
        response = SimpleNamespace(read=lambda size: b'8.8.4.4\n')
        from unittest.mock import MagicMock
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch.object(autoconfig.urllib.request, 'build_opener', return_value=opener) as build:
            self.assertEqual(autoconfig.query_ip(autoconfig.IP_SOURCES[0]), '8.8.4.4')
            self.assertEqual(build.call_args.args[0].proxies, {})
            self.assertIsInstance(build.call_args.args[1], autoconfig.NoRedirect)
            self.assertEqual(opener.open.call_args.kwargs['timeout'], 4)

    def test_init_can_detect_ip_but_explicit_ip_stays_offline(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), \
                patch.object(autoconfig, 'detect_public_ipv4', return_value={'address':'8.8.4.4'}) as detect:
            common = ['init', '--up', '10', '--down', '30', '--http', '--accept-http-risk']
            launcher.main(common + ['--output', str(Path(directory)/'auto.json')])
            self.assertEqual(detect.call_count, 1)
            launcher.main(common + ['--server-ip', '1.1.1.1', '--output', str(Path(directory)/'explicit.json')])
            self.assertEqual(detect.call_count, 1)

    def test_wizard_detects_ip_and_accepts_multi_select_without_retyping_ip(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), \
                patch.object(launcher.sys.stdin,'isatty',return_value=True), \
                patch.object(autoconfig,'detect_public_ipv4',return_value={'address':'8.8.4.4','source':'test'}), \
                patch.object(launcher.platform,'system',return_value='Linux'), \
                patch.object(launcher.os,'geteuid',return_value=0), \
                patch.object(launcher.subprocess,'run'), patch.object(launcher,'dispatch') as dispatch, \
                patch('builtins.input',side_effect=[str(Path(directory)/'params.json'),'','', '30Mbps',
                    '1,2','sub.example.com','/root/fullchain.pem','/root/key.pem','HTTP','','INSTALL']):
            launcher.wizard()
            cfg=json.loads((Path(directory)/'params.json').read_text())
            self.assertEqual(cfg['server_ip'],'8.8.4.4')
            self.assertEqual((cfg['hy2_up_mbps'], cfg['hy2_down_mbps']), (30, 30))
            self.assertEqual(cfg['subscription']['http_port'],80)
            self.assertEqual(dispatch.call_args.args[0][-1],'8.8.4.4')


class MultiTransportTests(unittest.TestCase):
    def test_dual_needs_risk_ack_and_certificate(self):
        with self.assertRaises(ValueError):
            launcher.build_settings('8.8.8.8',10,30,'both',host='sub.example.com',
                                    cert='/root/fullchain.pem',key='/root/key.pem')
        with self.assertRaises(ValueError):
            launcher.build_settings('8.8.8.8',10,30,'both',accept_http_risk=True)

    def test_distinct_ports_and_https_primary(self):
        cfg = config(True)
        self.assertEqual(render.subscription_endpoints(cfg),
            [{'scheme':'https','host':'sub.example.com','port':8443},
             {'scheme':'http','host':'sub.example.com','port':80}])
        for port in (22,443,8443,0,65536,True):
            cfg['subscription']['http_port'] = port
            with self.subTest(port=port), self.assertRaises(ValueError):
                render.settings(cfg)

    def test_dual_renders_same_five_files_and_two_listeners(self):
        with tempfile.TemporaryDirectory() as directory:
            out = render.render(config(True), Path(directory)/'new')
            report = check.check(out)
            self.assertEqual(report['cross_config_checks'], 'passed')
            nginx = (out/'nginx.conf').read_text()
            self.assertIn('listen 80;', nginx)
            self.assertIn('listen 8443 ssl;', nginx)
            self.assertEqual(nginx.count('location = /sub/'), 5)
            entries = json.loads((out/'transport-urls.json').read_text())
            self.assertEqual([urlsplit(x['base']).scheme for x in entries], ['https','http'])
            self.assertEqual(urlsplit(entries[0]['base']).path,urlsplit(entries[1]['base']).path)
            self.assertEqual((out/'transport-urls.json').stat().st_mode & 0o777,0o600)
            self.assertEqual(len((out/'subscription-urls.txt').read_text().splitlines()),2)
            self.assertIn('clashmeta://', (out/'transport-import.txt').read_text())

    def test_dual_cloud_template_has_all_required_ports(self):
        cfg = config(True)
        doc = firewall.generate(cfg, ['198.51.100.1/32'])
        self.assertTrue(firewall.check(cfg,doc)['valid'])
        self.assertEqual([x['协议及端口'] for x in doc['data']],
                         ['TCP:22','TCP:8443','TCP:80','TCP:443','UDP:443','ICMP'])
        self.assertEqual(hostfirewall.required_ports(cfg), [('tcp',443),('udp',443),('tcp',8443),('tcp',80)])

    def test_dual_urls_commands_show_both_and_filter(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(vpn,'CONFIG',Path(directory)), patch.object(vpn,'STATE',Path(directory)):
            out = render.render(config(True),Path(directory)/'generated')
            (Path(directory)/'settings.json').write_text(render.dump(config(True)))
            stream = io.StringIO()
            with redirect_stdout(stream):
                vpn.show_transports('all','clash')
            self.assertIn('normal: https://',stream.getvalue())
            self.assertIn('normal: http://',stream.getvalue())
            stream = io.StringIO()
            with redirect_stdout(stream):
                vpn.show_transports('http','android')
            self.assertIn('clashmeta://',stream.getvalue())


class FirewallAutomationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.files = tuple(Path(self.tmp.name)/name for name in ('user.rules','user6.rules'))
        for file in self.files:
            file.write_text('# empty test rules\n')
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(hostfirewall,'UFW_FILES',self.files))
        self.stack.enter_context(patch.object(hostfirewall.shutil,'which',side_effect=lambda name: '/usr/sbin/'+name if name=='ufw' else None))
        self.status = 'Status: active\nDefault: deny (incoming), allow (outgoing), deny (routed)\n'
        self.commands = []
        self.stack.enter_context(patch.object(hostfirewall,'run',side_effect=self.command))

    def command(self,args):
        self.commands.append(args)
        if args[:2] == ['ufw','status']:
            tags = ''.join(file.read_text() for file in self.files)
            return SimpleNamespace(returncode=0,stdout=self.status+tags,stderr='')
        if args[:2] == ['ufw','allow']:
            tag=args[-1]
            for file in self.files:
                file.write_text(file.read_text()+' '.join(args[1:])+'\n'+tag+'\n')
        elif args[:3] == ['ufw','--force','delete']:
            for file in self.files:
                file.write_text(file.read_text().replace(' '.join(args[3:])+'\n'+args[-1]+'\n',''))
        else:
            raise AssertionError('Unexpected firewall command: '+str(args))
        return SimpleNamespace(returncode=0,stdout='Rule added',stderr='')

    def test_only_narrow_rules_added_and_rolled_back(self):
        before = [p.read_bytes() for p in self.files]
        state={}
        report=hostfirewall.apply(config(True),state,lambda s:None)
        self.assertEqual(report['cloud_status'],'unverified')
        self.assertEqual(len(state['host_firewall']['rules']),4)
        adds=[a for a in self.commands if a[:2]==['ufw','allow']]
        self.assertEqual({a[a.index('port')+1] for a in adds},{'443','80','8443'})
        self.assertFalse(any(x in ('enable','disable','reset','reload','flush') for a in self.commands for x in a))
        hostfirewall.restore(state,lambda s:None)
        self.assertEqual([p.read_bytes() for p in self.files],before)

    def test_explicit_deny_is_not_bypassed(self):
        self.status += '443/tcp DENY IN Anywhere\n'
        state={}
        report=hostfirewall.apply(config(),state,lambda s:None)
        self.assertEqual(report['status'],'manual-review')
        self.assertFalse(state['host_firewall']['changed'])
        self.assertFalse(any(a[:2]==['ufw','allow'] for a in self.commands))

    def test_existing_restricted_rule_never_relabelled_or_deleted(self):
        self.files[0].write_text('### tuple ### allow tcp 443 0.0.0.0/0 any 1.2.3.4 in\n# administrator\n')
        state={}
        hostfirewall.apply(config(),state,lambda s:None)
        self.assertFalse(any(a[:2]==['ufw','allow'] and a[4]=='tcp' and a[a.index('port')+1]=='443' for a in self.commands))
        hostfirewall.restore(state,lambda s:None)
        self.assertIn('# administrator',self.files[0].read_text())

    def test_range_any_and_v6_overlap(self):
        for proto,ports in [('tcp','400:500'),('any','any'),('tcp','80,443')]:
            self.files[1].write_text(f'### tuple ### allow {proto} {ports} ::/0 any ::/0 in\n')
            self.assertTrue(hostfirewall.overlaps_existing('tcp',443))

    def test_check_mode_never_writes(self):
        state={}
        report=hostfirewall.apply(config(),state,lambda s:None,'check')
        self.assertEqual(report['status'],'check-only-no-changes')
        self.assertFalse(any(a[:2]==['ufw','allow'] for a in self.commands))

    def test_drift_blocks_rollback_before_mutation(self):
        state={}
        hostfirewall.apply(config(),state,lambda s:None)
        self.files[0].write_text(self.files[0].read_text()+'# later admin change\n')
        self.commands.clear()
        with self.assertRaises(RuntimeError):
            hostfirewall.restore(state,lambda s:None)
        self.assertFalse(self.commands)

    def test_incomplete_intent_requires_review(self):
        state={'host_firewall':{'rules':[{'status':'intent'}]}}
        with self.assertRaises(RuntimeError):
            hostfirewall.check_restore(state)

    def test_native_firewall_not_disabled_and_unknown_not_open(self):
        with patch.object(hostfirewall.shutil,'which',return_value=None):
            self.assertEqual(hostfirewall.inspect(config())['status'],'manual-review')
        with patch.object(hostfirewall.shutil,'which',side_effect=lambda name:name if name=='nft' else None), \
                patch.object(hostfirewall,'run',return_value=SimpleNamespace(returncode=0,stdout='table inet custom {}')):
            self.assertEqual(hostfirewall.inspect(config())['status'],'manual-review')

    def test_empty_nft_still_cannot_verify_cloud(self):
        with patch.object(hostfirewall.shutil,'which',side_effect=lambda name:name if name=='nft' else None), \
                patch.object(hostfirewall,'run',return_value=SimpleNamespace(returncode=0,stdout='')):
            report=hostfirewall.inspect(config())
            self.assertEqual(report['status'],'no-filter-rules-observed')
            self.assertEqual(report['cloud_status'],'unverified')

    def test_inspection_timeout_is_reported_not_assumed_open(self):
        with patch.object(hostfirewall,'run',side_effect=subprocess.TimeoutExpired('ufw',20)):
            self.assertEqual(hostfirewall.inspect(config())['status'],'manual-review')

    def test_unknown_tuple_warns_before_any_changes(self):
        self.files[0].write_text('### tuple ### unrecognized\n')
        state={}
        report=hostfirewall.apply(config(),state,lambda s:None)
        self.assertEqual(report['status'],'manual-review')
        self.assertFalse(any(a[:2]==['ufw','allow'] for a in self.commands))


if __name__ == '__main__':
    unittest.main()
