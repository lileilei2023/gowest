import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import launcher
import render


class BandwidthTests(unittest.TestCase):
    def test_bit_rate_units_and_decimal_conversion(self):
        for text, expected in [('30', 30), ('30Mbps', 30), (' 30 Mbps ', 30),
                               ('30mbps', 30), ('1Gbps', 1000), ('1 gbps', 1000),
                               ('1.5Gbps', 1500), ('0.001Gbps', 1), ('30.0Mbps', 30),
                               ('100Gbps', 100000), ('00030', 30)]:
            with self.subTest(text=text):
                self.assertEqual(launcher.whole_mbps(text, '带宽'), expected)

    def test_invalid_or_byte_units_rejected_without_rounding(self):
        for text in ('', '0', '-30', '30.5Mbps', '0.0001Gbps', '100.001Gbps',
                     '1GBps', '30MBps', '30MB/s', '1GB/s', 'NaN', 'inf', '1e3',
                     '1Gbps trailing', '30 Mbps; id', '9' * 65, None, True,
                     '1.000000000000000000000000000000000001Mbps'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                launcher.whole_mbps(text, '带宽')

    def test_one_rate_sets_both_and_advanced_remains_available(self):
        self.assertEqual(launcher.bandwidth_values('1Gbps'), (1000, 1000))
        self.assertEqual(launcher.bandwidth_values(up='10', down='30Mbps'), (10, 30))

    def test_missing_partial_or_conflicting_inputs_rejected(self):
        for kwargs in ({}, {'up': '30'}, {'down': '30'},
                       {'bandwidth': '30', 'up': '10'},
                       {'bandwidth': '30', 'down': '10'},
                       {'bandwidth': '30', 'up': '10', 'down': '30'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                launcher.bandwidth_values(**kwargs)

    def test_wizard_usual_case_is_one_input(self):
        out = io.StringIO()
        with patch('builtins.input', return_value='1Gbps') as ask, redirect_stdout(out):
            self.assertEqual(launcher.prompt_bandwidth(), (1000, 1000))
        self.assertEqual(ask.call_count, 1)
        self.assertIn('不代表线路一定对称', out.getvalue())
        self.assertIn('上行 1000 Mbps，下行 1000 Mbps', out.getvalue())

    def test_wizard_advanced_inputs_are_client_directions(self):
        out = io.StringIO()
        with patch('builtins.input', side_effect=['advanced', '10Mbps', '1Gbps']) as ask, redirect_stdout(out):
            self.assertEqual(launcher.prompt_bandwidth(), (10, 1000))
        self.assertEqual(ask.call_count, 3)
        self.assertIn('上行 = 客户端上传', out.getvalue())

    def test_cli_gigabit_saves_integers_and_renders_mihomo_units(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            path = Path(directory) / 'settings.json'
            launcher.main(['init', '--server-ip', '8.8.4.4', '--bandwidth', '1Gbps',
                           '--http', '--accept-http-risk', '--output', str(path)])
            cfg = json.loads(path.read_text())
            self.assertEqual((cfg['hy2_up_mbps'], cfg['hy2_down_mbps']), (1000, 1000))
            self.assertIs(type(cfg['hy2_up_mbps']), int)
            out = render.render(cfg, Path(directory) / 'generated')
            for name in ('normal.yaml', 'strict.yaml'):
                content = (out / name).read_text()
                self.assertIn('up: 1000 Mbps', content)
                self.assertIn('down: 1000 Mbps', content)
                self.assertNotIn('1000.0 Mbps', content)

    def test_cli_legacy_asymmetric_flags_preserved(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            path = Path(directory) / 'settings.json'
            launcher.main(['init', '--server-ip', '8.8.4.4', '--up', '10', '--down', '30',
                           '--http', '--accept-http-risk', '--output', str(path)])
            cfg = json.loads(path.read_text())
            self.assertEqual((cfg['hy2_up_mbps'], cfg['hy2_down_mbps']), (10, 30))

    def test_bad_cli_bandwidth_stops_before_ip_detection_or_writes(self):
        for flags in ([], ['--bandwidth', '30MBps'], ['--up', '10'],
                      ['--bandwidth', '30', '--up', '10', '--down', '20']):
            with self.subTest(flags=flags), redirect_stderr(io.StringIO()), \
                    patch.object(launcher.autoconfig, 'detect_public_ipv4') as detect, \
                    patch.object(launcher, 'save_settings') as save:
                with self.assertRaises(SystemExit) as exc:
                    launcher.main(['init', '--http', '--accept-http-risk'] + flags)
                self.assertEqual(exc.exception.code, 2)
                detect.assert_not_called()
                save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
