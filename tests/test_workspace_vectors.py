"""Shared workspace-resolution vectors: scripts/local.py must agree with jobfather_mcp/workspace.py."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from support import REPO

import local
from local import WorkspaceError

VECTORS = REPO / 'tests' / 'vectors' / 'workspace_resolution.json'
ISOLATED = ('JOBFATHER_HOME', 'JOBFATHER_TEST', 'HOME', 'USERPROFILE', 'XDG_CONFIG_HOME', 'APPDATA')


def run_case(case, base, resolve):
    """Apply one vector case under `base` and return {'source', 'path'} or {'error'}."""
    home = base / 'home'
    fill = lambda text: text.replace('{HOME}', str(home)).replace('{BASE}', str(base))
    data = json.loads(VECTORS.read_bytes().decode('utf-8'))
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
        explicit = fill(case['explicit']) if case.get('explicit') else None
        try:
            source, path = resolve(explicit)
        except WorkspaceError:
            return {'error': 'WorkspaceError'}
    return {'source': source, 'path': str(path)}


def expected(case, base):
    want = dict(case['expect'])
    if 'path' in want:
        want['path'] = str(Path(want['path'].replace('{HOME}', str(base / 'home')).replace('{BASE}', str(base))))
    return want


def applies(case):
    return case.get('os', 'any') in ('any', 'nt' if os.name == 'nt' else 'posix')


def skill_resolve(explicit):
    source, _ = next(iter(local.workspace_candidates(explicit)))
    return source, local.workspace(explicit)


class WorkspaceVectorTests(unittest.TestCase):
    def test_vectors_file_is_well_formed(self):
        data = json.loads(VECTORS.read_bytes().decode('utf-8'))
        self.assertEqual(data['schema'], 'the-jobfather/workspace-vectors@1')
        names = [c['name'] for c in data['cases']]
        self.assertEqual(len(names), len(set(names)))
        for case in data['cases']:
            self.assertIn(case.get('os', 'any'), ('any', 'posix', 'nt'))
            self.assertTrue({'source', 'path'} <= set(case['expect']) or set(case['expect']) == {'error'}, case['name'])

    def test_skill_resolution_matches_vectors(self):
        data = json.loads(VECTORS.read_bytes().decode('utf-8'))
        ran = 0
        for case in data['cases']:
            if not applies(case):
                continue
            with self.subTest(case=case['name']), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp).resolve()
                self.assertEqual(run_case(case, base, skill_resolve), expected(case, base))
                ran += 1
        self.assertGreaterEqual(ran, 10)


if __name__ == '__main__':
    unittest.main()
