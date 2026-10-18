"""Workspace resolution: explicit > JOBFATHER_HOME > pointer > default, and the safety guards."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from support import SKILL  # noqa: F401  (puts scripts on sys.path)

import local
import onboard
from local import WorkspaceError


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.env = {'HOME': str(self.base / 'home'), 'XDG_CONFIG_HOME': str(self.base / 'cfg'),
                    'APPDATA': str(self.base / 'appdata'), 'USERPROFILE': str(self.base / 'home')}

    def tearDown(self):
        self.temp.cleanup()

    def resolve(self, extra, explicit=None):
        env = dict(self.env, **extra)
        with mock.patch.dict(os.environ, env, clear=False):
            for key in ('JOBFATHER_HOME', 'JOBFATHER_TEST'):
                if key not in extra:
                    os.environ.pop(key, None)
            return local.workspace(explicit)

    def test_precedence(self):
        pointer_target = self.base / 'from-pointer'
        with mock.patch.dict(os.environ, self.env):
            local.write_pointer(pointer_target)
        self.assertEqual(self.resolve({}, explicit=str(self.base / 'arg')), self.base / 'arg')
        self.assertEqual(self.resolve({'JOBFATHER_HOME': str(self.base / 'env')}), self.base / 'env')
        self.assertEqual(self.resolve({}), pointer_target)

    def test_unsubstituted_host_variable_is_ignored(self):
        self.assertEqual(self.resolve({'JOBFATHER_HOME': '${user_config.workspace_dir}'}), self.base / 'home' / 'Jobfather')

    def test_tests_cannot_fall_back_to_real_locations(self):
        with self.assertRaises(WorkspaceError):
            self.resolve({'JOBFATHER_TEST': '1'})

    def test_workspace_inside_code_is_refused(self):
        with self.assertRaises(WorkspaceError):
            self.resolve({'JOBFATHER_HOME': str(local.CODE_ROOT / 'inside')})

    def test_init_creates_skeleton_without_overwriting(self):
        target = self.base / 'new-ws'
        with mock.patch.dict(os.environ, dict(self.env, JOBFATHER_TEST='1', JOBFATHER_HOME=str(target))):
            result = onboard.init(str(target))
            self.assertIsNone(result['pointer'])  # tests never write the real pointer
            self.assertTrue((target / 'profile' / 'EXPERIENCE.md').is_file())
            hr = (target / 'profile' / 'HR-DETAILS.json').read_text(encoding='utf-8')
            self.assertIn(result['workspace_id'], hr)
            self.assertNotIn('__WORKSPACE_ID__', hr)
            self.assertEqual(onboard.next_step(target), 'preflight')
            with self.assertRaises(WorkspaceError):
                onboard.init(str(target))


if __name__ == '__main__':
    unittest.main()
