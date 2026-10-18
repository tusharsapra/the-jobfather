"""Hermetic test workspaces built from the fictional demo persona (examples/demo-workspace)."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / 'plugins' / 'the-jobfather' / 'skills' / 'the-jobfather'
SCRIPTS = SKILL / 'scripts'
DEMO = REPO / 'examples' / 'demo-workspace'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


_TOOL_DIRS = None


def _tool_dirs():
    global _TOOL_DIRS
    if _TOOL_DIRS is None:
        import pdf_tools
        _TOOL_DIRS = sorted({str(Path(p).parent) for p in pdf_tools.resolve().values() if p})
    return _TOOL_DIRS


class DemoWorkspace:
    """Copy the demo into a temp folder and point every lookup at it.

    HOME, XDG_CONFIG_HOME and APPDATA also move to the temp folder, and
    JOBFATHER_TEST stops any fallback to a real pointer file or ~/Jobfather.
    """

    def __init__(self, init=True, evaluation=True, source=DEMO, host='claude-code'):
        self.temp = tempfile.TemporaryDirectory(prefix='jobfather-test-')
        base = Path(self.temp.name).resolve()
        self.base = base
        self.root = base / 'ws'
        shutil.copytree(str(source), str(self.root), ignore=shutil.ignore_patterns('README.md'))
        home = base / 'home'
        home.mkdir()
        tool_dirs = _tool_dirs()  # resolve real TeX/Poppler before HOME moves
        self.env = mock.patch.dict(os.environ, {
            'JOBFATHER_HOME': str(self.root), 'JOBFATHER_TEST': '1', 'HOME': str(home),
            'XDG_CONFIG_HOME': str(home / '.config'), 'APPDATA': str(home / 'AppData'),
            'USERPROFILE': str(home)})
        self.env.start()
        if tool_dirs:
            import local
            local.machine_file().parent.mkdir(parents=True, exist_ok=True)
            local.atomic_json(local.machine_file(), {'tool_bin_paths': tool_dirs})
        cfg = json.loads((self.root / 'config.json').read_bytes().decode('utf-8'))
        cfg['evaluation_mode'] = evaluation
        (self.root / 'config.json').write_bytes((json.dumps(cfg, indent=2) + '\n').encode('utf-8'))
        self.init_event = self.initialize(host) if init else None

    def initialize(self, host='claude-code'):
        import layout
        import memory
        import onboard
        from local import atomic_json
        atomic_json(self.root / layout.ONBOARDING, {
            'schema_version': 1, 'phase': 'collecting',
            'steps': {s: {'state': 'done', 'updated_at': None, 'note': None} for s in onboard.STEPS}})
        onboard.mark_ready(self.root, host, 'These fictional files are correct.')
        return memory.initialize(self.root, host)

    def close(self):
        self.env.stop()
        self.temp.cleanup()


def proposal(ws, suffix='first', host='claude-code', paths=('profile/VOICE.md',), change_type='preference'):
    """A valid global proposal appending a fictional lesson to each path."""
    import memory
    from local import digest
    return {'transaction_id': 'TX-fixture-' + suffix, 'intent': 'explicit-update',
            'user_request': 'Save this fictional fixture preference as my default.',
            'platform': host, 'scope': 'global', 'change_type': change_type,
            'expected_revision': memory.current(ws)['revision'],
            'summary': {'before': 'Supplied default.', 'after': 'Fictional isolated test preference: ' + suffix},
            'reason': 'Explicit fixture feedback; never a real user preference.',
            'review': {'user_authorized': True, 'facts_checked': True, 'scope_checked': True},
            'changes': [{'path': p, 'before_sha256': digest(Path(ws) / p),
                         'append': '## Fixture ' + suffix + '\n\nShort direct opening.'} for p in paths]}
