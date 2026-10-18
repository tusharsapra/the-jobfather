"""doctor.py with every lookup patched: readiness, install hints, PDF tools, workspace health, connections."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from support import DemoWorkspace

import connect
import doctor
import layout
import local

TOOLS = {name: '/opt/fake/tex/' + name for name in ('pdflatex', 'kpsewhich')}
TOOLS.update({name: '/opt/fake/poppler/' + name for name in ('pdfinfo', 'pdftotext', 'pdftoppm', 'pdffonts')})


def fake_run(missing_tex=(), node='v22.11.0'):
    def run(argv, timeout=20):
        name = os.path.basename(str(argv[0]))
        if name == 'node':
            return 0, node
        if name == 'kpsewhich':
            files = [a for a in argv[1:] if a not in missing_tex]
            return (0 if files else 1), '\n'.join('/opt/fake/texmf/' + f for f in files)
        if name == 'npx':
            return 0, 'Version 0.0.83'
        return 0, name + ' 1.0'
    return run


class DoctorCase(unittest.TestCase):
    def setUp(self):
        self.demo = DemoWorkspace(init=False)
        self.ws = self.demo.root
        os.chmod(self.ws, 0o700)
        self.patches = [
            mock.patch.object(doctor, 'which', lambda name: '/opt/fake/bin/' + name),
            mock.patch.object(doctor, 'run', side_effect=fake_run()),
            mock.patch.object(doctor, 'os_kind', return_value='macos'),
            mock.patch.object(doctor, 'exists', lambda path: 'Google Chrome' in path),
            mock.patch.object(doctor.pdf_tools, 'resolve', return_value=dict(TOOLS)),
            mock.patch.object(connect, 'machine_id', return_value='fictional-machine-id'),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.demo.close()

    def by_id(self, result):
        return {c['id']: c for c in result['checks']}


class DoctorTests(DoctorCase):
    def test_everything_present_is_ready(self):
        result = doctor.report()
        checks = self.by_id(result)
        self.assertTrue(result['ready'])
        for name in ('python', 'uv', 'git', 'node', 'browser', 'tex', 'poppler', 'locks'):
            self.assertEqual(checks[name]['status'], 'ok', name)
        self.assertIn('14/14 template files', checks['tex']['detail'])
        self.assertEqual(checks['workspace']['status'], 'info')  # onboarding not finished: no history yet
        self.assertEqual(checks['workspace']['history_events'], 0)
        self.assertEqual(checks['connections']['status'], 'info')

    def test_missing_tools_get_consent_first_hints_per_os(self):
        with mock.patch.object(doctor, 'which', return_value=None):
            for kind, needle in (('macos', 'brew install uv'), ('linux', 'astral.sh/uv/install.sh'),
                                 ('windows', 'winget install --id=astral-sh.uv')):
                with mock.patch.object(doctor, 'os_kind', return_value=kind):
                    checks = self.by_id(doctor.report())
                self.assertEqual(checks['uv']['status'], 'warn')
                self.assertIn(needle, checks['uv']['fix'])
                self.assertTrue(checks['uv']['fix'].startswith('Ask the user before installing'))
                self.assertEqual(checks['node']['status'], 'warn')
                self.assertEqual(checks['git']['status'], 'warn')

    def test_old_node_and_browser_discovery(self):
        with mock.patch.object(doctor, 'run', side_effect=fake_run(node='v16.20.0')):
            self.assertEqual(self.by_id(doctor.report())['node']['status'], 'warn')
        with mock.patch.object(doctor, 'exists', lambda path: 'Edge' in path):
            browser = self.by_id(doctor.report())['browser']
            self.assertEqual(browser['status'], 'warn')
            self.assertIn('msedge', browser['detail'])
            (self.ws / 'config' / 'browser.json').write_text('{"mode": "profile", "channel": "msedge"}', encoding='utf-8')
            self.assertEqual(self.by_id(doctor.report())['browser']['status'], 'ok')
        with mock.patch.object(doctor, 'exists', return_value=False):
            self.assertIn('asks for Edge', self.by_id(doctor.report())['browser']['detail'])
            (self.ws / 'config' / 'browser.json').write_text('{"mode": "profile"}', encoding='utf-8')
            self.assertIn('Neither', self.by_id(doctor.report())['browser']['detail'])
        with mock.patch.dict(os.environ, {'PROGRAMFILES': 'C:/Program Files', 'LOCALAPPDATA': 'C:/Users/you/AppData/Local'}):
            chrome, edge = doctor.browser_candidates('windows')
        self.assertTrue(any(p.endswith('chrome.exe') for p in chrome))
        self.assertTrue(any(p.endswith('msedge.exe') for p in edge))

    def test_pdf_tools_matter_only_with_require_pdf(self):
        with mock.patch.object(doctor, 'run', side_effect=fake_run(missing_tex=('carlito.sty', 'english.ldf'))):
            relaxed = doctor.report()
            strict = doctor.report(require_pdf=True)
        self.assertTrue(relaxed['ready'])
        self.assertEqual(self.by_id(relaxed)['tex']['status'], 'warn')
        self.assertFalse(strict['ready'])
        self.assertEqual(self.by_id(strict)['tex']['missing_files'], ['carlito.sty', 'english.ldf'])
        no_poppler = dict(TOOLS, pdfinfo=None, pdffonts=None)
        with mock.patch.object(doctor.pdf_tools, 'resolve', return_value=no_poppler):
            result = doctor.report(require_pdf=True)
        self.assertFalse(result['ready'])
        self.assertIn('pdfinfo, pdffonts', self.by_id(result)['poppler']['detail'])
        no_tex = dict(TOOLS, pdflatex=None, kpsewhich=None)
        with mock.patch.object(doctor.pdf_tools, 'resolve', return_value=no_tex), \
                mock.patch.object(doctor, 'os_kind', return_value='windows'):
            self.assertIn('MiKTeX', self.by_id(doctor.report())['tex']['fix'])

    def test_workspace_health(self):
        self.demo.initialize()
        checks = self.by_id(doctor.report())
        self.assertEqual(checks['workspace']['status'], 'ok')
        self.assertGreaterEqual(checks['workspace']['history_events'], 1)
        self.assertEqual(checks['workspace']['phase'], 'complete')
        self.assertNotIn('cloud', checks)
        self.assertNotIn('git', [c['id'] for c in doctor.report()['checks'] if c['title'] == 'Workspace location'])
        (self.demo.base / '.git').mkdir()
        self.assertIn('git work tree', ' '.join(c['detail'] for c in doctor.report()['checks']))
        for path, cloud in (('/Users/you/Library/Mobile Documents/com~apple~CloudDocs/Jobfather', 'iCloud Drive'),
                            ('/Users/you/Library/CloudStorage/Dropbox/Jobfather', 'a cloud-synced folder'),
                            ('C:\\Users\\you\\OneDrive\\Jobfather', 'OneDrive'), ('/home/user/Jobfather', None)):
            self.assertEqual(doctor.cloud_folder(path), cloud, path)

    def test_lock_probe_and_broken_workspace(self):
        with mock.patch.object(doctor, 'shared_lock',
                               side_effect=local.WorkspaceError('This folder does not support file locks')):
            result = doctor.report()
        self.assertFalse(result['ready'])
        self.assertEqual(self.by_id(result)['locks']['status'], 'fail')
        (self.ws / layout.CONFIG).write_text('{broken', encoding='utf-8')
        self.assertFalse(doctor.report()['ready'])
        os.remove(self.ws / layout.CONFIG)
        result = doctor.report()
        self.assertTrue(result['ready'])
        self.assertEqual(self.by_id(result)['workspace']['status'], 'warn')

    @mock.patch.object(connect, 'NAUKRI_SHIPPED', True)
    def test_connection_flags(self):
        connect.enable(self.ws, 'naukri', 'Yes, turn Naukri on.')
        flags = self.by_id(doctor.report())['connections']['flags']
        self.assertEqual(flags, {'naukri': ['not_connected']})
        connect.record(self.ws, 'codex', 'naukri', 'installed', 'yes')
        connect.record(self.ws, 'codex', 'naukri', 'authenticated', 'yes')
        connect.record(self.ws, 'codex', 'linkedin', 'installed', 'yes')
        connect.record(self.ws, 'codex', 'linkedin', 'authenticated', 'no')
        data = connect.load_connections(self.ws)
        old = (connect._utc() - connect.timedelta(days=9)).isoformat()
        data['hosts']['codex']['services']['naukri']['checked_at']['authenticated'] = old
        data['resume'] = {'host': 'codex', 'action': 'Start a new Codex thread.', 'created_at': old}
        connect.save_connections(self.ws, data)
        check = self.by_id(doctor.report())['connections']
        self.assertEqual(check['status'], 'warn')
        self.assertEqual(check['flags']['codex/naukri'], ['stale', 'restart_pending'])
        self.assertEqual(check['flags']['codex/linkedin'], ['reauth_needed', 'restart_pending'])
        self.assertIn('Start a new Codex thread.', check['fix'])
        self.assertIn('do not reset profiles', check['fix'])

    def test_save_tools_writes_machine_settings_only(self):
        before = sorted(p.relative_to(self.ws).as_posix() for p in self.ws.rglob('*') if p.is_file())
        result = doctor.report(save=True)
        saved = json.loads(local.machine_file().read_text(encoding='utf-8'))
        self.assertEqual(result['saved_tools'], str(local.machine_file()))
        self.assertEqual([Path(d).as_posix() for d in saved['tool_bin_paths']], ['/opt/fake/poppler', '/opt/fake/tex'])
        self.assertEqual(saved['tools']['uv'], '/opt/fake/bin/uvx')
        self.assertNotIn(str(self.ws), str(local.machine_file()))
        after = sorted(p.relative_to(self.ws).as_posix() for p in self.ws.rglob('*') if p.is_file())
        self.assertEqual([p for p in after if p not in before], ['.state/locks/doctor-probe.lock',
                                                                  '.state/locks/doctor-probe.lock.json'])

    def test_live_probes_and_cli(self):
        result = doctor.report(live=True)
        self.assertIn('Playwright MCP: Version 0.0.83', self.by_id(result)['node']['detail'])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doctor.main(['--json'])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())['ready'])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(doctor.pdf_tools, 'resolve', return_value=dict(TOOLS, pdflatex=None)):
            code = doctor.main(['--require-pdf'])
        self.assertEqual(code, 1)
        self.assertIn('[FAIL] TeX', out.getvalue())
        self.assertIn('Ready: no', out.getvalue())

    def test_python_version_gate(self):
        with mock.patch.object(doctor.sys, 'version_info', (3, 8, 18)):
            self.assertEqual(doctor.check_python()['status'], 'fail')


if __name__ == '__main__':
    unittest.main()
