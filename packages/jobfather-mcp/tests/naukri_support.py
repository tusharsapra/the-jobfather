"""Helpers for the Naukri server tests: workspaces, sanitized fixtures and an offline fake session.

Fictional data only. The fake session replaces the browser: it serves the sanitized fixtures in
``fixtures/naukri/`` and scripted pages, counts page loads through the real limiter and lets a test
script the confirm-page decision and the outcome of the Apply click.
"""
import copy
import json
import os
import re
from pathlib import Path
from unittest import mock

import pkgsupport  # noqa: F401  (puts src on sys.path)
from pkgsupport import TempWorkspace

from jobfather_mcp.naukri import site as site_mod
from jobfather_mcp.naukri import server
from jobfather_mcp.naukri.limits import Limiter
from jobfather_mcp.naukri.session import Loaded, Session

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'naukri'
ACK = {'at': '2026-10-19T09:00:00+00:00', 'notice_version': 1, 'statement': 'Yes, turn on Naukri for me.'}
NO_SPACING = {name: {'spacing_s': 0} for name in ('page', 'search', 'recommended', 'saved', 'similar', 'job', 'apply')}


def platforms(enabled=True, acknowledged=True, limits=None):
    return {'schema': 'the-jobfather/platforms@1',
            'naukri': {'enabled': enabled, 'acknowledged': ACK if acknowledged else None,
                       'limits': NO_SPACING if limits is None else limits}}


def workspace(enabled=True, acknowledged=True, policy='submit-with-approval', limits=None):
    """A temp workspace with Naukri opted in (test mode relaxes the pacing floors)."""
    ws = TempWorkspace(demo=False, policy=policy, platforms=platforms(enabled, acknowledged, limits))
    ws.extra_env = mock.patch.dict(os.environ, {'JOBFATHER_TEST_MODE': '1'})
    ws.extra_env.start()
    os.environ.pop('JOBFATHER_NAUKRI_BASE', None)
    site_mod.reset()
    return ws


def close_workspace(ws):
    server.STATE.session = None
    ws.extra_env.stop()
    ws.close()
    site_mod.reset()


def fixture(name):
    folder = FIXTURES / name
    responses = json.loads((folder / 'responses.json').read_text(encoding='utf-8'))
    html_path = folder / 'page.html'
    meta = json.loads((folder / 'meta.json').read_text(encoding='utf-8'))
    return responses, (html_path.read_text(encoding='utf-8') if html_path.is_file() else ''), meta


def fixture_job_id(name='job'):
    responses, _, _ = fixture(name)
    for item in responses:
        match = re.search(r'/jobapi/v\d+/job/(\d+)', item['url'])
        if match:
            return match.group(1)
    raise AssertionError('no job response in fixture %s' % name)


def error_of(result):
    if not result.is_error:
        return None
    return json.loads(result.content[0].text)['error']


def data_of(result):
    assert not result.is_error, result.content[0].text
    return result.structured_content


# ---------------------------------------------------------------------- fake browser
class FakePage:
    def __init__(self):
        self.closed = False
        self.url = 'about:blank'

    def is_closed(self):
        return self.closed

    async def bring_to_front(self):
        return None

    async def close(self):
        self.closed = True


class FakeActions:
    """Scripted stand-in for apply.PageActions."""

    def __init__(self):
        self.state_value = 'ready'
        self.outcomes = []
        self.clicks = 0
        self.filled = []
        self.submits = 0

    async def state(self, page, detail):
        return self.state_value

    async def snapshot(self, page):
        return {'success': 0, 'applied': False, 'dialog': False}

    async def click_apply(self, page):
        self.clicks += 1

    async def outcome(self, page, before, known_qids=frozenset(), wait_s=0):
        return self.outcomes.pop(0) if self.outcomes else {'kind': 'unknown', 'text': None, 'url': None}

    async def fill(self, page, questions, answers):
        self.filled.append(dict(answers))

    async def submit(self, page):
        self.submits += 1


class FakeSession(Session):
    """Session without a browser: pages come from fixtures and scripted HTML."""

    def __init__(self, ws, site, limiter, logged_in=True):
        super().__init__(ws, site, limiter)
        self.logged_in = logged_in
        self.page_actions = FakeActions()
        self.decisions = []
        self.loads = []
        self.pages = {}       # path regex -> callable(url) -> Loaded fields
        self.checkpoint_next = None
        self.started = False

    @property
    def running(self):
        return self.started

    async def ensure(self):
        self.started = True
        if self.page is None or self.page.is_closed():
            self.page = FakePage()
        return self.page

    async def new_page(self):
        self.started = True
        return FakePage()

    async def close(self, from_idle=False):
        was = self.started
        self.started = False
        self.flows.clear()
        return was

    async def visible(self, page, selectors):
        return False

    async def checkpoint_on(self, page, status, login_flow=False):
        return None

    async def confirm(self, data, timeout_s, tick=None):
        self.confirm_data = data
        return self.decisions.pop(0) if self.decisions else 'timeout'

    async def screenshot(self, page, name):
        return '.state/naukri/evidence/' + name

    async def load(self, url, *, actions, expect=None, required=(), ready=None, wait_s=15.0, page=None,
                   login_flow=False):
        if not self.site.allowed(url):
            from jobfather_mcp.errors import JFError
            raise JFError('invalid_argument', 'The server only opens Naukri pages.', False)
        await self.limiter.acquire(['page'] + list(actions))
        self.loads.append(url)
        page = page or await self.ensure()
        if self.checkpoint_next:
            from jobfather_mcp.naukri.checkpoints import Checkpoint
            kind, self.checkpoint_next = self.checkpoint_next, None
            raise await self.stop_at(page, Checkpoint(kind, 'test_rule'))
        page.url = url
        loaded = Loaded(requested=url, url=url, status=200, title='', html='', responses={},
                        captured_at='2026-10-19T09:30:00+00:00', page=page)
        for pattern, handler in self.pages.items():
            if re.search(pattern, url):
                handler(loaded)
                break
        else:
            self._default(url, loaded, expect or {})
        return loaded

    def _default(self, url, loaded, expect):
        path = re.sub(r'^https?://[^/]+', '', url)
        if path.startswith('/job-listings-'):
            responses, html, _ = fixture('job')
            loaded.html = html
            for item in responses:
                body = copy.deepcopy(item['body'])
                if '/jobapi/v4/job/' in item['url']:
                    body['loggedIn'] = self.logged_in
                for name, regex in expect.items():
                    if re.search(regex, item['url']):
                        loaded.responses.setdefault(name, []).append((item['url'], body))
            job_id = fixture_job_id()
            if not path.endswith(job_id) and job_id not in path:
                loaded.url = self.site.base_url + '/jobs-in-somewhere?expJD=true'
        elif '-jobs' in path:
            responses, html, _ = fixture('search')
            loaded.html = html
            for item in responses:
                for name, regex in expect.items():
                    if re.search(regex, item['url']):
                        loaded.responses.setdefault(name, []).append((item['url'], copy.deepcopy(item['body'])))
        elif '/mnjuser/' in path and not self.logged_in:
            loaded.url = self.site.url(self.site.section('login')['login_url'])
            loaded.login_form = True
        elif '/mnjuser/homepage' in path:
            loaded.logged_in_marker = True


def install(ws, logged_in=True):
    """Put a FakeSession for this workspace into the server state and return it."""
    site_mod.reset()
    site = site_mod.site()
    platforms_data = json.loads((ws.root / 'config' / 'platforms.json').read_text())
    session = FakeSession(ws.root, site, Limiter(ws.root, platforms_data['naukri'].get('limits')), logged_in)
    server.STATE.session = session
    return session
