import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import firewall
import launcher
import probe

ROOT = Path(__file__).resolve().parents[1]


def settings(https=False):
    name = 'settings.example.json' if https else 'settings.http.example.json'
    return json.loads((ROOT / name).read_text())


class FirewallTests(unittest.TestCase):
    def test_http_ports_and_exact_schema(self):
        rules = firewall.generate(settings(), ['198.51.100.7/32'])
        self.assertEqual([x['协议及端口'] for x in rules['data']],
                         ['TCP:22', 'TCP:80', 'TCP:443', 'UDP:443', 'ICMP'])
        for row in rules['data']:
            self.assertEqual(set(row), firewall.FIELDS)
        report = firewall.check(settings(), rules)
        self.assertTrue(report['valid'])
        self.assertFalse(report['world_open_ssh'])
        self.assertFalse(report['rules_applied'])

    def test_static_templates_match_generator(self):
        for https, name in [(False, 'Firewall-Rule-Template-json.json'),
                            (True, 'Firewall-Rule-Template-https-json.json')]:
            with self.subTest(https=https):
                document = json.loads((ROOT / 'firewall' / name).read_text())
                self.assertEqual(document, firewall.generate(settings(https), allow_world_ssh=True))

    def test_https_and_custom_subscription_ports(self):
        for port in (8443, 9443, 8080):
            cfg = settings(True)
            cfg['subscription']['port'] = port
            doc = firewall.generate(cfg, ['198.51.100.7/32'])
            protocols = [r['协议及端口'] for r in doc['data']]
            self.assertIn(f'TCP:{port}', protocols)
            self.assertNotIn('TCP:80', protocols)

    def test_world_ssh_requires_explicit_acknowledgment(self):
        for sources in (None, ['0.0.0.0/0'], ['0.0.0.0/1', '128.0.0.0/1']):
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                firewall.generate(settings(), sources)
        doc = firewall.generate(settings(), allow_world_ssh=True)
        self.assertTrue(firewall.check(settings(), doc)['world_open_ssh'])

    def test_multiple_ssh_sources(self):
        doc = firewall.generate(settings(), ['198.51.100.7/32', '203.0.113.9/32'])
        self.assertEqual(len(doc['data']), 6)
        self.assertTrue(firewall.check(settings(), doc)['valid'])

    def test_ipv4_cidr_is_explicit_and_not_widened(self):
        for source in ('198.51.100.7', '198.51.100.7/24', '::/0', 'junk', 123):
            with self.subTest(source=source), self.assertRaises(ValueError):
                firewall.generate(settings(), [source])

    def test_custom_ssh_port_and_conflicts(self):
        doc = firewall.generate(settings(), ['198.51.100.7/32'], ssh_port=2222)
        self.assertTrue(firewall.check(settings(), doc, ssh_port=2222)['valid'])
        self.assertFalse(firewall.check(settings(), doc)['valid'])
        for port in (0, -1, 65536, True, 443, 80):
            with self.subTest(port=port), self.assertRaises(ValueError):
                firewall.generate(settings(), ['198.51.100.7/32'], ssh_port=port)

    def test_missing_required_rule(self):
        document = firewall.generate(settings(), allow_world_ssh=True)
        for index in range(5):
            doc = copy.deepcopy(document)
            doc['data'].pop(index)
            self.assertFalse(firewall.check(settings(), doc)['valid'])

    def test_unexpected_ports_rejected(self):
        for protocol in ('TCP:3389', 'TCP:8443', 'UDP:22', 'ALL', 'TCP:1-65535', 'TCP:99999'):
            doc = firewall.generate(settings(), allow_world_ssh=True)
            doc['data'].append(firewall.rule(protocol, firewall.WORLD, 'extra'))
            self.assertFalse(firewall.check(settings(), doc)['valid'], protocol)

    def test_duplicate_rules_rejected(self):
        with self.assertRaises(ValueError):
            firewall.generate(settings(), ['198.51.100.7/32', '198.51.100.7/32'])
        doc = firewall.generate(settings(), allow_world_ssh=True)
        doc['data'].append(copy.deepcopy(doc['data'][2]))
        self.assertFalse(firewall.check(settings(), doc)['valid'])

    def test_bad_schema_and_rule_values(self):
        for doc in (None, [], {}, {'data': {}}, {'data': [], 'other': []}, {'data': [None]}):
            self.assertFalse(firewall.check(settings(), doc)['valid'])
        for field, value in [('策略', '拒绝'), ('优先级', '中'), ('源地址', '::/0'),
                             ('协议及端口', 'TCP: 22'), ('备注', {})]:
            doc = firewall.generate(settings(), allow_world_ssh=True)
            doc['data'][0][field] = value
            self.assertFalse(firewall.check(settings(), doc)['valid'])

    def test_subscription_and_proxy_must_match_template_scope(self):
        doc = firewall.generate(settings(), allow_world_ssh=True)
        doc['data'][1]['源地址'] = '198.51.100.7/32'
        self.assertFalse(firewall.check(settings(), doc)['valid'])
        self.assertFalse(firewall.check(settings(True), doc)['valid'])

    def test_generator_never_overwrites_files_or_symlinks(self):
        with tempfile.TemporaryDirectory() as t, redirect_stdout(io.StringIO()):
            path = Path(t) / 'rules.json'
            args = ['generate', '--settings', str(ROOT / 'settings.http.example.json'),
                    '--allow-world-ssh', '--output', str(path)]
            self.assertEqual(firewall.main(args), 0)
            original = path.read_bytes()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                firewall.main(args)
            self.assertEqual(path.read_bytes(), original)
            link = Path(t) / 'rules-link.json'
            link.symlink_to(path)
            args[-1] = str(link)
            with self.assertRaises(FileExistsError):
                firewall.main(args)
            self.assertEqual(path.read_bytes(), original)

    def test_launcher_firewall_exit_code(self):
        args = ['check', '--settings', str(ROOT / 'settings.http.example.json'),
                '--file', str(ROOT / 'firewall/Firewall-Rule-Template-json.json')]
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exc:
            launcher.main(['firewall'] + args)
        self.assertEqual(exc.exception.code, 0)
        args[2] = str(ROOT / 'settings.example.json')
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exc:
            launcher.main(['firewall'] + args)
        self.assertEqual(exc.exception.code, 1)

    def test_node_prefix_in_init(self):
        with tempfile.TemporaryDirectory() as t, redirect_stdout(io.StringIO()):
            output = Path(t) / 'settings.json'
            launcher.main(['init', '--server-ip', '8.8.8.8', '--node-prefix', 'WSD', '--up', '10',
                           '--down', '30', '--http', '--accept-http-risk', '--output', str(output)])
            self.assertEqual(json.loads(output.read_text())['node_prefix'], 'WSD')

    def test_invalid_node_prefix_not_written(self):
        with self.assertRaises(ValueError):
            launcher.build_settings('8.8.8.8', 10, 30, 'http', accept_http_risk=True,
                                    node_prefix='bad prefix')


class ProbeTests(unittest.TestCase):
    def response(self, text='200\t1048576\t2.0\t524288\t0.5', code=0):
        return SimpleNamespace(stdout=text, returncode=code)

    def test_complete_download(self):
        with patch.object(probe.subprocess, 'run', return_value=self.response()) as run:
            row = probe.download_round(['curl', '-q'], 1048576, 1)
        self.assertTrue(row['completed'])
        self.assertEqual(row['download_mbps'], 4.19)
        self.assertIn('--max-filesize', run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['timeout'], 35)

    def test_partial_or_http_error_not_success(self):
        for response in (self.response('200\t500\t2\t250'), self.response(code=28),
                         self.response('403\t1048576\t2\t524288'), self.response(code=22)):
            with patch.object(probe.subprocess, 'run', return_value=response):
                row = probe.download_round(['curl'], 1048576, 1)
                self.assertFalse(row['completed'])
                self.assertNotIn('download_mbps', row)

    def test_bad_metrics_not_success(self):
        for raw in ('oops', '200\t1048576\t0\t524288', '200\t1048576\t2\tNaN',
                    '200\t1048576\t2\tinf', '200\t-1\t2\t100', '200\t1048576\t2\t0'):
            with patch.object(probe.subprocess, 'run', return_value=self.response(raw)):
                self.assertFalse(probe.download_round(['curl'], 1048576, 1)['completed'])

    def test_timeout_not_success(self):
        with patch.object(probe.subprocess, 'run', side_effect=subprocess.TimeoutExpired('curl', 35)):
            self.assertFalse(probe.download_round(['curl'], 1048576, 1)['completed'])

    def test_statistics_keep_failure_visible(self):
        report = probe.summarize_downloads([{'completed': True, 'download_mbps': 10},
                                           {'completed': False}, {'completed': True, 'download_mbps': 20}])
        self.assertFalse(report['all_completed'])
        self.assertEqual(report['failed'], 1)
        self.assertEqual((report['min_mbps'], report['median_mbps'], report['max_mbps']), (10, 15, 20))
        self.assertIsNone(probe.summarize_downloads([{'completed': False}])['median_mbps'])

    def test_expected_exit_mismatch_skips_download_and_stun(self):
        stream = io.StringIO()
        with redirect_stdout(stream), patch.object(probe.subprocess, 'run', return_value=self.response('ip=8.8.8.8')), \
                patch.object(probe, 'download_round') as download, patch.object(probe, 'stun') as stun:
            code = probe.main(['--expect-exit-ip', '1.1.1.1', '--download-mib', '4', '--stun'])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(stream.getvalue())['exit_ip_matches'])
        download.assert_not_called()
        stun.assert_not_called()

    def test_rounds_use_same_proxy_and_explicit_budget(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()), \
                patch.object(probe.subprocess, 'run', return_value=self.response('ip=8.8.8.8')) as curl, \
                patch.object(probe, 'download_round', side_effect=[{'completed': True, 'download_mbps': 10},
                                                                  {'completed': False}]) as download, \
                patch.object(probe.time, 'sleep') as sleep:
            code = probe.main(['--download-mib', '8', '--rounds', '2', '--interval', '3'])
        self.assertEqual(code, 1)
        self.assertEqual(curl.call_count, 1)
        self.assertEqual(download.call_count, 2)
        self.assertEqual(download.call_args_list[0].args[0], download.call_args_list[1].args[0])
        self.assertEqual(download.call_args.args[0][:2], ['curl', '-q'])
        sleep.assert_called_once_with(3)
        data = json.loads(output.getvalue())
        self.assertEqual(data['download_budget_mib'], 16)
        self.assertEqual(data['download_summary']['failed'], 1)

    def test_invalid_arguments_make_no_requests(self):
        for args in (['--rounds', '2'], ['--download-mib', '16'], ['--rounds', '11'],
                     ['--interval', '61'], ['--proxy', 'http://example.com:7897'],
                     ['--proxy', 'http://user:password@127.0.0.1:7897']):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), \
                    patch.object(probe.subprocess, 'run') as run, self.assertRaises(SystemExit):
                probe.main(args)
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
