"""Per-item approvals, byte-compatible with the skill's ``scripts/approvals.py``.

Format ``the-jobfather/approval@1``: one record per approval at
``<ws>/.state/approvals/<approval_id>.json`` (``json.dumps(indent=2, ensure_ascii=False)`` plus a
newline), an append-only ``ledger.jsonl`` of canonical JSON lines, and an HMAC-SHA256 over the
canonical record (without ``mac``) keyed by ``<ws>/.state/keys/approvals.key``. The skill creates
an approval from the user's own words; a server verifies it before an outward action and marks it
consumed afterwards. Any change made here is readable by the skill and the other way round.

The HMAC makes tampering evident; it is not authentication. High-stakes actions still need a
human-only step (a host permission prompt or the user's own final click).
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import JFError

SCHEMA = 'the-jobfather/approval@1'
KINDS = {'email_send': 'send-with-approval', 'ats_submit': 'submit-with-approval',
         'naukri_apply': 'submit-with-approval', 'linkedin_message': 'send-with-approval'}
MODE_RANK = {'drafts-only': 0, 'send-with-approval': 1, 'submit-with-approval': 2}
STATUSES = ('pending', 'confirmed', 'consumed', 'expired', 'revoked', 'cancelled')
APPROVALS = '.state/approvals'
KEY_FILE = '.state/keys/approvals.key'
LOCK_FILE = '.state/locks/approvals.lock'
POLICY = 'config/policy.json'
ASK_AGAIN = 'Show the user this exact item again and record their approval.'


class ApprovalError(JFError, ValueError):
    """An approval problem; ``code`` is one of the approval_* error codes."""

    def __init__(self, code: str, message: str, user_action: str | None = ASK_AGAIN) -> None:
        super().__init__(code, message, False, user_action=user_action)


# ------------------------------------------------------------------ shared format
def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(canonical(payload).encode('utf-8')).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _contained(ws: str | os.PathLike[str], relative: str) -> Path:
    """Join a relative POSIX path under ws, refusing traversal and symlinks (as local.contained)."""
    root = Path(ws).resolve()
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts:
        raise ApprovalError('approval_invalid', 'Invalid approval path')
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ApprovalError('approval_invalid', 'Symlinked approval paths are not accepted')
    path = root / rel
    if path.resolve() != root and root not in path.resolve().parents:
        raise ApprovalError('approval_invalid', 'Approval path escapes the workspace')
    return path


def _path(ws: str | os.PathLike[str], approval_id: str) -> Path:
    if (not isinstance(approval_id, str) or not approval_id.startswith('apv_') or not approval_id.isascii()
            or not approval_id.replace('_', '').isalnum()):
        raise ApprovalError('approval_invalid', 'Invalid approval id')
    return _contained(ws, APPROVALS + '/' + approval_id + '.json')


def _replace(source: str, target: Path, attempts: int = 6) -> None:
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != 'nt' or attempt == attempts - 1:
                raise
            time.sleep(0.05)


def _atomic_bytes(path: Path, data: bytes) -> None:
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


def dump_record(record: dict[str, Any]) -> bytes:
    """The exact bytes the skill writes for a record (local.atomic_json)."""
    return (json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _key(ws: str | os.PathLike[str], create: bool) -> bytes:
    path = Path(ws) / KEY_FILE
    if not path.is_file():
        if not create:
            raise ApprovalError('approval_invalid', 'The approval key is missing, so no approval can be trusted')
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # another process created it first
        else:
            with os.fdopen(fd, 'wb') as fh:
                fh.write(secrets.token_bytes(32))
    for _ in range(40):  # a racing creator may not have written its bytes yet
        key = path.read_bytes()
        if key:
            return key
        time.sleep(0.025)
    raise ApprovalError('approval_invalid', 'The approval key is empty')


def _mac(ws: str | os.PathLike[str], record: dict[str, Any], create_key: bool = False) -> str:
    body = {k: v for k, v in record.items() if k != 'mac'}
    return hmac.new(_key(ws, create_key), canonical(body).encode('utf-8'), hashlib.sha256).hexdigest()


def _append_ledger(ws: str | os.PathLike[str], entry: dict[str, Any]) -> None:
    path = _contained(ws, APPROVALS + '/ledger.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('ab') as fh:
        fh.write((canonical(entry) + '\n').encode('utf-8'))
        fh.flush()
        os.fsync(fh.fileno())


def ledger(ws: str | os.PathLike[str]) -> list[dict[str, Any]]:
    path = _contained(ws, APPROVALS + '/ledger.jsonl')
    if not path.is_file():
        return []
    entries = []
    for line in path.read_bytes().decode('utf-8').splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            # Fail closed: a damaged ledger could hide a "consumed" event.
            raise ApprovalError('approval_invalid', 'The approval ledger is damaged; ask the user to approve again',
                                user_action='Tell the user the approval ledger needs repair.') from None
    return entries


def policy_mode(ws: str | os.PathLike[str]) -> str:
    path = Path(ws) / POLICY
    if not path.is_file():
        return 'drafts-only'
    return json.loads(path.read_bytes().decode('utf-8')).get('mode', 'drafts-only')


# ------------------------------------------------------------------ operations
def create(ws: str | os.PathLike[str], kind: str, subject: dict[str, Any], payload: Any, user_statement: str,
           created_by: str, ttl_minutes: int = 15) -> dict[str, Any]:
    """Record the user's approval of one exact payload (same rules as the skill)."""
    if kind not in KINDS:
        raise ApprovalError('approval_invalid', 'Unknown approval kind: %s' % kind, user_action=None)
    mode = policy_mode(ws)
    if MODE_RANK.get(mode, 0) < MODE_RANK[KINDS[kind]]:
        raise ApprovalError('approval_required', 'Policy is %s; %s needs %s (changed only by a logged policy update)'
                            % (mode, kind, KINDS[kind]), user_action='Ask the user whether to change the policy mode.')
    if not isinstance(user_statement, str) or len(user_statement.strip()) < 2:
        raise ApprovalError('approval_required', "Record the user's own words approving this exact item")
    if not isinstance(subject, dict) or not subject:
        raise ApprovalError('approval_invalid', 'Describe what is being approved (subject)', user_action=None)
    if not 1 <= int(ttl_minutes) <= 60:
        raise ApprovalError('approval_invalid', 'Approvals expire within 1-60 minutes', user_action=None)
    clock = _now()
    record = {'schema': SCHEMA, 'approval_id': 'apv_%s_%s' % (clock.strftime('%Y%m%dT%H%M%SZ'), secrets.token_hex(4)),
              'kind': kind, 'subject': subject, 'payload_sha256': payload_hash(payload),
              'user_statement': user_statement.strip()[:2000], 'created_by': created_by,
              'created_at': clock.isoformat(), 'expires_at': (clock + timedelta(minutes=int(ttl_minutes))).isoformat(),
              'nonce': secrets.token_hex(16), 'status': 'pending', 'confirmation': None, 'result': None}
    record['mac'] = _mac(ws, record, create_key=True)
    _atomic_bytes(_path(ws, record['approval_id']), dump_record(record))
    _append_ledger(ws, {'at': clock.isoformat(), 'approval_id': record['approval_id'], 'event': 'created',
                        'kind': kind})
    return record


def load(ws: str | os.PathLike[str], approval_id: str) -> dict[str, Any]:
    """Read one record and check its MAC."""
    path = _path(ws, approval_id)
    if not path.is_file():
        raise ApprovalError('approval_invalid', 'No approval with id %s' % approval_id)
    try:
        record = json.loads(path.read_bytes().decode('utf-8'))
    except ValueError:
        raise ApprovalError('approval_invalid', 'Approval record is damaged') from None
    if not isinstance(record, dict) or record.get('schema') != SCHEMA:
        raise ApprovalError('approval_invalid', 'Not a %s record' % SCHEMA)
    if not hmac.compare_digest(str(record.get('mac', '')), _mac(ws, record)):
        raise ApprovalError('approval_invalid', 'Approval record was modified')
    return record


def verify(ws: str | os.PathLike[str], approval_id: str | None, kind: str, payload: Any,
           allowed: tuple[str, ...] = ('pending',)) -> dict[str, Any]:
    """Check an approval before acting: kind, exact payload, expiry, status and single use."""
    if not approval_id:
        raise ApprovalError('approval_required', 'This action needs the user\'s approval of this exact item')
    record = load(ws, approval_id)
    if record['kind'] != kind:
        raise ApprovalError('approval_mismatch', 'Approval is for %s, not %s' % (record['kind'], kind))
    if record['payload_sha256'] != payload_hash(payload):
        raise ApprovalError('approval_mismatch', 'Approval does not match this exact payload; ask the user again')
    if _now() >= datetime.fromisoformat(record['expires_at']):
        raise ApprovalError('approval_expired', 'Approval expired; ask the user again')
    if record['status'] not in allowed:
        code = {'consumed': 'approval_consumed', 'expired': 'approval_expired'}.get(record['status'], 'approval_invalid')
        raise ApprovalError(code, 'Approval is %s' % record['status'])
    if any(e.get('approval_id') == approval_id and e.get('event') == 'consumed' for e in ledger(ws)):
        raise ApprovalError('approval_consumed', 'Approval already used')
    return record


def mark(ws: str | os.PathLike[str], approval_id: str, status: str, detail: Any = None) -> dict[str, Any]:
    """Move a record to a new status, re-sign it and append the event to the ledger."""
    if status not in STATUSES:
        raise ApprovalError('approval_invalid', 'Unknown approval status', user_action=None)
    record = load(ws, approval_id)
    if record['status'] == 'consumed':
        raise ApprovalError('approval_consumed', 'Approval already used')
    record['status'] = status
    if status == 'confirmed':
        record['confirmation'] = detail
    elif status in ('consumed', 'cancelled', 'expired', 'revoked'):
        record['result'] = detail
    record['mac'] = _mac(ws, record)
    _atomic_bytes(_path(ws, approval_id), dump_record(record))
    _append_ledger(ws, {'at': _now().isoformat(), 'approval_id': approval_id, 'event': status})
    return record


@contextlib.contextmanager
def _exclusive(ws: str | os.PathLike[str], timeout: float = 10.0) -> Iterator[None]:
    path = _contained(ws, LOCK_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
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
                    raise JFError('timeout', 'The approval store is busy', True, retry_after_s=2) from None
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


def consume(ws: str | os.PathLike[str], approval_id: str | None, kind: str, payload: Any, detail: Any = None,
            allowed: tuple[str, ...] = ('pending', 'confirmed')) -> dict[str, Any]:
    """Verify and mark consumed under one lock, so two calls can never use the same approval."""
    with _exclusive(ws):
        verify(ws, approval_id, kind, payload, allowed=allowed)
        return mark(ws, str(approval_id), 'consumed', detail)
