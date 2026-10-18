"""Shared stdlib primitives: workspace resolution, byte-exact files, locks, time.

The code (this skill) and the user's data (the workspace) are separate folders.
Every script resolves the workspace with `workspace()`; nothing here is personal.
"""
import contextlib
import errno
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import socket
import tempfile
import threading
import time

import layout

CODE_ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ('claude-code', 'codex', 'antigravity', 'cursor', 'other', 'unknown')
APP_NAME = 'the-jobfather'
_HELD = threading.local()


class WorkspaceError(RuntimeError):
    """The workspace is missing, invalid or unsafe."""


def now():
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ config locations
def config_home():
    if os.name == 'nt':
        return Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming') / APP_NAME
    base = os.environ.get('XDG_CONFIG_HOME')
    return (Path(base) if base else Path.home() / '.config') / APP_NAME


def pointer_file():
    return config_home() / 'workspace'


def machine_file():
    return config_home() / 'machine.json'


def default_workspace():
    return Path(os.environ.get('USERPROFILE') or Path.home()) / 'Jobfather' if os.name == 'nt' else Path.home() / 'Jobfather'


def _usable_env(value):
    return bool(value) and '${' not in value


def workspace_candidates(explicit=None):
    """Ordered (source, path) candidates; tests may not fall back to real user locations."""
    if explicit:
        yield 'argument', Path(explicit).expanduser()
        return
    env = os.environ.get('JOBFATHER_HOME', '')
    if _usable_env(env):
        yield 'JOBFATHER_HOME', Path(env).expanduser()
        return
    if os.environ.get('JOBFATHER_TEST'):
        raise WorkspaceError('Tests must set JOBFATHER_HOME or pass a workspace explicitly')
    pointer = pointer_file()
    if pointer.is_file():
        text = read_text(pointer).strip()
        if text:
            yield 'pointer', Path(text).expanduser()
            return
    yield 'default', default_workspace()


def workspace(explicit=None):
    """Resolve the workspace folder (it may not exist yet)."""
    source, path = next(iter(workspace_candidates(explicit)))
    path = path.resolve()
    code = CODE_ROOT.resolve()
    inside_code = path == code or code in path.parents
    is_code_checkout = path in code.parents and (path / '.git').exists()
    if inside_code or is_code_checkout:
        raise WorkspaceError('The workspace must live outside the skill/code folder')
    return path


def require_workspace(explicit=None):
    path = workspace(explicit)
    if not (path / layout.CONFIG).is_file():
        raise WorkspaceError('No Jobfather workspace at %s yet; run the onboarding workflow first' % path)
    return path


def add_workspace_arg(parser):
    parser.add_argument('--workspace', help='workspace folder (default: JOBFATHER_HOME, pointer file, ~/Jobfather)')
    return parser


def write_pointer(path):
    pointer = pointer_file()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    atomic_text(pointer, str(Path(path).resolve()) + '\n')
    return pointer


# ------------------------------------------------------------------ byte-exact files
def read_bytes(path):
    return Path(path).read_bytes()


def read_text(path):
    """UTF-8 without newline translation, so hashes match on every OS."""
    return Path(path).read_bytes().decode('utf-8')


def read_json(path):
    return json.loads(read_text(path))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def sha_json(value):
    return sha_text(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')))


def _replace(source, target, attempts=6):
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            # Windows: antivirus, indexers or an open spreadsheet can hold the target briefly.
            if os.name != 'nt' or attempt == attempts - 1:
                raise
            time.sleep(0.05)


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def atomic_text(path, text):
    atomic_bytes(path, text.encode('utf-8'))


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def atomic_json(path, value):
    atomic_text(path, dumps(value))


def posix(path):
    return Path(path).as_posix()


def contained(root, relative):
    """Join a relative POSIX path under root, refusing traversal and symlinks."""
    root = Path(root).resolve()
    relative = Path(str(relative).replace('\\', '/'))
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Expected a relative path without parent traversal')
    path = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Symlink paths are not accepted')
    if path.resolve() != root and root not in path.resolve().parents:
        raise ValueError('Path escapes root')
    return path


# ------------------------------------------------------------------ locks
def _host_tag():
    return hashlib.sha256(socket.gethostname().encode('utf-8', 'replace')).hexdigest()[:12]


@contextlib.contextmanager
def shared_lock(lock_path, timeout=10):
    """OS advisory lock on a file that is never written; metadata lives beside it.

    Re-entrant per thread. A killed process releases its lock; old metadata never
    authorises deleting a live lock.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(lock_path.resolve())
    held = getattr(_HELD, 'locks', frozenset())
    if key in held:
        yield
        return
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    stream = os.fdopen(fd, 'r+b')
    try:
        start = time.monotonic()
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno in (getattr(errno, 'ENOTSUP', -1), getattr(errno, 'EOPNOTSUPP', -1), errno.ENOLCK):
                    raise WorkspaceError('This folder does not support file locks; move the workspace to a local disk')
                if time.monotonic() - start >= timeout:
                    raise RuntimeError('Workspace lock busy (%s); another task may be running. Do not delete it.' % lock_path.name)
                time.sleep(0.025)
        _HELD.locks = held | {key}
        try:
            atomic_text(Path(str(lock_path) + '.json'), json.dumps(
                {'pid': os.getpid(), 'host': _host_tag(), 'acquired_at': now()}) + '\n')
            yield
        finally:
            _HELD.locks = held
            if os.name == 'nt':
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def root_lock(ws, timeout=10):
    return shared_lock(Path(ws) / layout.ROOT_LOCK, timeout)


@contextlib.contextmanager
def locked(directory, timeout=5):
    """Lock one store directory (an application, the job registry, the pipeline)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with shared_lock(directory / '.jobfather.lock', timeout):
        yield


# ------------------------------------------------------------------ settings
DEFAULT_CONFIG = {
    'schema_version': 1,
    'pipeline': {'rank_limit': 10, 'top': 5, 'research_days': 30, 'follow_up_days': 10,
                 'max_follow_ups': 2, 'sync_limit': 50, 'overlap_days': 2},
    'tracker': {'date_format': '%Y-%m-%d', 'datetime_format': '%Y-%m-%d %H:%M',
                'stages': ['New', 'Reviewing', 'Drafted', 'Applied', 'Interviewing', 'Offer',
                           'Rejected', 'Withdrawn', 'Closed', 'Skipped']},
    'authorization_keywords': [],
    'evaluation_mode': False,
}


def _merge(base, extra):
    out = dict(base)
    for key, value in (extra or {}).items():
        out[key] = _merge(base[key], value) if isinstance(value, dict) and isinstance(base.get(key), dict) else value
    return out


def config(ws):
    path = Path(ws) / layout.CONFIG
    return _merge(DEFAULT_CONFIG, read_json(path) if path.is_file() else {})


def identity(ws):
    path = Path(ws) / layout.IDENTITY
    return read_json(path) if path.is_file() else {}


def machine():
    path = machine_file()
    return read_json(path) if path.is_file() else {}


_OFFSET = re.compile(r'^([+-])(\d{2}):?(\d{2})$')


def tz(ws=None):
    """(tzinfo, name, exact). Falls back to a fixed offset, then UTC, when tz data is missing."""
    name, fallback = None, None
    if ws is not None:
        locale = identity(ws).get('locale') or {}
        name = locale.get('timezone')
        fallback = locale.get('utc_offset_fallback') or config(ws).get('utc_offset_fallback')
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name), name, True
        except Exception:  # ZoneInfoNotFoundError or missing tzdata on Windows
            pass
    if fallback:
        m = _OFFSET.match(fallback)
        if m:
            delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
            return timezone(-delta if m.group(1) == '-' else delta), fallback, False
    return timezone.utc, 'UTC', name == 'UTC'


def local_now(ws=None):
    return datetime.now(timezone.utc).astimezone(tz(ws)[0])


def platform(value):
    if value not in PLATFORMS:
        raise ValueError('Platform must be explicit: ' + ', '.join(PLATFORMS))
    return value


def context_revision(ws):
    """Every task crosses the recovery barrier first (see memory.current)."""
    from memory import current
    return current(ws)
