import base64
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urlsplit, parse_qs, unquote

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import render
import routing
import vpn


class SubscriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='la-vpn-sub040-')
        cls.s = render.settings(json.loads((Path(render.__file__).parent / 'settings.example.json').read_text()))
        cls.out = render.render(cls.s, Path(cls.tmp.name) / 'generated')
        cls.k = json.loads((cls.out / 'secrets.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_block_yaml_not_json_and_roundtrip(self):
        for name, strict in [('normal.yaml', False), ('strict.yaml', True)]:
            text = (self.out / name).read_text()
            self.assertIn('\nproxies:\n', text)
            self.assertIn('\n- name:', text)
            with self.assertRaises(json.JSONDecodeError):
                json.loads(text)
            self.assertEqual(yaml.safe_load(text), render.client_config(self.s, self.k, strict))

    def test_all_client_files_private(self):
        for name in list(render.SUBSCRIPTION_FILES.values()) + ['client-urls.txt']:
            self.assertEqual((self.out / name).stat().st_mode & 0o777, 0o600)

    def test_native_uris_and_credentials(self):
        raw = (self.out / 'nodes.txt').read_text().strip()
        hy, vl = map(urlsplit, base64.b64decode(raw, validate=True).decode().splitlines())
        self.assertEqual((hy.scheme, vl.scheme), ('hysteria2', 'vless'))
        for uri in (hy, vl):
            self.assertEqual(uri.hostname, self.s['server_ip'])
            self.assertEqual(uri.port, 443)
        self.assertEqual(unquote(hy.username), self.k['hy2_password'])
        self.assertEqual(parse_qs(hy.query)['obfs-password'], [self.k['obfs_password']])
        self.assertEqual(parse_qs(hy.query)['pinSHA256'], [self.k['certificate_fingerprint']])
        self.assertEqual(vl.username, self.k['uuid'])
        self.assertEqual(parse_qs(vl.query)['pbk'], [self.k['reality_public_key']])
        self.assertEqual(parse_qs(vl.query)['sid'], [self.k['short_id']])
        self.assertEqual(parse_qs(vl.query)['flow'], ['xtls-rprx-vision'])

    def test_uri_reserved_characters_encoded(self):
        keys = copy.deepcopy(self.k)
        keys['hy2_password'] = 'a@b:c/?#&+ %中文'
        keys['obfs_password'] = 'obfs&+?/=#'
        hy = urlsplit(base64.b64decode(render.node_subscription(self.s, keys)).decode().splitlines()[0])
        self.assertEqual(unquote(hy.username), keys['hy2_password'])
        self.assertEqual(parse_qs(hy.query)['obfs-password'], [keys['obfs_password']])

    def test_legacy_urls_preserved_extra_urls_separate(self):
        old = (self.out / 'subscription-urls.txt').read_text().splitlines()
        self.assertEqual(len(old), 2)
        base = old[0].split(': ', 1)[1]
        self.assertEqual(old[1], 'strict: ' + base + '-us')
        extras = (self.out / 'client-urls.txt').read_text().splitlines()
        self.assertEqual(len(extras), 3)
        self.assertIn('nodes.txt: ' + base + '-nodes', extras)

    def test_exact_locations_headers_and_no_ua_guessing(self):
        text = (self.out / 'nginx.conf').read_text()
        for suffix, name in render.SUBSCRIPTION_FILES.items():
            self.assertIn('location = /sub/' + self.k['subscription_token'] + suffix + ' {', text)
            self.assertIn('alias /etc/la-vpn/subscriptions/' + name + ';', text)
            self.assertIn('filename="LA-' + name + '"', text)
        self.assertIn('access_log off;', text)
        self.assertNotIn('http_user_agent', text)

    def test_scene_rules_before_cn_and_no_remote_dependencies(self):
        cfg = render.client_config(self.s, self.k)
        boundary = cfg['rules'].index('GEOSITE,cn,DIRECT')
        for rule in routing.scene_rules():
            self.assertLess(cfg['rules'].index(rule), boundary)
        for key in ('rule-providers', 'proxy-providers', 'script'):
            self.assertNotIn(key, cfg)
        for domain, target in [('chatgpt.com', 'AI'), ('generativelanguage.googleapis.com', 'AI'),
                               ('googlevideo.com', 'YouTube'), ('tiktokshop.com', 'TikTok')]:
            self.assertIn(f'DOMAIN-SUFFIX,{domain},{target}', cfg['rules'])

    def test_all_scenes_default_to_same_server_auto(self):
        for strict in (False, True):
            cfg = render.client_config(self.s, self.k, strict)
            groups = {g['name']: g for g in cfg['proxy-groups']}
            self.assertEqual(groups['TikTok'], {'name': 'TikTok', 'type': 'select',
                                               'proxies': ['自动回退', 'LA-Hy2', 'LA-VLESS']})
            for name in routing.SCENES:
                self.assertNotIn('DIRECT', groups[name]['proxies'])
            if strict:
                self.assertEqual(groups['出口选择']['proxies'][0], '自动回退')
                self.assertNotIn('DIRECT', groups['出口选择']['proxies'])

    def test_dns_matches_scenes(self):
        cfg = render.client_config(self.s, self.k)
        for group, domains in routing.SCENES.items():
            for name in domains:
                self.assertEqual(cfg['dns']['nameserver-policy']['+.' + name],
                                 'https://1.1.1.1/dns-query#' + group)

    def test_no_overbroad_ai_infrastructure(self):
        for domain in ('google.com', 'googleapis.com', 'githubusercontent.com',
                       'microsoft.com', 'ubuntu.com', 'sentry.io', 'gstatic.com'):
            self.assertNotIn(domain, routing.AI + routing.CLAUDE)
        cfg = render.client_config(self.s, self.k)
        self.assertFalse(any(x.startswith('IP-ASN,') for x in cfg['rules']))

    def test_shadowrocket_same_scenes_no_clash_only_syntax(self):
        for strict, name in [(False, 'shadowrocket.conf'), (True, 'shadowrocket-us.conf')]:
            text = (self.out / name).read_text()
            self.assertIn('[General]', text)
            self.assertIn('[Proxy Group]', text)
            self.assertIn('[Rule]', text)
            self.assertIn('TikTok = select, 自动回退, LA-Hy2, LA-VLESS, policy-select-name=自动回退', text)
            for rule in routing.scene_rules():
                self.assertIn('\n' + rule + '\n', text)
            for token in ('GEOSITE,', 'MATCH,', 'PROCESS-NAME,', '[MITM]', '[Script]', 'policy-regex-filter'):
                self.assertNotIn(token, text)
            self.assertIn('FINAL,出口选择', text)
            self.assertEqual('GEOIP,CN,DIRECT' in text, not strict)

    def test_shadowrocket_dns_fallback_stays_same_server(self):
        text = render.shadowrocket_config(self.s, self.k, strict=True)
        self.assertIn('dns-server = https://1.1.1.1/dns-query#proxy=LA-Hy2', text)
        self.assertIn('fallback-dns-server = https://1.1.1.1/dns-query#proxy=LA-VLESS', text)
        self.assertNotIn('= system', text)
        self.assertNotIn('direct-dns-server', text)
        self.assertIn('udp-policy-not-supported-behaviour = REJECT', text)
        self.assertIn('block-quic = always-allow', text)

    def test_static_names_resolve_to_same_nodes(self):
        cfg = render.client_config(self.s, self.k)
        names = {p['name'] for p in cfg['proxies']} | {g['name'] for g in cfg['proxy-groups']} | {'DIRECT'}
        for group in cfg['proxy-groups']:
            self.assertTrue(set(group['proxies']) <= names)
        self.assertEqual(len(names), len(cfg['proxies']) + len(cfg['proxy-groups']) + 1)


if __name__ == '__main__':
    unittest.main()
