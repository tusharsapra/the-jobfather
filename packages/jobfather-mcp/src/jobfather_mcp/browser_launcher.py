"""jobfather-browser: start Playwright MCP on the workspace's own Chrome profile.

    jobfather-browser [--workspace PATH] [--print-argv] [--prefetch] [--version] [-- EXTRA PLAYWRIGHT ARGS]

Reads ``config/browser.json`` (``{"mode": "profile"}`` keeps a dedicated profile in
``<ws>/.state/browser/main``; ``{"mode": "extension"}`` attaches to the user's running Chrome
through the Playwright extension) and ``config/platforms.json``. LinkedIn stays blocked unless the
user opted in to LinkedIn in browser mode; Naukri is always blocked here because the Naukri server
owns that site. Blocking is a guard against accidental automation, not a security boundary.

Diagnostics go to stderr only: stdout belongs to the MCP protocol.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .workspace import (WorkspaceError, is_initialized, platform_enabled, read_browser, read_platforms,
                        resolve_with_source, state_dir)

PLAYWRIGHT_MCP = '@playwright/mcp@0.0.83'
LINKEDIN_ORIGINS = ('https://www.linkedin.com', 'https://linkedin.com')
NAUKRI_ORIGINS = ('https://www.naukri.com', 'https://naukri.com', 'https://login.naukri.com')
CHANNELS = ('chrome', 'msedge')
NAVIGATION_TIMEOUT_MS = 30000
IS_WINDOWS = os.name == 'nt'
USAGE = __doc__.split('\n\n')[1].strip()


def log(message: str) -> None:
    print('jobfather-browser: ' + message, file=sys.stderr, flush=True)


def linkedin_in_browser(platforms: dict[str, Any]) -> bool:
    linkedin = platforms.get('linkedin') or {}
    return platform_enabled(platforms, 'linkedin') and linkedin.get('mode') == 'browser'


def blocked_origins(platforms: dict[str, Any]) -> list[str]:
    """Origins Playwright must not request: Naukri always, LinkedIn unless opted in for the browser."""
    origins = [] if linkedin_in_browser(platforms) else list(LINKEDIN_ORIGINS)
    return origins + list(NAUKRI_ORIGINS)


def build_argv(ws: Path | None, browser: dict[str, Any], platforms: dict[str, Any], extra: Sequence[str] = (),
               npx: str = 'npx') -> tuple[list[str], list[str]]:
    """(argv, notes). ``ws`` is None when no workspace is set up yet (in-memory profile).

    ``argv[0]`` is the bare ``npx``; ``main`` swaps in the resolved executable when it runs it.
    """
    notes = []
    mode = browser.get('mode', 'profile')
    if mode not in ('profile', 'extension'):
        notes.append('unknown browser mode %r in config/browser.json; using "profile"' % (mode,))
        mode = 'profile'
    channel = browser.get('channel', 'chrome')
    if channel not in CHANNELS:
        notes.append('unknown browser channel %r; using "chrome"' % (channel,))
        channel = 'chrome'
    argv = [npx, '-y', PLAYWRIGHT_MCP, '--browser', channel]
    if mode == 'extension':
        argv.append('--extension')
    elif ws is not None:
        argv += ['--user-data-dir', str(ws / '.state' / 'browser' / 'main')]
    else:
        argv.append('--isolated')
        notes.append('no workspace yet: using a temporary in-memory browser profile (logins will not persist)')
    if ws is not None:
        argv += ['--output-dir', str(ws / '.state' / 'browser' / 'output')]
    argv += ['--snapshot-mode', 'none', '--timeout-navigation', str(NAVIGATION_TIMEOUT_MS)]
    origins = blocked_origins(platforms)
    if origins:
        argv += ['--blocked-origins', ';'.join(origins)]
    return argv + list(extra), notes


def _split(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    argv = list(argv)
    if '--' in argv:
        index = argv.index('--')
        return argv[:index], argv[index + 1:]
    return argv, []


def _parse(own: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {'workspace': None, 'print_argv': False, 'prefetch': False, 'version': False,
                               'help': False}
    items = iter(own)
    for item in items:
        if item == '--print-argv':
            options['print_argv'] = True
        elif item == '--prefetch':
            options['prefetch'] = True
        elif item == '--version':
            options['version'] = True
        elif item in ('-h', '--help'):
            options['help'] = True
        elif item == '--workspace':
            options['workspace'] = next(items, None)
            if not options['workspace']:
                raise SystemExit('jobfather-browser: --workspace needs a folder')
        elif item.startswith('--workspace='):
            options['workspace'] = item.split('=', 1)[1]
        else:
            raise SystemExit('jobfather-browser: unknown option %s (pass Playwright options after --)\n%s'
                             % (item, USAGE))
    return options


def _settings(explicit: str | None) -> tuple[Path | None, dict[str, Any], dict[str, Any], list[str]]:
    notes = []
    try:
        source, ws = resolve_with_source(explicit)
    except WorkspaceError as error:
        return None, {}, {}, ['workspace unusable (%s); running without it' % error]
    if not is_initialized(ws):
        return None, {}, {}, ['no Jobfather workspace at %s (%s)' % (ws, source)]
    try:
        browser = read_browser(ws)
    except WorkspaceError as error:
        notes.append('%s; using the default profile mode' % error)
        browser = {'mode': 'profile'}
    try:
        platforms = read_platforms(ws)
    except WorkspaceError as error:
        notes.append('%s; keeping LinkedIn and Naukri blocked' % error)
        platforms = {}
    return ws, browser, platforms, notes


def _prepare_folders(ws: Path, argv: list[str]) -> None:
    state_dir(ws, 'browser', 'output')
    if '--user-data-dir' in argv:
        profile = state_dir(ws, 'browser', 'main')
        if (profile / 'SingletonLock').exists() or (profile / 'lockfile').exists():
            log('the browser profile may be in use by another Chrome window or agent session; '
                'close it if Chrome fails to start (profile_locked)')


def main(argv: Sequence[str] | None = None) -> int:
    own, extra = _split(sys.argv[1:] if argv is None else argv)
    options = _parse(own)
    if options['help']:
        print(USAGE, file=sys.stderr)
        return 0
    if options['version']:
        print('jobfather-mcp %s (Playwright MCP %s)' % (__version__, PLAYWRIGHT_MCP.rsplit('@', 1)[1]))
        return 0
    npx = shutil.which('npx')
    if options['prefetch']:
        if not npx:
            log('npx was not found; install Node.js 18 or newer (run doctor.py for the command)')
            return 127
        log('downloading %s into the npx cache' % PLAYWRIGHT_MCP)
        done = subprocess.run([npx, '-y', PLAYWRIGHT_MCP, '--version'], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, errors='replace')
        sys.stderr.write(done.stdout)  # stdout stays clean for the MCP protocol
        return done.returncode
    ws, browser, platforms, notes = _settings(options['workspace'])
    command, more = build_argv(ws, browser, platforms, extra)
    if options['print_argv']:
        print(json.dumps(command))
        return 0
    for note in notes + more:
        log(note)
    if ws is not None:
        try:
            _prepare_folders(ws, command)
        except (OSError, WorkspaceError) as error:
            log('cannot prepare %s: %s' % (ws / '.state' / 'browser', error))
            return 1
    if not npx:
        log('npx was not found on PATH; install Node.js 18 or newer (run doctor.py for the command)')
        return 127
    log('starting %s (LinkedIn %s, Naukri blocked)'
        % (PLAYWRIGHT_MCP, 'allowed' if linkedin_in_browser(platforms) else 'blocked'))
    command[0] = npx
    if IS_WINDOWS:
        try:
            return subprocess.call(command)
        except KeyboardInterrupt:
            return 130
    os.execv(npx, command)
    return 0  # pragma: no cover - execv does not return


if __name__ == '__main__':
    sys.exit(main())
