"""One error shape for every Jobfather MCP tool.

Tools raise ``JFError(code, message, retryable, retry_after_s=None, user_action=None)``. The
helpers turn it into an MCP tool result with ``isError: true``: the error object travels as JSON
in the text block (what every host shows the model) and again in ``structuredContent``::

    {"error": {"code": "rate_limited", "message": "...", "retryable": true,
               "retry_after_s": 600, "user_action": null}}

``mcp`` is imported lazily, so this module (and ``approvals``) stay importable without it.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
from typing import Any, Callable, TypeVar

CODES = (
    'not_enabled',          # the user has not opted in to this platform or tool
    'workspace_invalid',    # no workspace, or its config cannot be read
    'invalid_argument',     # the caller sent something unusable
    'browser_unavailable',  # Chrome or the wrapped browser server could not start
    'profile_locked',       # another browser is using the same profile folder
    'auth_required',        # the user must log in (in the browser window, never in chat)
    'auth_inconclusive',    # login state could not be confirmed either way
    'checkpoint',           # CAPTCHA, verification or security check: stop and tell the user
    'blocked',              # the platform refused or restricted the account: stop, never retry
    'rate_limited',         # pacing or a daily cap; wait retry_after_s
    'site_changed',         # the page no longer matches what the server expects
    'not_found',            # the job, page or item does not exist
    'closed',               # the job no longer accepts applications
    'approval_required',    # an outward action needs the user's per-item approval
    'approval_invalid',     # unknown, damaged, cancelled or revoked approval
    'approval_expired',     # approval older than its time limit
    'approval_consumed',    # approval already used once
    'approval_mismatch',    # approval is for another kind of action or another payload
    'flow_expired',         # a prepared multi-step flow timed out; prepare again
    'timeout',              # the operation took too long
    'internal',             # a bug; details are in the server log
)

logger = logging.getLogger('jobfather_mcp')
_F = TypeVar('_F', bound=Callable[..., Any])


class JFError(Exception):
    """A tool failure the agent can act on."""

    def __init__(self, code: str, message: str, retryable: bool, retry_after_s: float | None = None,
                 user_action: str | None = None) -> None:
        if code not in CODES:
            raise ValueError('Unknown Jobfather error code: %r' % (code,))
        if not isinstance(message, str) or not message.strip():
            raise ValueError('JFError needs a message')
        if retry_after_s is not None and (isinstance(retry_after_s, bool)
                                          or not isinstance(retry_after_s, (int, float)) or retry_after_s < 0):
            raise ValueError('retry_after_s must be a non-negative number of seconds')
        if user_action is not None and not isinstance(user_action, str):
            raise ValueError('user_action must be text')
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = bool(retryable)
        self.retry_after_s = retry_after_s
        self.user_action = user_action

    def __str__(self) -> str:
        return '%s: %s' % (self.code, self.message)

    def to_dict(self) -> dict[str, Any]:
        return {'code': self.code, 'message': self.message, 'retryable': self.retryable,
                'retry_after_s': self.retry_after_s, 'user_action': self.user_action}

    def to_text(self) -> str:
        return json.dumps({'error': self.to_dict()}, ensure_ascii=False)

    def to_result(self) -> Any:
        """An ``mcp.types.CallToolResult`` with ``is_error=True``."""
        from mcp.types import CallToolResult, TextContent
        return CallToolResult(content=[TextContent(type='text', text=self.to_text())],
                              structured_content={'error': self.to_dict()}, is_error=True)


def error_result(code: str, message: str, retryable: bool, retry_after_s: float | None = None,
                 user_action: str | None = None) -> Any:
    """Build the error tool result directly."""
    return JFError(code, message, retryable, retry_after_s, user_action).to_result()


def as_jferror(exc: BaseException, tool: str | None = None) -> JFError:
    """Map any exception to a JFError; unexpected ones are logged (stderr) and become ``internal``."""
    if isinstance(exc, JFError):
        return exc
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return JFError('timeout', 'The operation timed out%s.' % (' in ' + tool if tool else ''), True)
    logger.error('Unexpected error in %s', tool or 'a tool', exc_info=(type(exc), exc, exc.__traceback__))
    return JFError('internal', 'Unexpected error%s; details are in the server log.' % (' in ' + tool if tool else ''),
                   False)


def result_for(exc: BaseException, tool: str | None = None) -> Any:
    return as_jferror(exc, tool).to_result()


def _protocol_error(exc: BaseException) -> bool:
    try:
        from mcp.shared.exceptions import MCPError
    except ImportError:  # pragma: no cover - mcp is a hard dependency of the servers
        return False
    return isinstance(exc, MCPError)


def tool_errors(fn: _F) -> _F:
    """Decorator for tool functions: JFError and unexpected errors become error results.

    Keeps the wrapped signature (MCPServer reads parameters through ``__wrapped__``). Protocol
    errors (``MCPError``) still propagate.
    """
    name = getattr(fn, '__name__', 'tool')
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                if _protocol_error(exc):
                    raise
                return result_for(exc, name)
        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if _protocol_error(exc):
                raise
            return result_for(exc, name)
    return wrapper  # type: ignore[return-value]
