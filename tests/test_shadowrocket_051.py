import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check
import render
import vpn


class ShadowrocketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='shadowrocket051-')
        raw = json.loads((Path(render.__file__).parent / 'settings.example.json').read_text())
        raw['node_prefix'] = 'DC'
        cls.settings = render.settings(raw)
        cls.out = render.render(cls.settings, Path(cls.temp.name) / 'generated')
        cls.keys = json.loads((cls.out / 'secrets.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_native_profiles_for_each_label(self):
        for label in ('LA', 'DC', 'US2'):
            s = copy.deepcopy(self.settings)
            s['node_prefix'] = label
            for strict in (False, True):
                cfg = render.client_config(s, self.keys, strict)
                text = render.shadowrocket_config(s, self.keys, strict)
                render.validate_shadowrocket_config(text, cfg)
                self.assertIn(label + '-Hy2', text)
                self.assertIn(label + '-VLESS', text)

    def test_reject_app_default_cross_server_and_regex_members(self):
        cfg = render.client_config(self.settings, self.keys)
        text = render.shadowrocket_config(self.settings, self.keys)
        for old, new in [('DC-Hy2', 'LA-Hy2'),
                         ('FINAL,出口选择', 'FINAL,PROXY'),
                         ('policy-select-name=自动回退', 'policy-regex-filter=.*')]:
            with self.subTest(new=new), self.assertRaises(ValueError):
                render.validate_shadowrocket_config(text.replace(old, new), cfg)

    def test_reject_default_node_dns(self):
        cfg = render.client_config(self.settings, self.keys)
        text = render.shadowrocket_config(self.settings, self.keys)
        with self.assertRaises(ValueError):
            render.validate_shadowrocket_config(text.replace('#proxy=DC-Hy2', '#proxy'), cfg)

    def test_private_copy_paste_guide_not_web_published(self):
        path = self.out / 'shadowrocket-import.txt'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        text = path.read_text()
        base = render.subscription_base(self.settings, self.keys)
        for suffix in ('-nodes', '-shadowrocket.conf', '-us-shadowrocket.conf'):
            self.assertIn('\n' + base + suffix + '\n', text)
        self.assertIn('首页选 DC-Hy2', text)
        self.assertNotIn('LA-Hy2', text)
        self.assertNotIn('shadowrocket://', text)
        self.assertNotIn('shadowrocket-import.txt', (self.out / 'nginx.conf').read_text())
        self.assertNotIn('shadowrocket-import.txt', render.SUBSCRIPTION_FILES.values())

    def test_url_scheme_and_port_handling(self):
        s = copy.deepcopy(self.settings)
        for scheme, port, authority in [('https', 8443, ':8443'), ('http', 80, ''), ('http', 8080, ':8080')]:
            s['subscription'].update(scheme=scheme, port=port)
            base = render.subscription_base(s, self.keys)
            self.assertTrue(base.startswith(scheme + '://' + s['subscription']['host'] + authority + '/sub/'))

    def test_cli_shadowrocket_guide(self):
        stdout = io.StringIO()
        with patch.object(vpn, 'STATE', Path(self.temp.name)), \
                patch.object(vpn, 'load_state', return_value={'status': 'installed'}), \
                patch.object(sys, 'argv', ['vpn.py', 'urls', '--client', 'shadowrocket']), \
                contextlib.redirect_stdout(stdout):
            vpn.main()
        self.assertIn('DC 小火箭导入说明', stdout.getvalue())
        self.assertIn('全局路由选“配置”', stdout.getvalue())
        self.assertNotIn('normal: ', stdout.getvalue())

    def test_offline_cross_config_check(self):
        result = check.check(self.out)
        self.assertIn('passed', result['shadowrocket_projection'])
        self.assertFalse(result['network_changes'])


if __name__ == '__main__':
    unittest.main()
