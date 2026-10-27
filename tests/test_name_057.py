import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import build_native
import kit_runtime
import launcher
import render
import vpn


class Name057Tests(unittest.TestCase):
    def test_display_name_cli_and_profiles_match(self):
        self.assertEqual(render.PROJECT_NAME, 'gowest')
        self.assertEqual(render.profile_names('DC'),
                         {'normal': 'gowest-DC-日常', 'strict': 'gowest-DC-全公网'})
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as result:
                launcher.main(['--version'])
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), 'gowest ' + render.KIT_VERSION)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit):
                launcher.main(['--help'])
        self.assertIn('usage: gowest ', output.getvalue())

    def test_current_runtime_files_have_no_old_brand(self):
        for name in build_native.MODULES + ('build_native',):
            content = (ROOT / (name + '.py')).read_text()
            for old in ('GoToWest', 'go_to_west', 'gotowest', 'goto_west'):
                self.assertNotIn(old, content, name)
        self.assertIn('gowest', build_native.ALLOWED_RELEASE_FILES)

    def test_renamed_client_wrappers_reference_existing_files(self):
        mac = ROOT / 'clients/macos/gowest-client.command'
        win = ROOT / 'clients/windows/gowest-client.cmd'
        self.assertIn('gowest-client.sh', mac.read_text())
        self.assertIn('gowest-client.ps1', win.read_text())
        self.assertTrue((mac.parent / 'gowest-client.sh').is_file())
        self.assertTrue((win.parent / 'gowest-client.ps1').is_file())
        self.assertTrue(os.access(mac, os.X_OK))
        self.assertTrue(win.read_bytes().isascii())
        ps1 = (win.parent / 'gowest-client.ps1').read_bytes()
        self.assertTrue(ps1.startswith(b'\xef\xbb\xbf'))
        self.assertNotIn(b'\n', ps1.replace(b'\r\n', b''))
        self.assertIn("$Kit = 'gowest 0.5.7'", ps1.decode('utf-8-sig'))

    def test_generated_guides_reference_new_names(self):
        guide = render.desktop_guide_text('http://8.8.4.4/sub/test-token', 'DC')
        for name in ('clients/macos/gowest-client.sh', 'clients/windows/gowest-client.cmd'):
            self.assertIn(name, guide)
            self.assertTrue((ROOT / name).is_file())
        self.assertIn('name=gowest-DC-', guide)

    def test_legacy_server_paths_preserved(self):
        self.assertEqual(vpn.PROGRAM, Path('/opt/la-vpn'))
        self.assertEqual(vpn.CONFIG, Path('/etc/la-vpn'))
        self.assertEqual(vpn.STATE, Path('/var/lib/la-vpn-deploy'))
        with patch.object(kit_runtime, 'compiled', return_value=True):
            self.assertEqual(vpn.installed_command('status'), ['/opt/la-vpn/gowest', 'status'])
        self.assertIn('la-vpn.service', vpn.SERVICES)

    def test_source_entry_works_outside_project_directory(self):
        result = subprocess.run(['sh', str(ROOT / 'deploy.sh'), '--version'], cwd='/tmp',
                                text=True, capture_output=True, check=True, timeout=30)
        self.assertEqual(result.stdout.strip(), 'gowest ' + render.KIT_VERSION)


if __name__ == '__main__':
    unittest.main()
