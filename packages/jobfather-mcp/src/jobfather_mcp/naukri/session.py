"""The server's own Chrome window: lazy start, profile lock, idle close, passive page reads.

Real Google Chrome through Playwright (``channel="chrome"``) with a persistent profile in
``<ws>/.state/browser/naukri``. No stealth flags, no changed user agent, no init scripts on Naukri
pages, no proxies. The server never reads or exports cookies and never records request headers;
it reads only the JSON responses the page itself requested (matched by URL pattern) and the
rendered page. Any checkpoint stops the run with the window left open for the user.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import platform
import plistlib
import re
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from ..errors import JFError
from ..workspace import state_dir
from . import site as site_mod
from . import store
from .checkpoints import Checkpoint
from .limits import Limiter

logger = logging.getLogger('jobfather_mcp.naukri')
IDLE_S = 600
FLOW_IDLE_S = 1800
IDLE_CHECK_S = 30
NAV_TIMEOUT_MS = 30000
CONFIRM_HTML = Path(__file__).resolve().parent / 'confirm.html'


# ====================================================================== install checks
def playwright_version() -> str | None:
    try:
        return metadata.version('playwright')
    except metadata.PackageNotFoundError:
        return None


def chrome_info() -> dict[str, Any]:
    """Whether Google Chrome is installed and its version, without starting it."""
    system = platform.system()
    if system == 'Darwin':
        for base in (Path('/Applications'), Path.home() / 'Applications'):
            plist = base / 'Google Chrome.app' / 'Contents' / 'Info.plist'
            if plist.is_file():
                try:
                    with plist.open('rb') as fh:
                        return {'found': True, 'version': plistlib.load(fh).get('CFBundleShortVersionString')}
                except (OSError, ValueError, plistlib.InvalidFileException):
                    return {'found': True, 'version': None}
        return {'found': False, 'version': None}
    if system == 'Windows':
        roots = [os.environ.get(k) for k in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA')]
        for root in filter(None, roots):
            app = Path(root) / 'Google' / 'Chrome' / 'Application'
            if (app / 'chrome.exe').is_file():
                versions = sorted((p.name for p in app.iterdir() if re.fullmatch(r'\d+(?:\.\d+){3}', p.name)),
                                  key=lambda v: [int(x) for x in v.split('.')])
                return {'found': True, 'version': versions[-1] if versions else None}
        return {'found': False, 'version': None}
    for binary in ('/opt/google/chrome/chrome', '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable'):
        if os.path.exists(binary):
            try:
                out = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=5).stdout
                match = re.search(r'(\d+(?:\.\d+){1,3})', out)
                return {'found': True, 'version': match.group(1) if match else None}
            except (OSError, subprocess.SubprocessError):
                return {'found': True, 'version': None}
    return {'found': False, 'version': None}


# ====================================================================== profile lock
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_start(pid: int) -> str | None:
    """The OS's start time of a process (guards against reused pids), best effort."""
    try:
        if sys.platform.startswith('linux'):
            stat = Path('/proc/%d/stat' % pid).read_text()
            return stat.rsplit(')', 1)[1].split()[19]
        if sys.platform == 'darwin':
            out = subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True, timeout=3)
            return out.stdout.strip() or None
    except (OSError, IndexError, subprocess.SubprocessError):
        return None
    return None


class ProfileLock:
    """``<ws>/.state/naukri/browser.lock`` created with O_EXCL; holds pid, start time and host."""

    def __init__(self, ws: Path, profile: Path) -> None:
        self.path = store.folder(ws) / 'browser.lock'
        self.profile = profile
        self.held = False

    def describe(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    def other_owner(self) -> bool:
        """True when another live process holds our lock or Chrome's own profile lock."""
        if self.held:
            return False
        info = self.describe()
        if info and self._owner_alive(info):
            return True
        return self._chrome_lock_alive()

    def _owner_alive(self, info: Mapping[str, Any]) -> bool:
        if info.get('host') != socket.gethostname():
            return True  # another machine (shared folder): cannot check, assume in use
        pid = int(info.get('pid') or 0)
        if pid == os.getpid() or not _pid_alive(pid):
            return False
        started = info.get('proc_start')
        current = _process_start(pid)
        return not (started and current and started != current)

    def _chrome_lock_alive(self) -> bool:
        singleton = self.profile / 'SingletonLock'
        if os.path.lexists(singleton):
            try:
                target = os.readlink(singleton)
            except OSError:
                return True
            host, _, pid = target.rpartition('-')
            if host and host != socket.gethostname():
                return True
            return pid.isdigit() and _pid_alive(int(pid))
        lockfile = self.profile / 'lockfile'  # Windows Chrome keeps this open while running
        if os.name == 'nt' and lockfile.exists():
            try:
                with open(lockfile, 'a'):
                    return False
            except OSError:
                return True
        return False

    def acquire(self) -> None:
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                info = self.describe()
                if info and self._owner_alive(info):
                    raise self.error() from None
                with contextlib.suppress(OSError):
                    os.unlink(self.path)  # stale: its owner is gone
                continue
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                json.dump({'pid': os.getpid(), 'proc_start': _process_start(os.getpid()), 'host': socket.gethostname(),
                           'since': store.iso(store.now_utc())}, fh)
            self.held = True
            if self._chrome_lock_alive():
                self.release()
                raise self.error()
            return
        raise self.error()

    def release(self) -> None:
        if self.held:
            with contextlib.suppress(OSError):
                os.unlink(self.path)
            self.held = False

    def error(self) -> JFError:
        return JFError('profile_locked', 'The Naukri browser profile is in use by another Chrome window or agent '
                       'session.', True, retry_after_s=60,
                       user_action='Close the other Naukri window (or the other agent session using it), then retry.')


# ====================================================================== page reads
@dataclass
class Loaded:
    requested: str
    url: str
    status: int | None
    title: str
    html: str
    responses: dict[str, list[tuple[str, Any]]] = field(default_factory=dict)
    logged_in_marker: bool = False
    logged_out_marker: bool = False
    login_form: bool = False
    captured_at: str = ''
    page: Any = None

    def first(self, name: str) -> tuple[str, Any] | None:
        items = self.responses.get(name) or []
        return items[-1] if items else None


class Session:
    """One Chrome window per server process. Use ``op_lock`` around browser operations."""

    def __init__(self, ws: Path, site: site_mod.Site, limiter: Limiter) -> None:
        self.ws = Path(ws)
        self.site = site
        self.limiter = limiter
        self.profile_dir = state_dir(self.ws, 'browser', 'naukri')
        self.lock = ProfileLock(self.ws, self.profile_dir)
        self.op_lock = asyncio.Lock()
        self.pw: Any = None
        self.context: Any = None
        self.page: Any = None
        self.login_page: Any = None
        self.flows: dict[str, Any] = {}
        self.background: asyncio.Task[Any] | None = None
        self.idle_task: asyncio.Task[Any] | None = None
        self.last_activity = time.monotonic()
        self.profile_cache: tuple[float, dict[str, Any]] | None = None
        self._closing = False

    # ------------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return self.context is not None

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    async def ensure(self) -> Any:
        """The main page, starting Chrome on first use."""
        self.touch()
        if self.context is not None:
            if self.page is None or self.page.is_closed():
                self.page = await self.context.new_page()
            return self.page
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise JFError('browser_unavailable', 'Playwright is not installed for the Naukri server.', False,
                          user_action='Reinstall the server (uvx installs Playwright with it).') from None
        await asyncio.to_thread(self.lock.acquire)
        try:
            if self.pw is None:  # a window the user closed leaves the driver running; reuse it
                self.pw = await async_playwright().start()
            self.context = await self.pw.chromium.launch_persistent_context(
                str(self.profile_dir), channel=site_mod.browser_channel(), headless=site_mod.headless(),
                no_viewport=True, accept_downloads=False)
        except Exception as exc:
            await self._teardown()
            text = str(exc)
            if re.search(r'SingletonLock|ProcessSingleton|profile.*(?:in use|locked)|user data directory is already',
                         text, re.I):
                raise self.lock.error() from None
            logger.warning('Chrome did not start: %s', text.splitlines()[0][:300] if text else type(exc).__name__)
            raise JFError('browser_unavailable', 'Google Chrome could not be started for Naukri.', True,
                          retry_after_s=30, user_action='Install Google Chrome (or update it), close any Chrome '
                          'window using the Naukri profile, then retry.') from None
        self.context.on('close', lambda *_: self._on_context_closed())
        pages = [p for p in self.context.pages if not p.is_closed()]
        self.page = pages[0] if pages else await self.context.new_page()
        self.idle_task = asyncio.get_running_loop().create_task(self._idle_watch())
        return self.page

    def _on_context_closed(self) -> None:
        if not self._closing:
            logger.info('the Naukri Chrome window was closed')
            self.context = None
            self.page = None
            self.flows.clear()
            self.lock.release()

    async def _idle_watch(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while self.context is not None:
                await asyncio.sleep(IDLE_CHECK_S)
                limit = FLOW_IDLE_S if self.flows else IDLE_S
                busy = self.op_lock.locked() or (self.background is not None and not self.background.done())
                if not busy and time.monotonic() - self.last_activity >= limit:
                    logger.info('closing the idle Naukri browser')
                    await self.close(from_idle=True)
                    return

    async def close(self, from_idle: bool = False) -> bool:
        was_open = self.context is not None
        if self.background is not None and not self.background.done():
            self.background.cancel()
        if self.idle_task is not None and not from_idle and not self.idle_task.done():
            self.idle_task.cancel()
        self.flows.clear()
        await self._teardown()
        return was_open

    async def _teardown(self) -> None:
        self._closing = True
        try:
            if self.context is not None:
                with contextlib.suppress(Exception):
                    await self.context.close()
            if self.pw is not None:
                with contextlib.suppress(Exception):
                    await self.pw.stop()
        finally:
            self.context = None
            self.pw = None
            self.page = None
            self.login_page = None
            self.lock.release()
            self._closing = False

    async def new_page(self) -> Any:
        await self.ensure()
        return await self.context.new_page()

    def detach_main_page(self) -> Any:
        """Hand the current main page to a flow; later reads use a fresh tab."""
        page = self.page
        self.page = None
        return page

    # ------------------------------------------------------------------ markers
    async def visible(self, page: Any, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                if await page.locator(selector).filter(visible=True).count():
                    return True
            except Exception:
                continue
        return False

    async def checkpoint_on(self, page: Any, status: int | None, login_flow: bool = False) -> Checkpoint | None:
        rules = self.site.checkpoint_rules
        try:
            title = await page.title()
        except Exception:
            title = ''
        try:
            text = await page.inner_text('body', timeout=5000)
        except Exception:
            text = ''
        visible = []
        for rule_id, selector in rules.element_selectors():
            if await self.visible(page, [selector]):
                visible.append(rule_id)
        return rules.detect(url=page.url, status=status, title=title, text=text, visible=visible,
                            in_login_flow=login_flow)

    async def stop_at(self, page: Any, found: Checkpoint) -> JFError:
        """Record the checkpoint (24 h cooldown), leave the window open, and return the error to raise."""
        await asyncio.to_thread(store.set_checkpoint, self.ws, found.kind, found.rule)
        with contextlib.suppress(Exception):
            await page.bring_to_front()
        return JFError(found.code, found.message() + ' The server stopped and will not retry.', False,
                       user_action='Tell the user: the Naukri window is open; they can look at it themselves. '
                                   'Naukri automation pauses for 24 hours or until they sign in again with '
                                   'naukri_login.')

    # ------------------------------------------------------------------ reads
    async def load(self, url: str, *, actions: list[str], expect: Mapping[str, str] | None = None,
                   required: tuple[str, ...] = (), ready: str | None = None, wait_s: float = 15.0,
                   page: Any = None, login_flow: bool = False) -> Loaded:
        """Open ``url`` (counted and spaced), capture matching JSON responses, stop at checkpoints."""
        if not self.site.allowed(url):
            raise JFError('invalid_argument', 'The server only opens Naukri pages.', False)
        await self.limiter.acquire(['page'] + list(actions))
        page = page or await self.ensure()
        self.touch()
        patterns = {name: re.compile(regex) for name, regex in (expect or {}).items()}
        seen: list[tuple[str, Any]] = []

        def on_response(response: Any) -> None:
            try:
                if response.request.resource_type not in ('xhr', 'fetch'):
                    return
                for name, pattern in patterns.items():
                    if pattern.search(response.url):
                        seen.append((name, response))
                        return
            except Exception:
                return

        page.on('response', on_response)
        try:
            try:
                response = await page.goto(url, wait_until='domcontentloaded', timeout=NAV_TIMEOUT_MS)
            except Exception as exc:
                if 'Timeout' in type(exc).__name__ or 'timeout' in str(exc).lower():
                    raise JFError('timeout', 'Naukri did not load in time.', True, retry_after_s=30) from None
                if page.is_closed():
                    raise JFError('browser_unavailable', 'The Naukri window was closed.', True) from None
                raise JFError('timeout', 'Naukri could not be reached (network error).', True,
                              retry_after_s=30) from None
            status = response.status if response is not None else None
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                names = {name for name, _ in seen}
                if required and all(r in names for r in required):
                    break
                if not required and ready:
                    with contextlib.suppress(Exception):
                        if await page.locator(ready).count():
                            break
                if not required and not ready:
                    break
                await asyncio.sleep(0.25)
            await asyncio.sleep(0.3)
            bodies: dict[str, list[tuple[str, Any]]] = {}
            for name, resp in list(seen):
                try:
                    body = await resp.json()
                except Exception:
                    continue
                bodies.setdefault(name, []).append((resp.url, body))
        finally:
            page.remove_listener('response', on_response)
        final = page.url
        found = await self.checkpoint_on(page, status, login_flow=login_flow)
        if found is not None:
            raise await self.stop_at(page, found)
        if not self.site.allowed(final) and not final.startswith('about:'):
            with contextlib.suppress(Exception):
                await page.goto('about:blank')
            raise JFError('site_changed', 'Naukri sent the browser to another site (site profile %s).'
                          % self.site.version, False)
        if status == 404:
            raise JFError('not_found', 'Naukri says this page does not exist.', False)
        if status is not None and status >= 500:
            raise JFError('timeout', 'Naukri answered with a server error (HTTP %d).' % status, True, retry_after_s=60)
        login = self.site.section('login')
        loaded = Loaded(requested=url, url=final, status=status, title='', html='', responses=bodies,
                        captured_at=store.iso(store.now_utc()) or '', page=page)
        with contextlib.suppress(Exception):
            loaded.title = await page.title()
        with contextlib.suppress(Exception):
            loaded.html = await page.content()
        loaded.logged_in_marker = await self.visible(page, login.get('logged_in_selectors', []))
        loaded.logged_out_marker = await self.visible(page, login.get('logged_out_selectors', []))
        loaded.login_form = await self.visible(page, login.get('login_form_selectors', []))
        self.touch()
        return loaded

    # ------------------------------------------------------------------ confirm tab
    async def confirm(self, data: Mapping[str, Any], timeout_s: float,
                      tick: Callable[[float], Awaitable[None]] | None = None) -> str:
        """Show the confirm tab and wait for the user's click: 'submit', 'cancel' or 'timeout'."""
        await self.ensure()
        tab = await self.context.new_page()
        token = secrets.token_hex(16)
        try:
            await tab.set_content(CONFIRM_HTML.read_text(encoding='utf-8'))
            await tab.evaluate('([data, token]) => window.__jf_render(data, token)', [dict(data), token])
            await tab.bring_to_front()
            deadline = time.monotonic() + timeout_s
            last_tick = time.monotonic()
            while time.monotonic() < deadline:
                self.touch()
                if tab.is_closed():
                    return 'cancel'
                try:
                    decision = await tab.evaluate('() => window.__jf_decision || null')
                except Exception:
                    if tab.is_closed():
                        return 'cancel'
                    decision = None
                if isinstance(decision, dict) and decision.get('token') == token and decision.get('trusted') is True:
                    if decision.get('decision') in ('submit', 'cancel'):
                        return decision['decision']
                if tick is not None and time.monotonic() - last_tick >= 10:
                    last_tick = time.monotonic()
                    with contextlib.suppress(Exception):
                        await tick(time.monotonic() - (deadline - timeout_s))
                await asyncio.sleep(0.4)
            return 'timeout'
        finally:
            with contextlib.suppress(Exception):
                if not tab.is_closed():
                    await tab.close()

    async def screenshot(self, page: Any, name: str) -> str | None:
        """Save a screenshot under evidence/ and return its workspace-relative path."""
        directory = store.evidence_dir(self.ws)
        path = directory / name
        try:
            await page.screenshot(path=str(path), full_page=False, timeout=15000)
        except Exception:
            return None
        return path.relative_to(self.ws).as_posix()
