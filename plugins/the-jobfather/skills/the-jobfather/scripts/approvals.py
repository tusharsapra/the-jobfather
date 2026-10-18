"""Per-item approvals for outward actions (submit an application, send an email, message someone).

An approval record binds one exact payload (by hash) to the user's own words, expires
quickly and can be used once. Records live in <workspace>/.state/approvals/ with an
append-only ledger. The HMAC makes tampering evident; it is not authentication, so
high-stakes actions also need a human-only step (a host permission prompt, a confirm
page, or the user clicking Submit themselves).

Format: the-jobfather/approval@1 (shared with the jobfather-mcp package).
"""
import argparse
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import sys

import layout
import time

from local import add_workspace_arg, atomic_json, contained, read_json, read_text, require_workspace, shared_lock

SCHEMA = 'the-jobfather/approval@1'
KINDS = {'email_send': 'send-with-approval', 'ats_submit': 'submit-with-approval',
         'naukri_apply': 'submit-with-approval', 'linkedin_message': 'send-with-approval'}
MODE_RANK = {'drafts-only': 0, 'send-with-approval': 1, 'submit-with-approval': 2}
STATUSES = ('pending', 'confirmed', 'consumed', 'expired', 'revoked', 'cancelled')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def payload_hash(payload):
    return hashlib.sha256(canonical(payload).encode('utf-8')).hexdigest()


def _key(ws, create=False):
    """The HMAC key. Only creating an approval may create it; reads never do."""
    path = Path(ws) / layout.STATE / 'keys' / 'approvals.key'
    if not path.is_file():
        if not create:
            raise ValueError('No approvals have been created in this workspace')
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # another process created it first
        else:
            with os.fdopen(fd, 'wb') as fh:
                fh.write(secrets.token_bytes(32))
    for _ in range(100):  # a concurrent creator may not have written the bytes yet
        data = path.read_bytes()
        if len(data) == 32:
            return data
        time.sleep(0.01)
    raise ValueError('Approval key is damaged')


def _mac(ws, record, create=False):
    body = {k: v for k, v in record.items() if k != 'mac'}
    return hmac.new(_key(ws, create), canonical(body).encode('utf-8'), hashlib.sha256).hexdigest()


def _lock(ws):
    return shared_lock(Path(ws) / layout.LOCKS / 'approvals.lock')


def _path(ws, approval_id):
    if not approval_id.startswith('apv_') or not approval_id.replace('_', '').isalnum():
        raise ValueError('Invalid approval id')
    return contained(ws, layout.APPROVALS + '/' + approval_id + '.json')


def _ledger(ws, entry):
    path = contained(ws, layout.APPROVALS + '/ledger.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('ab') as fh:
        fh.write((canonical(entry) + '\n').encode('utf-8'))
        fh.flush()
        os.fsync(fh.fileno())


def ledger(ws):
    path = contained(ws, layout.APPROVALS + '/ledger.jsonl')
    if not path.is_file():
        return []
    return [json.loads(line) for line in read_text(path).splitlines() if line.strip()]


def policy_mode(ws):
    path = Path(ws) / layout.POLICY
    return read_json(path).get('mode', 'drafts-only') if path.is_file() else 'drafts-only'


def _now():
    return datetime.now(timezone.utc)


def create(ws, kind, subject, payload, user_statement, created_by, ttl_minutes=15):
    """Record the user's approval of one exact payload. Refuses when the policy mode is too low."""
    if kind not in KINDS:
        raise ValueError('Unknown approval kind: ' + kind)
    mode = policy_mode(ws)
    if MODE_RANK[mode] < MODE_RANK[KINDS[kind]]:
        raise ValueError('Policy is %s; %s needs %s (changed only by a logged policy update)' % (mode, kind, KINDS[kind]))
    if not isinstance(user_statement, str) or len(user_statement.strip()) < 2:
        raise ValueError('Record the user\'s own words approving this exact item')
    if not isinstance(subject, dict) or not subject:
        raise ValueError('Describe what is being approved (subject)')
    if not 1 <= int(ttl_minutes) <= 60:
        raise ValueError('Approvals expire within 1-60 minutes')
    clock = _now()
    record = {'schema': SCHEMA, 'approval_id': 'apv_%s_%s' % (clock.strftime('%Y%m%dT%H%M%SZ'), secrets.token_hex(4)),
              'kind': kind, 'subject': subject, 'payload_sha256': payload_hash(payload),
              'user_statement': user_statement.strip()[:2000], 'created_by': created_by,
              'created_at': clock.isoformat(), 'expires_at': (clock + timedelta(minutes=int(ttl_minutes))).isoformat(),
              'nonce': secrets.token_hex(16), 'status': 'pending', 'confirmation': None, 'result': None}
    record['mac'] = _mac(ws, record, create=True)
    atomic_json(_path(ws, record['approval_id']), record)
    _ledger(ws, {'at': clock.isoformat(), 'approval_id': record['approval_id'], 'event': 'created', 'kind': kind})
    return record


def load(ws, approval_id):
    record = read_json(_path(ws, approval_id))
    if not hmac.compare_digest(record.get('mac', ''), _mac(ws, record)):
        raise ValueError('Approval record was modified')
    return record


def verify(ws, approval_id, kind, payload, allowed=('pending',)):
    """Check an approval before acting: kind, payload, expiry, status and single use."""
    record = load(ws, approval_id)
    if record['kind'] != kind:
        raise ValueError('Approval is for %s, not %s' % (record['kind'], kind))
    if record['payload_sha256'] != payload_hash(payload):
        raise ValueError('Approval does not match this exact payload; ask the user again')
    if _now() >= datetime.fromisoformat(record['expires_at']):
        raise ValueError('Approval expired; ask the user again')
    if record['status'] not in allowed:
        raise ValueError('Approval is %s' % record['status'])
    if any(e['approval_id'] == approval_id and e['event'] == 'consumed' for e in ledger(ws)):
        raise ValueError('Approval already used')
    return record


def _mark_locked(ws, approval_id, status, detail):
    record = load(ws, approval_id)
    if record['status'] == 'consumed':
        raise ValueError('Approval already used')
    record['status'] = status
    if status == 'confirmed':
        record['confirmation'] = detail
    elif status in ('consumed', 'cancelled', 'expired', 'revoked'):
        record['result'] = detail
    record['mac'] = _mac(ws, record)
    atomic_json(_path(ws, approval_id), record)
    _ledger(ws, {'at': _now().isoformat(), 'approval_id': approval_id, 'event': status})
    return record


def mark(ws, approval_id, status, detail=None):
    if status not in STATUSES:
        raise ValueError('Unknown approval status')
    with _lock(ws):
        return _mark_locked(ws, approval_id, status, detail)


def consume(ws, approval_id, kind, payload, detail=None, allowed=('pending', 'confirmed')):
    """Verify and mark used in one locked step, so one approval can never act twice."""
    with _lock(ws):
        verify(ws, approval_id, kind, payload, allowed)
        return _mark_locked(ws, approval_id, 'consumed', detail)


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    sub = parser.add_subparsers(dest='command', required=True)
    child = sub.add_parser('create')
    child.add_argument('--kind', choices=sorted(KINDS), required=True)
    child.add_argument('--subject-file', type=Path, required=True)
    child.add_argument('--payload-file', type=Path, required=True)
    child.add_argument('--statement', required=True, help="the user's own words approving this item")
    child.add_argument('--platform', required=True)
    child.add_argument('--ttl-minutes', type=int, default=15)
    child = sub.add_parser('show')
    child.add_argument('approval_id')
    child = sub.add_parser('cancel')
    child.add_argument('approval_id')
    sub.add_parser('mode')
    args = parser.parse_args()
    ws = require_workspace(args.workspace)
    if args.command == 'create':
        result = create(ws, args.kind, json.loads(read_text(args.subject_file)), json.loads(read_text(args.payload_file)),
                        args.statement, args.platform, args.ttl_minutes)
    elif args.command == 'show':
        result = load(ws, args.approval_id)
    elif args.command == 'cancel':
        result = mark(ws, args.approval_id, 'cancelled', {'by': 'user'})
    else:
        result = {'mode': policy_mode(ws)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit('Approval failed: ' + str(error))
