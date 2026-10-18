"""Login state: a truth table over one fresh read of a protected page, and the hand login flow.

The server never fills, reads or screenshots login fields, never reads cookies, and never treats
an unchecked session as logged in. ``yes`` needs a fresh protected page showing a logged-in
marker; a redirect to the login page or a visible login form is ``no``; timeouts, network and
server errors are ``inconclusive``; a checkpoint is ``inconclusive`` and activates the cooldown;
never checked is ``unknown``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import timedelta
from typing import Any, Awaitable, Callable

from ..errors import JFError
from . import store
from .session import Session

logger = logging.getLogger('jobfather_mcp.naukri')
POLL_S = 2.0
BACKGROUND_S = 900
GOOGLE_NOTE = ("If Google sign-in is refused in this window (Google can block sign-in in automated browsers), use "
               "Naukri's own login instead: email or mobile with password, or a one-time code.")


def classify(*, error: str | None = None, checkpoint: bool = False, redirected_to_login: bool = False,
             login_form: bool = False, logged_in_marker: bool = False) -> tuple[str, str]:
    """(state, basis) for one protected-page read. Pure; the order of checks is the truth table."""
    if checkpoint:
        return 'inconclusive', 'checkpoint page'
    if error:
        return 'inconclusive', error
    if redirected_to_login:
        return 'no', 'redirected to the login page'
    if login_form:
        return 'no', 'login form shown'
    if logged_in_marker:
        return 'yes', 'protected page with logged-in marker'
    return 'inconclusive', 'protected page without a logged-in marker'


def cached_auth(status: dict[str, Any]) -> dict[str, Any]:
    auth = dict(status.get('auth') or {})
    if auth.get('state') not in ('yes', 'no', 'inconclusive'):
        return {'state': 'unknown', 'checked_at': None, 'basis': 'never checked'}
    return auth


async def check_auth(session: Session) -> dict[str, Any]:
    """Read the protected page once (a counted page load) and record the result."""
    url = session.site.url(session.site.section('login')['protected_url'])
    try:
        loaded = await session.load(url, actions=[], ready=None)
    except JFError as error:
        if error.code in ('checkpoint', 'blocked'):
            state, basis = classify(checkpoint=True)
            await asyncio.to_thread(store.set_auth, session.ws, state, basis)
            raise
        if error.code in ('timeout', 'browser_unavailable', 'site_changed', 'not_found'):
            state, basis = classify(error=error.code)
            return await asyncio.to_thread(store.set_auth, session.ws, state, basis)
        raise
    state, basis = classify(redirected_to_login=session.site.is_login_url(loaded.url), login_form=loaded.login_form,
                            logged_in_marker=loaded.logged_in_marker)
    return await asyncio.to_thread(store.set_auth, session.ws, state, basis)


def require_auth(loaded: Any, session: Session) -> None:
    """Raise auth_required when a page that needs a login shows the login page or form instead."""
    if session.site.is_login_url(loaded.url) or loaded.login_form:
        store.set_auth(session.ws, 'no', 'redirected to the login page')
        raise JFError('auth_required', 'Naukri needs the user to be logged in for this.', False,
                      user_action='Call naukri_login; the user signs in in the Naukri window (never in chat).')


async def _signed_in(session: Session, page: Any) -> bool:
    """True when the login tab, or any other tab of the server's window, shows a signed-in Naukri page.

    Only URLs and logged-in markers are looked at, never the form fields.
    """
    pages = [page] + [p for p in (session.context.pages if session.context is not None else []) if p is not page]
    for candidate in pages:
        if await _signed_in_page(session, candidate):
            return True
    return False


async def _signed_in_page(session: Session, page: Any) -> bool:
    if page.is_closed():
        return False
    url = page.url
    if not url or url.startswith('about:') or session.site.is_login_url(url) or not session.site.allowed(url):
        return False
    login = session.site.section('login')
    if await session.visible(page, login.get('logged_in_selectors', [])):
        return True
    return '/mnjuser/' in url


async def login(session: Session, wait_s: float,
                tick: Callable[[float], Awaitable[None]] | None = None) -> dict[str, Any]:
    """Open the login page in the server's window and wait up to wait_s for the user to sign in."""
    site = session.site
    login_url = site.url(site.section('login')['login_url'])
    if session.background is not None and not session.background.done():
        session.background.cancel()  # this call takes over watching
    page = session.login_page if session.login_page is not None and not session.login_page.is_closed() \
        else await session.new_page()
    session.login_page = page
    try:
        await page.bring_to_front()
    except Exception:
        pass
    try:
        await session.load(login_url, actions=[], page=page, login_flow=True)
    except JFError as error:
        if error.code in ('checkpoint', 'blocked'):
            return _result('checkpoint', store.read_status(session.ws), error.message)
        raise
    started = time.monotonic()
    while True:
        if session.context is not None and await _signed_in(session, page):
            return await _finish(session, page)
        if page.is_closed() or session.context is None:
            return _result('window_closed', store.read_status(session.ws),
                           'The Naukri login window was closed before sign-in finished. Call naukri_login again '
                           'when the user is ready.')
        found = await session.checkpoint_on(page, None, login_flow=True)
        if found is not None:
            raise await session.stop_at(page, found)
        elapsed = time.monotonic() - started
        if elapsed >= wait_s:
            break
        if tick is not None:
            with contextlib.suppress(Exception):
                await tick(elapsed)
        await asyncio.sleep(POLL_S)
    _start_background(session, page)
    return _result('waiting_for_user', store.read_status(session.ws),
                   'Sign in to Naukri in the Chrome window that just opened (the user types their own details '
                   'there, never in chat). ' + GOOGLE_NOTE + ' Then call naukri_status; the server keeps watching '
                   'the window for up to 15 minutes.')


async def _finish(session: Session, page: Any) -> dict[str, Any]:
    with contextlib.suppress(Exception):
        if not page.is_closed() and page is not session.page:
            await page.close()
    auth = await check_auth(session)
    if auth['state'] == 'yes':
        await asyncio.to_thread(store.clear_checkpoint, session.ws)
        await asyncio.to_thread(store.set_login_poll, session.ws, 'logged_in')
        return _result('logged_in', store.read_status(session.ws), 'Signed in to Naukri.')
    return _result('waiting_for_user', store.read_status(session.ws),
                   'The window left the login page but Naukri does not show a signed-in page yet (%s). '
                   'Finish signing in there, then call naukri_status with check "live".' % auth.get('basis'))


def _start_background(session: Session, page: Any) -> None:
    if session.background is not None and not session.background.done():
        return
    until = store.now_utc() + timedelta(seconds=BACKGROUND_S)
    store.set_login_poll(session.ws, 'watching', until)
    session.background = asyncio.get_running_loop().create_task(_watch(session, page, BACKGROUND_S))


async def _watch(session: Session, page: Any, limit_s: float) -> None:
    """Keep watching the login tab (no navigation, no field reads) so naukri_status can report a login."""
    started = time.monotonic()
    try:
        while time.monotonic() - started < limit_s:
            await asyncio.sleep(POLL_S)
            if session.context is None:
                await asyncio.to_thread(store.set_login_poll, session.ws, 'window_closed')
                return
            if await _signed_in(session, page):
                async with session.op_lock:
                    try:
                        await _finish(session, page)
                    except JFError as error:
                        logger.info('login check after sign-in failed: %s', error.code)
                return
        await asyncio.to_thread(store.set_login_poll, session.ws, 'expired')
    except asyncio.CancelledError:
        return
    except Exception as exc:  # never let the watcher crash the server
        logger.info('login watcher stopped: %s', type(exc).__name__)


def _result(state: str, status: dict[str, Any], message: str) -> dict[str, Any]:
    return {'ok': True, 'state': state, 'authenticated': cached_auth(status), 'message_for_user': message}
