"""Find the user's Jobfather workspace exactly like the skill's ``scripts/local.py`` does.

Order: an explicit argument, then ``JOBFATHER_HOME`` (ignored when empty or when it still holds
an unsubstituted host placeholder such as ``${user_config.workspace_dir}``), then the pointer
file written at onboarding, then ``~/Jobfather`` (``%USERPROFILE%\\Jobfather`` on Windows).
``tests/vectors/workspace_resolution.json`` in the repository keeps both implementations in step.

Stdlib only, so servers can import it before anything heavy loads.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

APP_NAME = 'the-jobfather'
CONFIG = 'config.json'
STATE = '.state'
PLATFORMS = 'config/platforms.json'
BROWSER = 'config/browser.json'
POLICY = 'config/policy.json'
CODE_ROOT = Path(__file__).resolve().parent

DEFAULT_PLATFORMS: dict[str, Any] = {
    'schema': 'the-jobfather/platforms@1',
    'naukri': {'enabled': False, 'acknowledged': None, 'limits': {}},
    'linkedin': {'enabled': False, 'mode': 'browser', 'acknowledged': None},
    'gmail': {'mode': 'drafts_only'},
    'ats': {},
}
DEFAULT_BROWSER: dict[str, Any] = {'mode': 'profile'}


class WorkspaceError(RuntimeError):
    """The workspace is missing, invalid or unsafe."""


# ------------------------------------------------------------------ locations
def config_home() -> Path:
    """Per-user settings folder (holds the workspace pointer and machine.json)."""
    if os.name == 'nt':
        return Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming') / APP_NAME
    base = os.environ.get('XDG_CONFIG_HOME')
    return (Path(base) if base else Path.home() / '.config') / APP_NAME


def pointer_file() -> Path:
    return config_home() / 'workspace'


def machine_file() -> Path:
    return config_home() / 'machine.json'


def default_workspace() -> Path:
    if os.name == 'nt':
        return Path(os.environ.get('USERPROFILE') or Path.home()) / 'Jobfather'
    return Path.home() / 'Jobfather'


def _usable_env(value: str | None) -> bool:
    return bool(value) and '${' not in value


def candidate(explicit: str | os.PathLike[str] | None = None) -> tuple[str, Path]:
    """The winning ``(source, path)`` before resolution; source is argument|JOBFATHER_HOME|pointer|default."""
    if explicit:
        return 'argument', Path(explicit).expanduser()
    env = os.environ.get('JOBFATHER_HOME', '')
    if _usable_env(env):
        return 'JOBFATHER_HOME', Path(env).expanduser()
    if os.environ.get('JOBFATHER_TEST'):
        raise WorkspaceError('Tests must set JOBFATHER_HOME or pass a workspace explicitly')
    pointer = pointer_file()
    if pointer.is_file():
        text = pointer.read_bytes().decode('utf-8').strip()
        if text:
            return 'pointer', Path(text).expanduser()
    return 'default', default_workspace()


def resolve_with_source(explicit: str | os.PathLike[str] | None = None) -> tuple[str, Path]:
    """Like :func:`resolve` but also says where the answer came from."""
    source, path = candidate(explicit)
    path = path.resolve()
    inside_code = path == CODE_ROOT or CODE_ROOT in path.parents
    is_code_checkout = path in CODE_ROOT.parents and (path / '.git').exists()
    if inside_code or is_code_checkout:
        raise WorkspaceError('The workspace must live outside the code folder')
    return source, path


def resolve(explicit: str | os.PathLike[str] | None = None) -> Path:
    """The workspace folder (it may not exist yet)."""
    return resolve_with_source(explicit)[1]


def is_initialized(ws: str | os.PathLike[str]) -> bool:
    return (Path(ws) / CONFIG).is_file()


def require(explicit: str | os.PathLike[str] | None = None) -> Path:
    """The workspace folder, which must already be set up (it has config.json)."""
    ws = resolve(explicit)
    if not is_initialized(ws):
        raise WorkspaceError('No Jobfather workspace at %s yet; set it up with the skill first' % ws)
    return ws


def state_dir(ws: str | os.PathLike[str], *parts: str) -> Path:
    """``<ws>/.state/<parts...>``, creating missing folders as 0700. Never creates the workspace itself."""
    current = Path(ws)
    if not current.is_dir():
        raise WorkspaceError('Workspace folder is missing: %s' % current)
    for part in (STATE,) + tuple(parts):
        if not part or part in ('.', '..') or '/' in part or '\\' in part:
            raise ValueError('Invalid state folder name: %r' % (part,))
        current = current / part
        if current.is_symlink():
            raise WorkspaceError('Refusing a symlinked state folder: %s' % current)
        if not current.is_dir():
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                if not current.is_dir():
                    raise WorkspaceError('Expected a folder at %s' % current) from None
            else:
                if os.name != 'nt':
                    os.chmod(current, 0o700)
    return current


# ------------------------------------------------------------------ settings
def read_json(path: str | os.PathLike[str]) -> Any:
    """UTF-8 JSON without newline translation (same bytes on every OS)."""
    return json.loads(Path(path).read_bytes().decode('utf-8'))


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _settings(ws: str | os.PathLike[str], relative: str, defaults: dict[str, Any]) -> dict[str, Any]:
    path = Path(ws) / relative
    if not path.is_file():
        return copy.deepcopy(defaults)
    try:
        data = read_json(path)
    except (OSError, ValueError) as error:
        raise WorkspaceError('%s is not readable JSON: %s' % (relative, error)) from None
    if not isinstance(data, dict):
        raise WorkspaceError('%s must hold a JSON object' % relative)
    return _merge(defaults, data)


def read_platforms(ws: str | os.PathLike[str]) -> dict[str, Any]:
    """``config/platforms.json`` merged over the defaults (everything opt-in is off)."""
    return _settings(ws, PLATFORMS, DEFAULT_PLATFORMS)


def read_browser(ws: str | os.PathLike[str]) -> dict[str, Any]:
    """``config/browser.json`` merged over the defaults (``{"mode": "profile"}``)."""
    return _settings(ws, BROWSER, DEFAULT_BROWSER)


def platform_enabled(platforms: dict[str, Any], name: str) -> bool:
    """True only when the user opted in (``enabled`` is literally true and acknowledged)."""
    entry = platforms.get(name)
    return isinstance(entry, dict) and entry.get('enabled') is True and bool(entry.get('acknowledged'))
