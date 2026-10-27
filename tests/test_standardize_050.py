import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import render
import standardize


class StandardizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        raw = json.loads((Path(render.__file__).parent / 'settings.example.json').read_text())
        out = render.render(raw, Path(cls.tmp.name) / 'generated')
        cls.keys = json.loads((out / 'secrets.json').read_text())
        cls.source = render.client_config(render.settings(raw), cls.keys)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_transport_byte_values_preserved_except_name(self):
        before = copy.deepcopy(self.source)
        after = standardize.standardize(before, 'DC')
        self.assertEqual(before, self.source)
        for old, new in zip(before['proxies'], after['proxies']):
            self.assertEqual({k:v for k,v in old.items() if k != 'name'},
                             {k:v for k,v in new.items() if k != 'name'})

    def test_server_independent_groups_and_rules(self):
        la = standardize.standardize(self.source, 'LA')
        dc = standardize.standardize(self.source, 'DC')
        self.assertEqual(la['rules'], dc['rules'])
        self.assertEqual(la['dns'], dc['dns'])
        self.assertEqual([g['name'] for g in la['proxy-groups']],
                         [g['name'] for g in dc['proxy-groups']])
        for cfg in [la, dc]:
            groups = {g['name']:g for g in cfg['proxy-groups']}
            self.assertEqual(groups['出口选择']['default-selected'], '自动回退')
            self.assertEqual(groups['TikTok']['proxies'][0], '自动回退')
            self.assertEqual(groups['Claude']['proxies'][0], 'AI')

    def test_variants_keep_same_nodes_and_groups(self):
        normal = standardize.standardize(self.source, 'LA')
        strict = standardize.standardize(self.source, 'LA', True)
        self.assertEqual(normal['proxies'], strict['proxies'])
        self.assertEqual([g['name'] for g in normal['proxy-groups']],
                         [g['name'] for g in strict['proxy-groups']])
        self.assertIn('GEOSITE,cn,DIRECT', normal['rules'])
        self.assertNotIn('GEOSITE,cn,DIRECT', strict['rules'])
        self.assertNotIn('DIRECT', strict['proxy-groups'][0]['proxies'])
        self.assertEqual(strict['rules'][-1], 'MATCH,出口选择')

    def test_legacy_tailnet_preserved(self):
        source = copy.deepcopy(self.source)
        source['dns']['nameserver-policy']['+.ts.net'] = '100.100.100.100'
        out = standardize.standardize(source, 'LA')
        self.assertEqual(out['dns']['nameserver-policy']['+.ts.net'], '100.100.100.100')

    def test_reject_cross_server_and_chain(self):
        source = copy.deepcopy(self.source)
        source['proxies'][0]['server'] = '192.0.2.99'
        with self.assertRaisesRegex(ValueError, 'cross-server'):
            standardize.standardize(source, 'LA')
        source = copy.deepcopy(self.source)
        source['proxies'][0]['dialer-proxy'] = 'Other'
        with self.assertRaisesRegex(ValueError, 'Chained'):
            standardize.standardize(source, 'LA')

    def test_migration_is_idempotent(self):
        first = standardize.standardize(self.source, 'DC', True)
        self.assertEqual(first, standardize.standardize(first, 'DC', True))


if __name__ == '__main__':
    unittest.main()
