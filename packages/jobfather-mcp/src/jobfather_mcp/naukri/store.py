"""Server state on disk under ``<ws>/.state/naukri/`` (states only: never cookies, headers or page bodies).

- ``status.json``: auth state, search verification, checkpoint/cooldown and the login poller.
- ``ledger.jsonl``: pacing and daily caps (see ``limits``).
- ``flows/<flow_id>.json``: questionnaire flows (questions, approved answers, status).
- ``evidence/``: confirmation screenshots.

Writes are atomic and serialised with a file lock, so several hosts can share one workspace.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from ..errors import JFError
from ..workspace import state_dir

SCHEMA = 'the-jobfather/naukri-status@1'
FLOW_ID = re.compile(r'^flw_[0-9a-f]{16}$')


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).isoformat(timespec='seconds') if moment else None


def parse_iso(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def folder(ws: str | os.PathLike[str], *parts: str) -> Path:
    return state_dir(ws, 'naukri', *parts)


@contextlib.contextmanager
def locked(path: Path, timeout: float = 10.0) -> Iterator[None]:
    """An exclusive advisory lock on ``path`` (created if needed)."""
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
                    raise JFError('timeout', 'The Naukri state files are busy', True, retry_after_s=2) from None
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


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name + '-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write((json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_bytes().decode('utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ status.json
DEFAULT_STATUS: dict[str, Any] = {
    'schema': SCHEMA,
    'auth': {'state': 'unknown', 'checked_at': None, 'basis': None},
    'search_verified': {'state': 'unknown', 'checked_at': None, 'jobs_seen': None},
    'checkpoint': {'active': False, 'kind': None, 'rule': None, 'since': None, 'cooldown_until': None},
    'login_poll': {'state': None, 'since': None, 'until': None},
}


def read_status(ws: str | os.PathLike[str]) -> dict[str, Any]:
    data = _read(folder(ws) / 'status.json')
    out = json.loads(json.dumps(DEFAULT_STATUS))
    for key, value in data.items():
        if key in out and isinstance(value, dict):
            out[key].update({k: v for k, v in value.items() if k in out[key]})
    return out


def update_status(ws: str | os.PathLike[str], change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    base = folder(ws)
    with locked(base / 'status.lock'):
        status = read_status(ws)
        change(status)
        status['schema'] = SCHEMA
        status['updated_at'] = iso(now_utc())
        atomic_json(base / 'status.json', status)
        return status


def set_auth(ws: str | os.PathLike[str], state: str, basis: str) -> dict[str, Any]:
    def change(status: dict[str, Any]) -> None:
        status['auth'] = {'state': state, 'checked_at': iso(now_utc()), 'basis': basis}
    return update_status(ws, change)['auth']


def set_search_verified(ws: str | os.PathLike[str], jobs_seen: int) -> None:
    def change(status: dict[str, Any]) -> None:
        state = 'yes' if jobs_seen > 0 else 'inconclusive'
        if status['search_verified'].get('state') == 'yes' and state != 'yes':
            return  # an empty search does not undo an earlier verified one
        status['search_verified'] = {'state': state, 'checked_at': iso(now_utc()), 'jobs_seen': jobs_seen}
    update_status(ws, change)


def set_checkpoint(ws: str | os.PathLike[str], kind: str, rule: str, hours: int = 24) -> dict[str, Any]:
    moment = now_utc()

    def change(status: dict[str, Any]) -> None:
        status['checkpoint'] = {'active': True, 'kind': kind, 'rule': rule, 'since': iso(moment),
                                'cooldown_until': iso(moment + timedelta(hours=hours))}
        if status['auth'].get('state') == 'yes':
            status['auth'] = {'state': 'inconclusive', 'checked_at': iso(moment), 'basis': 'checkpoint page'}
    return update_status(ws, change)['checkpoint']


def clear_checkpoint(ws: str | os.PathLike[str]) -> None:
    def change(status: dict[str, Any]) -> None:
        status['checkpoint'] = dict(DEFAULT_STATUS['checkpoint'])
    update_status(ws, change)


def active_checkpoint(status: dict[str, Any], moment: datetime | None = None) -> dict[str, Any] | None:
    """The checkpoint record while its cooldown runs, else None."""
    checkpoint = status.get('checkpoint') or {}
    if not checkpoint.get('active'):
        return None
    until = parse_iso(checkpoint.get('cooldown_until'))
    if until is not None and (moment or now_utc()) >= until:
        return None
    return checkpoint


def set_login_poll(ws: str | os.PathLike[str], state: str | None, until: datetime | None = None) -> None:
    def change(status: dict[str, Any]) -> None:
        status['login_poll'] = {'state': state, 'since': iso(now_utc()), 'until': iso(until)}
    update_status(ws, change)


# ------------------------------------------------------------------ flows
def flow_path(ws: str | os.PathLike[str], flow_id: str) -> Path:
    if not FLOW_ID.fullmatch(flow_id or ''):
        raise JFError('invalid_argument', 'Invalid flow id', False)
    return folder(ws, 'flows') / (flow_id + '.json')


def write_flow(ws: str | os.PathLike[str], flow: dict[str, Any]) -> None:
    path = flow_path(ws, flow['flow_id'])
    with locked(path.parent / '.lock'):
        atomic_json(path, flow)


def read_flow(ws: str | os.PathLike[str], flow_id: str) -> dict[str, Any] | None:
    data = _read(flow_path(ws, flow_id))
    return data or None


def evidence_dir(ws: str | os.PathLike[str]) -> Path:
    return folder(ws, 'evidence')
