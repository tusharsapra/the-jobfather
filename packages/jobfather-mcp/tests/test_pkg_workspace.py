"""Workspace resolution (shared vectors with the skill), state folders and settings."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from pkgsupport import ISOLATED, VECTORS, TempWorkspace, in_repo

from jobfather_mcp import workspace
from jobfather_mcp.workspace import WorkspaceError


def run_case(case, base, data):
    home = base / 'home'
    fill = lambda text: text.replace('{HOME}', str(home)).replace('{BASE}', str(base))
    env = {k: fill(v) for k, v in data['base_env'].items()}
    for key, value in case.get('env', {}).items():
        env[key] = None if value is None else fill(value)
    with mock.patch.dict(os.environ, {}, clear=False):
        for key in ISOLATED:
            os.environ.pop(key, None)
        os.environ.update({k: v for k, v in env.items() if v is not None})
        for name, content in case.get('files', {}).items():
            path = Path(fill(name))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(fill(content).encode('utf-8'))
        try:
            source, path = workspace.resolve_with_source(fill(case['explicit']) if case.get('explicit') else None)
        except WorkspaceError:
            return {'error': 'WorkspaceError'}
    return {'source': source, 'path': str(path)}


@unittest.skipUnless(in_repo(), 'shared vectors live in the repository checkout')
class VectorTests(unittest.TestCase):
    def test_package_resolution_matches_shared_vectors(self):
        data = json.loads(VECTORS.read_bytes().decode('utf-8'))
        ran = 0
        for case in data['cases']:
            if case.get('os', 'any') not in ('any', 'nt' if os.name == 'nt' else 'posix'):
                continue
            with self.subTest(case=case['name']), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp).resolve()
                want = dict(case['expect'])
                if 'path' in want:
                    want['path'] = str(Path(want['path'].replace('{HOME}', str(base / 'home')).replace('{BASE}', str(base))))
                self.assertEqual(run_case(case, base, data), want)
                ran += 1
        self.assertGreaterEqual(ran, 10)


class StateAndSettingsTests(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace(demo=False)

    def tearDown(self):
        self.ws.close()

    def test_require_needs_config(self):
        self.assertEqual(workspace.require(), self.ws.root)
        (self.ws.root / 'config.json').unlink()
        with self.assertRaises(WorkspaceError):
            workspace.require()

    def test_state_dir_creates_private_folders_and_never_the_workspace(self):
        path = workspace.state_dir(self.ws.root, 'browser', 'main')
        self.assertEqual(path, self.ws.root / '.state' / 'browser' / 'main')
        if os.name != 'nt':
            for folder in (path, path.parent):
                self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        with self.assertRaises(ValueError):
            workspace.state_dir(self.ws.root, '..')
        with self.assertRaises(WorkspaceError):
            workspace.state_dir(self.ws.base / 'missing', 'x')
        self.assertFalse((self.ws.base / 'missing').exists())

    def test_platforms_merge_over_defaults_and_report_bad_json(self):
        self.assertFalse(workspace.read_platforms(self.ws.root)['naukri']['enabled'])
        self.ws.write('config/platforms.json', {'linkedin': {'enabled': True, 'acknowledged': {'statement': 'yes'}}})
        merged = workspace.read_platforms(self.ws.root)
        self.assertEqual(merged['linkedin']['mode'], 'browser')
        self.assertTrue(workspace.platform_enabled(merged, 'linkedin'))
        self.assertFalse(workspace.platform_enabled(merged, 'naukri'))
        self.ws.write('config/platforms.json', {'naukri': {'enabled': True, 'acknowledged': None}})
        self.assertFalse(workspace.platform_enabled(workspace.read_platforms(self.ws.root), 'naukri'))
        (self.ws.root / 'config' / 'platforms.json').write_text('{not json', encoding='utf-8')
        with self.assertRaises(WorkspaceError):
            workspace.read_platforms(self.ws.root)
        self.assertEqual(workspace.read_browser(self.ws.root), {'mode': 'profile'})

    def test_workspace_inside_the_package_is_refused(self):
        with mock.patch.dict(os.environ, {'JOBFATHER_HOME': str(workspace.CODE_ROOT / 'inside')}):
            with self.assertRaises(WorkspaceError):
                workspace.resolve()


if __name__ == '__main__':
    unittest.main()
