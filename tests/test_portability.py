"""Byte-exact files, POSIX paths, timezone fallback and lock metadata."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from support import DemoWorkspace

import layout
import local
import managed
from memory import apply, current, events
from support import proposal


class PortabilityTests(unittest.TestCase):
    def test_crlf_content_round_trips_byte_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'x.md'
            local.atomic_text(path, '# Title\r\nline\r\n')
            self.assertEqual(path.read_bytes(), b'# Title\r\nline\r\n')
            self.assertEqual(local.read_text(path), '# Title\r\nline\r\n')
            self.assertEqual(local.digest(path), local.sha_text('# Title\r\nline\r\n'))

    def test_user_file_with_crlf_can_still_be_updated(self):
        demo = DemoWorkspace()
        try:
            voice = demo.root / layout.VOICE
            voice.write_bytes(local.read_bytes(voice).replace(b'\n', b'\r\n'))
            current(demo.root)  # logs the outside edit
            e = apply(proposal(demo.root, 'crlf'), demo.root)
            self.assertEqual(e['status'], 'committed')
        finally:
            demo.close()

    def test_contained_returns_posix_safe_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = local.contained(tmp, 'jobs\\applications/APP-0123456789ab')
            self.assertEqual(path.relative_to(Path(tmp).resolve()).as_posix(), 'jobs/applications/APP-0123456789ab')

    def test_timezone_falls_back_to_fixed_offset_then_utc(self):
        demo = DemoWorkspace(init=False)
        try:
            zone, name, exact = local.tz(demo.root)
            self.assertEqual((name, exact), ('America/Toronto', True))
            with mock.patch.dict('sys.modules', {'zoneinfo': None}):
                zone, name, exact = local.tz(demo.root)
            self.assertEqual((name, exact), ('-05:00', False))
            self.assertEqual(local.tz(None)[1:], ('UTC', False))
        finally:
            demo.close()

    def test_lock_file_is_never_written_and_metadata_sits_beside_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / 'locks' / 'workspace.lock'
            with local.shared_lock(lock):
                with local.shared_lock(lock):  # re-entrant
                    pass
            self.assertEqual(lock.read_bytes(), b'')
            self.assertIn('acquired_at', local.read_text(Path(str(lock) + '.json')))

    def test_revisions_are_keyed_by_posix_relative_paths(self):
        demo = DemoWorkspace()
        try:
            files = managed.raw_revision(demo.root)['files']
            self.assertIn('profile/VOICE.md', files)
            self.assertTrue(all('\\' not in p for p in files))
            self.assertEqual(files['cv/master-cv.tex'], 'missing')
        finally:
            demo.close()


if __name__ == '__main__':
    unittest.main()
