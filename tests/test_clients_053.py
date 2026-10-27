import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import launcher
import render
import vpn

MIHOMO_RATE = re.compile(r'^(\d+)\s*([KMGT]?)([Bb])ps$')  # Mihomo common/utils/mbps.go


class Release053Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='clients053-')
        raw = json.loads((ROOT / 'settings.example.json').read_text())
        raw['node_prefix'] = 'DC'
        cls.out = render.render(raw, Path(cls.temp.name) / 'generated')
        cls.normal = yaml.safe_load((cls.out / 'normal.yaml').read_text())
        cls.strict = yaml.safe_load((cls.out / 'strict.yaml').read_text())
        cls.base = (cls.out / 'subscription-urls.txt').read_text().splitlines()[0].split(': ', 1)[1]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_bandwidth_is_whole_mbps_parseable_by_mihomo(self):
        for up, down in [(10, 30), (10.0, 30.0)]:
            cfg = launcher.build_settings('8.8.4.4', up, down, 'http', accept_http_risk=True)
            self.assertIs(type(cfg['hy2_up_mbps']), int)
        hy = next(p for p in self.normal['proxies'] if p['type'] == 'hysteria2')
        self.assertRegex(hy['up'], MIHOMO_RATE)
        self.assertRegex(hy['down'], MIHOMO_RATE)
        with self.assertRaises(ValueError):
            launcher.build_settings('8.8.4.4', 12.5, 30, 'http', accept_http_risk=True)

    def test_normal_dns_only_mainland_domains_use_mainland_doh(self):
        dns = self.normal['dns']
        self.assertTrue(all('#出口选择' in s for s in dns['nameserver']))
        self.assertEqual(dns['nameserver-policy']['geosite:cn'],
                         ['https://dns.alidns.com/dns-query', 'https://doh.pub/dns-query'])
        self.assertNotIn('geosite:cn', self.strict['dns']['nameserver-policy'])

    def test_tailscale_direct_on_all_mihomo_platforms(self):
        for name in ('tailscaled', 'tailscaled.exe', 'tailscale-ipn.exe', 'com.tailscale.ipn'):
            self.assertIn('PROCESS-NAME,' + name + ',DIRECT', self.normal['rules'])

    def test_install_links_round_trip_for_verge_and_cmfa(self):
        guide = (self.out / 'client-import.txt').read_text()
        self.assertEqual((self.out / 'client-import.txt').stat().st_mode & 0o777, 0o600)
        links = re.findall(r'(clash(?:meta)?://install-config\?\S+)', guide)
        self.assertEqual(len(links), 4)
        for link in links:
            query = urlsplit(link).query
            # Clash Verge Rev: everything after the first url= is the address.
            verge_url = unquote(query[query.find('url=') + 4:])
            cmfa = parse_qs(query)
            self.assertEqual(verge_url, cmfa['url'][0])
            self.assertIn(verge_url, (self.base, self.base + '-us'))
            self.assertTrue(cmfa['name'][0].startswith('gowest-DC-'))
            self.assertLess(query.find('name='), query.find('url='))
        for section in ('Mac / Windows', 'Android', '小火箭'):
            self.assertIn(section, guide)

    def test_hangup_redirects_output_and_children_get_own_session(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(vpn, 'STATE', Path(tmp)):
            saved = [os.dup(fd) for fd in (0, 1, 2)]
            try:
                vpn.on_hangup(signal.SIGHUP, None)
                os.write(1, b'after hangup\n')
            finally:
                for fd, copy in zip((0, 1, 2), saved):
                    os.dup2(copy, fd)
                    os.close(copy)
            log = (Path(tmp) / 'session-output.log')
            self.assertIn('after hangup', log.read_text())
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        with patch('subprocess.Popen') as run:
            process = run.return_value.__enter__.return_value
            process.returncode = 0
            process.communicate.return_value = ('', '')
            vpn.run(['true'])
            self.assertTrue(run.call_args.kwargs['start_new_session'])

    def test_client_scripts_pin_same_release(self):
        mac = (ROOT / 'clients/macos/gowest-client.sh').read_text()
        win_bytes = (ROOT / 'clients/windows/gowest-client.ps1').read_bytes()
        self.assertTrue(win_bytes.startswith(b'\xef\xbb\xbf'), 'PowerShell 5.1 needs UTF-8 BOM')
        win = win_bytes.decode('utf-8-sig')
        version = re.search(r"CV_VERSION='([0-9.]+)'", mac).group(1)
        self.assertIn(f"$CvVersion = '{version}'", win)
        self.assertIn(version, render.DESKTOP_CLIENT)
        for text in (mac, win):
            self.assertEqual(len(set(re.findall(r'\b[0-9a-f]{64}\b', text))), 2)
            self.assertNotRegex(text, r'/sub/[0-9a-f]{48}')
        self.assertTrue((ROOT / 'clients/windows/gowest-client.cmd').read_bytes().isascii())


if __name__ == '__main__':
    unittest.main()
