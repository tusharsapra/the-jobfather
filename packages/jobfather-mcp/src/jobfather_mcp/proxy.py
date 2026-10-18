"""jobfather-proxy: expose only allow-listed tools of another stdio MCP server.

    jobfather-proxy --server-name NAME --allow TOOL[,TOOL...] [--daily-cap TOOL=N ...]
                    [--startup-timeout SECONDS] [--workspace PATH] -- COMMAND [ARGS...]

The proxy runs COMMAND as a child MCP server and sits between it and the host: ``tools/list``
shows only allow-listed tools, ``tools/call`` refuses every other tool, progress and log
notifications pass through, and optional per-tool daily caps are counted in
``<workspace>/.state/proxy/<NAME>-ledger.jsonl`` (tool names and times only, never arguments).
If the child stops, the next call starts it again (a crash loop is refused).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Sequence

import anyio
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp.types.version import MODERN_PROTOCOL_VERSIONS

from . import __version__
from .errors import JFError
from .workspace import WorkspaceError, is_initialized, resolve, state_dir

if sys.version_info >= (3, 11):
    _ExceptionGroup = BaseExceptionGroup  # noqa: F821 - builtin on 3.11+
else:  # pragma: no cover - anyio installs the backport on 3.10
    from exceptiongroup import BaseExceptionGroup as _ExceptionGroup

SERVER_NAME = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')
TOOL_NAME = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
LEVELS = ('debug', 'info', 'notice', 'warning', 'error', 'critical', 'alert', 'emergency')
CLIENT_INFO = types.Implementation(name='jobfather-proxy', version=__version__)
RESTART_WINDOW_S = 600
MAX_STARTS_IN_WINDOW = 4
PING_TIMEOUT_S = 5


def log(message: str) -> None:
    print('jobfather-proxy: ' + message, file=sys.stderr, flush=True)


def _describe(exc: BaseException) -> str:
    while isinstance(exc, _ExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if isinstance(exc, TimeoutError):
        return 'it did not answer in time'
    text = str(exc).strip()
    return ('%s: %s' % (type(exc).__name__, text) if text else type(exc).__name__)[:400]


# ------------------------------------------------------------------ arguments
class Config:
    """Validated command-line settings."""

    def __init__(self, name: str, allow: Sequence[str], caps: dict[str, int], command: str, args: Sequence[str],
                 startup_timeout: float = 120.0, workspace: str | None = None) -> None:
        self.name = name
        self.allow = tuple(allow)
        self.caps = dict(caps)
        self.command = command
        self.args = tuple(args)
        self.startup_timeout = startup_timeout
        self.workspace = workspace


def parse_args(argv: Sequence[str] | None = None) -> Config:
    argv = list(sys.argv[1:] if argv is None else argv)
    own, child = (argv[:argv.index('--')], argv[argv.index('--') + 1:]) if '--' in argv else (argv, [])
    parser = argparse.ArgumentParser(prog='jobfather-proxy', description=__doc__.splitlines()[0],
                                     usage='%(prog)s --server-name NAME --allow TOOLS [options] -- COMMAND [ARGS...]')
    parser.add_argument('--server-name', required=True, help='short name, used for logs and the cap ledger')
    parser.add_argument('--allow', required=True, help='comma-separated tool names to expose')
    parser.add_argument('--daily-cap', action='append', default=[], metavar='TOOL=N',
                        help='at most N calls of TOOL per local calendar day (repeatable)')
    parser.add_argument('--startup-timeout', type=float, default=120.0, help='seconds to wait for the child')
    parser.add_argument('--workspace', help='workspace folder (default: same lookup as the skill)')
    parser.add_argument('--version', action='version', version='jobfather-proxy ' + __version__)
    args = parser.parse_args(own)
    if not SERVER_NAME.match(args.server_name):
        parser.error('--server-name must be 1-32 lower-case letters, digits, "-" or "_"')
    allow = tuple(dict.fromkeys(t.strip() for t in args.allow.split(',') if t.strip()))
    if not allow:
        parser.error('--allow needs at least one tool name')
    bad = [t for t in allow if not TOOL_NAME.match(t)]
    if bad:
        parser.error('invalid tool names: ' + ', '.join(bad))
    caps = {}
    for item in args.daily_cap:
        tool, sep, number = item.partition('=')
        if not sep or tool not in allow or not number.isdigit():
            parser.error('--daily-cap needs TOOL=N for an allowed tool, got %r' % item)
        caps[tool] = int(number)
    if args.startup_timeout <= 0:
        parser.error('--startup-timeout must be positive')
    if not child:
        parser.error('give the wrapped server command after --')
    return Config(args.server_name, allow, caps, child[0], child[1:], args.startup_timeout, args.workspace)


# ------------------------------------------------------------------ daily caps
@contextlib.contextmanager
def _file_lock(path: Path | None, timeout: float = 10.0) -> Iterator[None]:
    if path is None:
        yield
        return
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        start = time.monotonic()
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() - start >= timeout:
                    raise JFError('timeout', 'The cap ledger is busy', True, retry_after_s=2) from None
                time.sleep(0.025)
        try:
            yield
        finally:
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class DailyCaps:
    """Per-tool limits per local calendar day, counted in a JSONL ledger (or in memory without a workspace)."""

    def __init__(self, name: str, caps: dict[str, int], ws: Path | None) -> None:
        self.caps = dict(caps)
        self.path: Path | None = None
        self.lock: Path | None = None
        if self.caps and ws is not None:
            folder = state_dir(ws, 'proxy')
            self.path = folder / (name + '-ledger.jsonl')
            self.lock = folder / (name + '.lock')
        self._memory: list[dict[str, Any]] = []
        self._guard = anyio.Lock()

    @staticmethod
    def clock() -> datetime:
        return datetime.now().astimezone()

    def _entries(self) -> list[dict[str, Any]]:
        if self.path is None:
            return list(self._memory)
        if not self.path.is_file():
            return []
        entries = []
        for line in self.path.read_bytes().decode('utf-8', 'replace').splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue  # a torn last line from a crash; caps pace usage, they are not a security boundary
            if isinstance(item, dict):
                entries.append(item)
        return entries

    def _append(self, entry: dict[str, Any]) -> None:
        if self.path is None:
            self._memory.append(entry)
            return
        with self.path.open('ab') as fh:
            fh.write((json.dumps(entry, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8'))
            fh.flush()
            os.fsync(fh.fileno())

    def used(self, tool: str, day: str) -> int:
        return sum(1 for e in self._entries()
                   if e.get('event') == 'call' and e.get('tool') == tool and e.get('day') == day)

    def reserve_sync(self, tool: str) -> None:
        if tool not in self.caps:
            return
        with _file_lock(self.lock):
            now = self.clock()
            day = now.date().isoformat()
            entry = {'at': now.astimezone(timezone.utc).isoformat(timespec='seconds'), 'day': day, 'tool': tool}
            if self.used(tool, day) >= self.caps[tool]:
                self._append(dict(entry, event='refused'))
                midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                raise JFError('rate_limited', 'Daily limit reached for %s (%d per day); it resets at local midnight.'
                              % (tool, self.caps[tool]), True, retry_after_s=max(1, int((midnight - now).total_seconds())),
                              user_action='Wait until tomorrow, or ask the user before raising the limit.')
            self._append(dict(entry, event='call'))

    async def reserve(self, tool: str) -> None:
        """Count one call of ``tool`` or raise ``rate_limited``."""
        if tool not in self.caps:
            return
        async with self._guard:
            await anyio.to_thread.run_sync(self.reserve_sync, tool)


# ------------------------------------------------------------------ child connection
class _Connection:
    def __init__(self) -> None:
        self.ready = anyio.Event()
        self.closing = anyio.Event()
        self.session: ClientSession | None = None
        self.error: str | None = None
        self.dead = False


class Proxy:
    """One proxy instance; create it inside the running event loop."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.allow = frozenset(config.allow)
        self.params = StdioServerParameters(command=config.command, args=list(config.args), env=dict(os.environ))
        ws = None
        if config.caps:
            try:
                found = resolve(config.workspace)
                if is_initialized(found):
                    ws = found
                else:
                    log('no Jobfather workspace at %s: daily caps are counted in memory only' % found)
            except WorkspaceError as error:
                log('%s: daily caps are counted in memory only' % error)
        self.caps = DailyCaps(config.name, config.caps, ws)
        self._conn: _Connection | None = None
        self._starts: list[float] = []
        self._spawn_lock = anyio.Lock()
        self._tg: Any = None
        self._host: Any = None
        self._log_level: str | None = None
        self._warned_missing = False

    # -------------------------------------------------------------- server
    def build_server(self) -> Server[Any]:
        listed = ', '.join(self.config.allow)
        with warnings.catch_warnings():
            # logging/setLevel is deprecated in protocol 2026-07-28; current hosts still speak the handshake era.
            warnings.simplefilter('ignore')
            server = Server(
                'jobfather-' + self.config.name, version=__version__,
                instructions='Filtered access to the %s MCP server through The Jobfather. Only these tools are '
                             'available: %s. Every other tool of that server is disabled.' % (self.config.name, listed),
                lifespan=self._lifespan, on_list_tools=self._list_tools, on_call_tool=self._call_tool,
                on_set_logging_level=self._set_level)
        server.add_notification_handler('notifications/initialized', types.NotificationParams, self._initialized)
        return server

    @contextlib.asynccontextmanager
    async def _lifespan(self, server: Any) -> AsyncIterator[Proxy]:
        async with anyio.create_task_group() as tg:
            self._tg = tg
            self._spawn()  # start the child now, so the first tools/list is quick
            try:
                yield self
            finally:
                if self._conn is not None:
                    self._conn.closing.set()
                # stdio_client shields the child's shutdown, so cancelling cannot leak the process.
                tg.cancel_scope.cancel()

    def _spawn(self) -> _Connection:
        conn = _Connection()
        self._conn = conn
        self._starts = [t for t in self._starts if time.monotonic() - t < RESTART_WINDOW_S] + [time.monotonic()]
        self._tg.start_soon(self._run, conn)
        return conn

    async def _run(self, conn: _Connection) -> None:
        try:
            async with stdio_client(self.params, errlog=sys.stderr) as (read, write):
                async with ClientSession(read, write, logging_callback=self._child_log,
                                         message_handler=self._child_message, client_info=CLIENT_INFO) as session:
                    with anyio.fail_after(self.config.startup_timeout):
                        result = await session.initialize()
                    conn.session = session
                    conn.ready.set()
                    log('%s server ready (%s %s)' % (self.config.name, result.server_info.name,
                                                     result.server_info.version or '?'))
                    await conn.closing.wait()
        except Exception as exc:
            conn.error = _describe(exc)
            log('the wrapped %s server stopped: %s' % (self.config.name, conn.error))
        finally:
            conn.session = None
            conn.dead = True
            conn.ready.set()

    def _drop(self, session: ClientSession | None) -> None:
        conn = self._conn
        if conn is not None and (session is None or conn.session is session):
            conn.closing.set()

    async def _session(self) -> ClientSession:
        async with self._spawn_lock:
            conn = self._conn
            if conn is None or conn.dead or conn.closing.is_set():
                recent = [t for t in self._starts if time.monotonic() - t < RESTART_WINDOW_S]
                if len(recent) >= MAX_STARTS_IN_WINDOW:
                    raise JFError('browser_unavailable', 'The %s server keeps stopping (%s).'
                                  % (self.config.name, conn.error if conn and conn.error else 'no details'), False,
                                  user_action='Restart this MCP server from the host; run doctor.py if it fails again.')
                conn = self._spawn()
        with anyio.move_on_after(self.config.startup_timeout + 10):
            await conn.ready.wait()
        if conn.session is None:
            raise JFError('browser_unavailable', 'The %s server is not available: %s.'
                          % (self.config.name, conn.error or 'it did not start in time'), True, retry_after_s=30,
                          user_action='Check that uv is installed and the server login is done (doctor.py, connect.py status).')
        return conn.session

    async def _live_session(self) -> ClientSession:
        """A session that answered a ping, restarting a child that died since the last call."""
        session = await self._session()
        with anyio.move_on_after(PING_TIMEOUT_S):
            try:
                await session.send_ping()
            except MCPError as exc:
                if exc.code == types.CONNECTION_CLOSED:
                    self._drop(session)
                    session = await self._session()
        return session

    # -------------------------------------------------------------- handlers
    async def _initialized(self, ctx: Any, params: Any) -> None:
        self._host = ctx.session

    async def _all_tools(self, session: ClientSession) -> list[types.Tool]:
        tools: list[types.Tool] = []
        cursor = None
        for _ in range(100):
            page = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor) if cursor else None)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                break
        return tools

    async def _list_tools(self, ctx: Any, params: Any) -> types.ListToolsResult:
        self._host = ctx.session
        tools: list[types.Tool] = []
        for attempt in (1, 2):
            try:
                session = await self._session()
            except JFError as error:
                raise MCPError(code=types.INTERNAL_ERROR, message=error.message) from None
            try:
                tools = await self._all_tools(session)
                break
            except MCPError as exc:
                if exc.code != types.CONNECTION_CLOSED or attempt == 2:
                    raise
                self._drop(session)
        missing = sorted(self.allow - {t.name for t in tools})
        if missing and not self._warned_missing:
            self._warned_missing = True
            log('allow-listed tools the wrapped server does not offer: ' + ', '.join(missing))
        return types.ListToolsResult(tools=[t for t in tools if t.name in self.allow])

    async def _call_tool(self, ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        self._host = ctx.session
        name = params.name
        if name not in self.allow:
            return JFError('not_enabled', '%r is not available through The Jobfather %s proxy. Allowed tools: %s.'
                           % (name, self.config.name, ', '.join(self.config.allow)), False).to_result()
        try:
            session = await self._live_session()
            await self.caps.reserve(name)
        except JFError as error:
            return error.to_result()

        async def progress(value: float, total: float | None, message: str | None) -> None:
            with contextlib.suppress(Exception):
                await ctx.session.report_progress(value, total, message)

        try:
            return await session.call_tool(name, params.arguments, progress_callback=progress)
        except MCPError as exc:
            if exc.code != types.CONNECTION_CLOSED:
                raise
            self._drop(session)
            return JFError('browser_unavailable', 'The %s server stopped during %s; it restarts on the next call. '
                           'Check whether the action happened before retrying.' % (self.config.name, name),
                           True).to_result()
        except Exception as exc:
            log('unusable result from %s: %s' % (name, _describe(exc)))
            return JFError('internal', 'The wrapped %s server returned an unusable result for %s.'
                           % (self.config.name, name), False).to_result()

    async def _set_level(self, ctx: Any, params: types.SetLevelRequestParams) -> types.EmptyResult:
        self._host = ctx.session
        self._log_level = params.level
        conn = self._conn
        if conn is not None and conn.session is not None:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                with contextlib.suppress(Exception):
                    await conn.session.set_logging_level(params.level)
        return types.EmptyResult()

    # -------------------------------------------------------------- notifications from the child
    def _host_accepts_unsolicited(self) -> bool:
        host = self._host
        return host is not None and host.protocol_version not in MODERN_PROTOCOL_VERSIONS

    async def _child_log(self, params: types.LoggingMessageNotificationParams) -> None:
        if self._log_level in LEVELS and LEVELS.index(params.level) < LEVELS.index(self._log_level):
            return
        if not self._host_accepts_unsolicited():
            log('[%s] %s' % (params.level, str(params.data)[:500]))
            return
        message = types.LoggingMessageNotification(params=types.LoggingMessageNotificationParams(
            level=params.level, logger=params.logger or self.config.name, data=params.data))
        with contextlib.suppress(Exception):
            await self._host.send_notification(message)

    async def _child_message(self, message: Any) -> None:
        if isinstance(message, Exception):
            log('transport error from the wrapped %s server: %s' % (self.config.name, _describe(message)))
        elif isinstance(message, types.ToolListChangedNotification) and self._host_accepts_unsolicited():
            with contextlib.suppress(Exception):
                await self._host.send_tool_list_changed()


async def serve(config: Config) -> None:
    proxy = Proxy(config)
    server = proxy.build_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options(NotificationOptions(tools_changed=True)))


def main(argv: Sequence[str] | None = None) -> int:
    config = parse_args(argv)
    try:
        anyio.run(serve, config)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
