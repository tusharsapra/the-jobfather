"""Helpers for the jobfather-mcp tests. Fictional data only; nothing touches real user folders."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]
SRC = PACKAGE / 'src'
REPO = PACKAGE.parents[1]
DEMO = REPO / 'examples' / 'demo-workspace'
SKILL_SCRIPTS = REPO / 'plugins' / 'the-jobfather' / 'skills' / 'the-jobfather' / 'scripts'
VECTORS = REPO / 'tests' / 'vectors' / 'workspace_resolution.json'
FAKE_CHILD = Path(__file__).resolve().parent / 'fake_child_server.py'
ISOLATED = ('JOBFATHER_HOME', 'JOBFATHER_TEST', 'HOME', 'USERPROFILE', 'XDG_CONFIG_HOME', 'APPDATA')
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def in_repo():
    """True inside the repository checkout (shared vectors, demo workspace and skill scripts exist)."""
    return VECTORS.is_file() and DEMO.is_dir() and SKILL_SCRIPTS.is_dir()


def skill_module(name):
    """Import a skill script (stdlib, Python 3.9+) from the checkout."""
    if str(SKILL_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SKILL_SCRIPTS))
    return __import__(name)


class TempWorkspace:
    """A temp workspace (a copy of the fictional demo, or a minimal one) with HOME and config moved too."""

    def __init__(self, demo=True, policy=None, platforms=None, browser=None):
        self.temp = tempfile.TemporaryDirectory(prefix='jobfather-mcp-test-')
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'ws'
        if demo and DEMO.is_dir():
            shutil.copytree(str(DEMO), str(self.root), ignore=shutil.ignore_patterns('README.md'))
        else:
            (self.root / 'config').mkdir(parents=True)
            self.write('config.json', {'schema_version': 1})
        (self.root / '.state').mkdir(exist_ok=True)
        if policy:
            self.write('config/policy.json', {'schema_version': 1, 'mode': policy})
        if platforms is not None:
            self.write('config/platforms.json', platforms)
        if browser is not None:
            self.write('config/browser.json', browser)
        home = self.base / 'home'
        home.mkdir()
        self.env = mock.patch.dict(os.environ, {
            'JOBFATHER_HOME': str(self.root), 'JOBFATHER_TEST': '1', 'HOME': str(home),
            'USERPROFILE': str(home), 'XDG_CONFIG_HOME': str(home / '.config'), 'APPDATA': str(home / 'AppData')})
        self.env.start()

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((json.dumps(value, indent=2) + '\n').encode('utf-8'))

    def close(self):
        self.env.stop()
        self.temp.cleanup()
