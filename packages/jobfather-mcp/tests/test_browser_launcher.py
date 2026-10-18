"""jobfather-browser: argv per mode, blocked origins, --print-argv, pass-through args and exec."""
import contextlib
import io
import json
import os
import stat
import unittest
from unittest import mock

from pkgsupport import TempWorkspace

import jobfather_mcp
from jobfather_mcp import browser_launcher as launcher

ACK = {'at': '2026-10-01T09:00:00+00:00', 'notice_version': 1, 'statement': 'Yes, I understand the risk.'}
LINKEDIN = 'https://www.linkedin.com;https://linkedin.com'
NAUKRI = 'https://www.naukri.com;https://naukri.com;https://login.naukri.com'


def run_main(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = launcher.main(list(args))
    return code, out.getvalue(), err.getvalue()


class LauncherTests(unittest.TestCase):
    def tearDown(self):
        if getattr(self, 'ws', None):
            self.ws.close()

    def printed(self, *args):
        code, out, _ = run_main('--print-argv', *args)
        self.assertEqual(code, 0)
        return json.loads(out)

    def test_profile_mode_blocks_linkedin_and_naukri_by_default(self):
        self.ws = TempWorkspace()
        root = self.ws.root
        self.assertEqual(self.printed(), [
            'npx', '-y', '@playwright/mcp@0.0.83', '--browser', 'chrome',
            '--user-data-dir', str(root / '.state' / 'browser' / 'main'),
            '--output-dir', str(root / '.state' / 'browser' / 'output'),
            '--snapshot-mode', 'none', '--timeout-navigation', '30000',
            '--blocked-origins', LINKEDIN + ';' + NAUKRI])

    def test_linkedin_opens_only_when_opted_in_for_the_browser(self):
        self.ws = TempWorkspace(platforms={'linkedin': {'enabled': True, 'mode': 'browser', 'acknowledged': ACK},
                                           'naukri': {'enabled': True, 'acknowledged': ACK}})
        argv = self.printed()
        self.assertEqual(argv[argv.index('--blocked-origins') + 1], NAUKRI)  # Naukri stays blocked here
        self.ws.write('config/platforms.json', {'linkedin': {'enabled': True, 'mode': 'mcp', 'acknowledged': ACK}})
        argv = self.printed()
        self.assertEqual(argv[argv.index('--blocked-origins') + 1], LINKEDIN + ';' + NAUKRI)
        self.ws.write('config/platforms.json', {'linkedin': {'enabled': True, 'mode': 'browser', 'acknowledged': None}})
        argv = self.printed()
        self.assertIn('https://www.linkedin.com', argv[argv.index('--blocked-origins') + 1])

    def test_extension_mode_and_passthrough(self):
        self.ws = TempWorkspace(browser={'mode': 'extension'})
        argv = self.printed('--', '--headless', '--viewport-size', '1280x720')
        self.assertIn('--extension', argv)
        self.assertNotIn('--user-data-dir', argv)
        self.assertEqual(argv[-3:], ['--headless', '--viewport-size', '1280x720'])
        self.ws.write('config/browser.json', {'mode': 'extension', 'channel': 'msedge'})
        self.assertEqual(self.printed()[3:5], ['--browser', 'msedge'])

    def test_bad_settings_fall_back_safely(self):
        self.ws = TempWorkspace(browser={'mode': 'teleport', 'channel': 'netscape'})
        (self.ws.root / 'config' / 'platforms.json').write_text('{broken', encoding='utf-8')
        argv = self.printed()
        self.assertIn('--user-data-dir', argv)
        self.assertEqual(argv[4], 'chrome')
        self.assertEqual(argv[argv.index('--blocked-origins') + 1], LINKEDIN + ';' + NAUKRI)

    def test_no_workspace_uses_an_isolated_profile(self):
        self.ws = TempWorkspace(demo=False)
        (self.ws.root / 'config.json').unlink()
        argv = self.printed()
        self.assertIn('--isolated', argv)
        self.assertNotIn('--user-data-dir', argv)
        self.assertNotIn('--output-dir', argv)
        self.assertFalse((self.ws.root / '.state' / 'browser').exists())

    def test_exec_creates_private_folders_and_logs_only_to_stderr(self):
        self.ws = TempWorkspace()
        with mock.patch('shutil.which', return_value='/opt/fake/bin/npx'), \
                mock.patch('os.execv', side_effect=SystemExit(0)) as execv, \
                mock.patch.object(launcher, 'IS_WINDOWS', False):
            with self.assertRaises(SystemExit):
                run_main('--', '--headless')
        path, argv = execv.call_args[0]
        self.assertEqual(path, '/opt/fake/bin/npx')
        self.assertEqual(argv[0], '/opt/fake/bin/npx')
        self.assertEqual(argv[-1], '--headless')
        main = self.ws.root / '.state' / 'browser' / 'main'
        self.assertTrue(main.is_dir())
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(main.stat().st_mode), 0o700)

    def test_windows_propagates_the_exit_code(self):
        self.ws = TempWorkspace()
        with mock.patch('shutil.which', return_value='C:/fake/npx.cmd'), \
                mock.patch.object(launcher, 'IS_WINDOWS', True), \
                mock.patch('subprocess.call', return_value=3) as call:
            code, out, _ = run_main()
        self.assertEqual(code, 3)
        self.assertEqual(out, '')
        self.assertEqual(call.call_args[0][0][0], 'C:/fake/npx.cmd')

    def test_missing_npx_is_reported(self):
        self.ws = TempWorkspace()
        with mock.patch('shutil.which', return_value=None):
            code, out, err = run_main()
        self.assertEqual(code, 127)
        self.assertEqual(out, '')
        self.assertIn('Node.js', err)

    def test_prefetch_and_version(self):
        done = mock.Mock(returncode=0, stdout='Version 0.0.83\n')
        with mock.patch('shutil.which', return_value='/opt/fake/bin/npx'), \
                mock.patch('subprocess.run', return_value=done) as run:
            code, out, err = run_main('--prefetch')
        self.assertEqual(code, 0)
        self.assertEqual(out, '')
        self.assertIn('Version 0.0.83', err)
        self.assertEqual(run.call_args[0][0], ['/opt/fake/bin/npx', '-y', '@playwright/mcp@0.0.83', '--version'])
        code, out, _ = run_main('--version')
        self.assertEqual(code, 0)
        self.assertIn('jobfather-mcp %s ' % jobfather_mcp.__version__, out)
        with self.assertRaises(SystemExit):
            run_main('--bogus')


if __name__ == '__main__':
    unittest.main()
