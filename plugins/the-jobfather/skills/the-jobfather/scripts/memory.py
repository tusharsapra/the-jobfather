"""Explicit, journaled updates to the user's managed workspace files, with rollback.

Every change is a proposal the user asked for. The utility locks the workspace,
checks revisions, snapshots before/after, journals multi-file writes, appends one
event to learning/changes.jsonl and regenerates learning/CHANGELOG.md.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

# CLI execution and local.context_revision must share one module (and lock state).
if __name__ == '__main__':
    sys.modules['memory'] = sys.modules[__name__]

import layout
import managed
from local import (add_workspace_arg, atomic_json, atomic_text, config, contained, digest, locked, now,
                   platform, read_json, read_text, require_workspace, root_lock, sha_text, tz)

TYPES = ('factual-correction', 'preference', 'example', 'workflow', 'application-specific', 'market',
         'policy', 'form-answer', 'cv-render')
SYSTEM_TYPES = ('initialization', 'external-edit')
VERBS = ('update', 'correct', 'remember', 'persist', 'save', 'store', 'change', 'set', 'replace', 'add',
         'remove', 'forget', 'switch', 'enable', 'disable', 'allow', 'stop', 'use')
WITHHELD = '[withheld: contains sensitive values]'


def sha(text):
    return sha_text(text)


# ------------------------------------------------------------------ log
def events(ws, repair=False):
    path = contained(ws, layout.CHANGES)
    if not path.exists():
        return []
    data = path.read_bytes()
    if data and not data.endswith(b'\n'):
        if not repair:
            raise RuntimeError('Incomplete change-log tail; run recovery')
        end = data.rfind(b'\n') + 1
        fragment = data[end:]
        backup = contained(ws, layout.LOG_FRAGMENTS + '/' + hashlib.sha256(fragment).hexdigest() + '.bin')
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(fragment)
        with path.open('r+b') as stream:
            stream.truncate(end)
            stream.flush()
            os.fsync(stream.fileno())
        data = data[:end]
    result = [json.loads(line) for line in data.decode('utf-8').splitlines()]
    ids = [e['event_id'] for e in result]
    if len(ids) != len(set(ids)):
        raise RuntimeError('Duplicate event IDs in the change log')
    return result


def append_event(ws, event):
    found = next((e for e in events(ws, repair=True) if e['event_id'] == event['event_id']), None)
    if found:
        compare = lambda e: {k: v for k, v in e.items() if k not in ('status', 'recovered_at')}
        if compare(found) != compare(event):
            raise RuntimeError('Existing event ID has conflicting transaction content')
        return found
    path = contained(ws, layout.CHANGES)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('ab') as stream:
        stream.write((json.dumps(event, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8'))
        stream.flush()
        os.fsync(stream.fileno())
    return event


def regenerate(ws):
    revision = managed.raw_revision(ws)
    owners = {}
    for path, spec in managed.MANAGED.items():
        owners.setdefault(spec['owner'], []).append(path)
    atomic_json(contained(ws, layout.INDEX), {
        'source_revision': revision,
        'source_log_sha256': digest(contained(ws, layout.CHANGES)),
        'owners': owners,
        'dependencies': {p: s['depends_on'] for p, s in managed.MANAGED.items() if s['depends_on']},
        'groups': managed.GROUPS,
        'note': 'Hash index only; facts live in the managed files. Historical outputs are immutable.'})
    lines = ['# The Jobfather change history', '',
             'Generated from learning/changes.jsonl; edit nothing here by hand.', '']
    for e in events(ws):
        lines += ['## ' + e['timestamp_local'] + ' · ' + e['platform'] + ' · ' + e['status'], '',
                  e['summary']['after'], '', 'Before: ' + e['summary']['before'],
                  'Reason: ' + e['reason'], 'Files: ' + ', '.join(e['affected_paths']),
                  'Event: `' + e['event_id'] + '`; transaction: `' + e['transaction_id'] + '`.', '']
    atomic_text(contained(ws, layout.CHANGELOG), '\n'.join(lines))


# ------------------------------------------------------------------ journal
def _checkpoint(ws, point):
    # Crash injection exists only for isolated evaluation workspaces.
    if config(ws).get('evaluation_mode') and os.environ.get('JOBFATHER_TEST_CRASH') == point:
        os._exit(87)


def _write_target(ws, item):
    active = contained(ws, item['path'])
    if item['after_sha256'] == 'missing':
        if active.exists():
            active.unlink()
        return
    data = contained(ws, item['after_snapshot']).read_bytes()
    from local import atomic_bytes
    atomic_bytes(active, data)


def _hash_or_missing(path):
    return digest(path) if path.exists() else 'missing'


def _finish(ws, manifest_path, recovered=False):
    manifest = read_json(manifest_path)
    if manifest['phase'] == 'complete':
        return manifest['event']
    for item in manifest['targets']:
        for key, snapshot in (('before_sha256', 'before_snapshot'), ('after_sha256', 'after_snapshot')):
            if item[key] != 'missing' and _hash_or_missing(contained(ws, item[snapshot])) != item[key]:
                raise RuntimeError('Recovery snapshot integrity failed')
        actual = _hash_or_missing(contained(ws, item['path']))
        if actual not in (item['before_sha256'], item['after_sha256']):
            raise RuntimeError('Recovery refused: %s was changed outside the transaction' % item['path'])
    for i, item in enumerate(manifest['targets']):
        if _hash_or_missing(contained(ws, item['path'])) != item['after_sha256']:
            _write_target(ws, item)
        _checkpoint(ws, 'after-file-' + str(i + 1))
    _checkpoint(ws, 'before-log')
    event = manifest['event']
    if recovered and event['status'] == 'committed':
        event = dict(event, status='recovered', recovered_at=now())
    event = append_event(ws, event)
    _checkpoint(ws, 'after-log')
    regenerate(ws)
    manifest['event'] = event
    manifest['phase'] = 'complete'
    atomic_json(manifest_path, manifest)
    return event


def recover_locked(ws):
    directory = contained(ws, layout.TRANSACTIONS)
    recovered = []
    if directory.exists():
        for manifest in sorted(directory.glob('*/journal.json')):
            if read_json(manifest)['phase'] != 'complete':
                recovered.append(_finish(ws, manifest, recovered=True))
    if events(ws):
        index = contained(ws, layout.INDEX)
        stale = not index.exists()
        if not stale:
            value = read_json(index)
            stale = (value['source_revision'] != managed.raw_revision(ws) or
                     value.get('source_log_sha256') != digest(contained(ws, layout.CHANGES)))
        if stale or not contained(ws, layout.CHANGELOG).exists():
            regenerate(ws)
    return recovered


def _latest_snapshot(ws, path, wanted_sha):
    """Find a stored copy of `path` whose content hash is `wanted_sha`."""
    directory = contained(ws, layout.TRANSACTIONS)
    if not directory.exists():
        return None
    for journal in sorted(directory.glob('*/journal.json'), key=lambda p: p.stat().st_mtime, reverse=True):
        for side in ('after', 'before'):
            candidate = journal.parent / side / path
            if candidate.is_file() and digest(candidate) == wanted_sha:
                return candidate
    return None


def _record_external_edits(ws):
    """Log edits made outside The Jobfather, so history and rollback stay truthful."""
    log = events(ws)
    if not log:
        return None
    known = log[-1]['after_revision']['files']
    actual = managed.raw_revision(ws)['files']
    drifted = [p for p in sorted(managed.MANAGED) if known.get(p, 'missing') != actual.get(p, 'missing')]
    if not drifted:
        return None
    tx = 'EXT-' + hashlib.sha256(json.dumps([[p, known.get(p), actual.get(p)] for p in drifted]).encode()).hexdigest()[:24]
    directory = contained(ws, layout.TRANSACTIONS + '/' + tx)
    targets, warnings = [], []
    for p in drifted:
        before_sha, after_sha = known.get(p, 'missing'), actual.get(p, 'missing')
        before_rel = layout.TRANSACTIONS + '/' + tx + '/before/' + p
        after_rel = layout.TRANSACTIONS + '/' + tx + '/after/' + p
        if before_sha != 'missing':
            source = _latest_snapshot(ws, p, before_sha)
            if source is None:
                return None  # no trustworthy copy of the old content; leave history unchanged
            contained(ws, before_rel).parent.mkdir(parents=True, exist_ok=True)
            contained(ws, before_rel).write_bytes(source.read_bytes())
        if after_sha != 'missing':
            contained(ws, after_rel).parent.mkdir(parents=True, exist_ok=True)
            contained(ws, after_rel).write_bytes(contained(ws, p).read_bytes())
            try:
                old_text = read_text(contained(ws, before_rel)) if before_sha != 'missing' else None
                managed.validate_file(p, old_text, read_text(contained(ws, p)))
            except (ValueError, UnicodeDecodeError) as error:
                warnings.append('%s: %s' % (p, error))
        targets.append({'path': p, 'before_snapshot': before_rel, 'after_snapshot': after_rel,
                        'before_sha256': before_sha, 'after_sha256': after_sha})
    revision = managed.raw_revision(ws)
    proposal = {'transaction_id': tx, 'platform': 'unknown', 'scope': 'global', 'change_type': 'external-edit',
                'user_request': '', 'reason': 'Detected edits made outside The Jobfather (for example in an editor).',
                'summary': {'before': 'Last version saved by The Jobfather.',
                            'after': 'Edited outside The Jobfather: ' + ', '.join(drifted)}}
    event = _event(ws, proposal, log[-1]['after_revision'], revision, {}, status='observed',
                   hashes={t['path']: {'before': t['before_sha256'], 'after': t['after_sha256']} for t in targets})
    if warnings:
        event['validation_warnings'] = warnings
    journal = directory / 'journal.json'
    atomic_json(journal, {'phase': 'prepared', 'targets': targets, 'event': event,
                          'proposal_sha256': sha(json.dumps(proposal, sort_keys=True))})
    return _finish(ws, journal)


def current(ws=None):
    """Recovery barrier for every task: finish pending writes, log outside edits, return the revision."""
    ws = require_workspace(ws)
    with root_lock(ws):
        recover_locked(ws)
        _record_external_edits(ws)
        revision = managed.raw_revision(ws)
        managed.remember_revision(ws, revision)
        return revision


def read_context(names, ws=None):
    ws = require_workspace(ws)
    paths = [managed.resolve(n) for n in names]
    if not paths:
        raise ValueError('Select managed files to read')
    with root_lock(ws):
        recover_locked(ws)
        _record_external_edits(ws)
        revision = managed.raw_revision(ws)
        managed.remember_revision(ws, revision)
        return {'context_revision': revision,
                'content': {p: (read_text(contained(ws, p)) if contained(ws, p).is_file() else None) for p in paths}}


# ------------------------------------------------------------------ events
def _event(ws, proposal, before, after, changes, status='committed', hashes=None):
    clock = datetime.now(timezone.utc)
    zone, name, exact = tz(ws)
    result = {'schema_version': 2,
              'event_id': 'EV-' + uuid.uuid5(uuid.NAMESPACE_URL, proposal['transaction_id']).hex,
              'transaction_id': proposal['transaction_id'], 'timestamp_utc': clock.isoformat(),
              'timestamp_local': clock.astimezone(zone).isoformat(timespec='seconds'), 'timezone': name,
              'timezone_exact': exact, 'workspace_id': config(ws).get('workspace_id'),
              'platform': platform(proposal['platform']), 'change_type': proposal['change_type'],
              'scope': proposal['scope'], 'affected_paths': sorted(hashes or changes),
              'hashes': hashes or {p: {'before': sha(v[0]) if v[0] is not None else 'missing', 'after': sha(v[1])}
                                   for p, v in changes.items()},
              'before_revision': before, 'after_revision': after,
              'summary': proposal['summary'], 'reason': proposal['reason'],
              'user_feedback': proposal['user_request'], 'status': status,
              'accepted_output_ref': proposal.get('accepted_output_ref')}
    for key in ('surface', 'version', 'rollback_of', 'application_id', 'learning_ids'):
        if proposal.get(key):
            result[key] = proposal[key]
    return result


def initialize(ws=None, host='unknown', confirmation=None):
    """Register the onboarding-reviewed files as the first event (never invents history)."""
    ws = require_workspace(ws)
    platform(host)
    with root_lock(ws):
        recover_locked(ws)
        existing = events(ws)
        if existing:
            return existing[0]
        receipt_path = contained(ws, layout.ONBOARDING)
        receipt = read_json(receipt_path) if receipt_path.is_file() else {}
        revision = managed.raw_revision(ws)
        if receipt.get('phase') != 'ready-to-init':
            raise ValueError('Finish onboarding review first (onboarding receipt is not ready-to-init)')
        if receipt.get('files') != revision['files']:
            raise ValueError('Files changed after the onboarding review; review them again')
        texts = managed.read_all(ws)
        for path, text in texts.items():
            managed.validate_file(path, None, text)
        managed.validate_references(texts)
        tx = 'INIT-' + revision['revision'][:24]
        directory = contained(ws, layout.TRANSACTIONS + '/' + tx)
        targets = []
        for path in sorted(texts):
            snapshot = layout.TRANSACTIONS + '/' + tx + '/after/' + path
            contained(ws, snapshot).parent.mkdir(parents=True, exist_ok=True)
            contained(ws, snapshot).write_bytes(contained(ws, path).read_bytes())
            targets.append({'path': path, 'before_snapshot': None, 'after_snapshot': snapshot,
                            'before_sha256': 'missing', 'after_sha256': digest(contained(ws, path))})
        statement = (confirmation or receipt.get('confirmation') or '')[:4000]
        proposal = {'transaction_id': tx, 'platform': host, 'scope': 'global', 'change_type': 'initialization',
                    'user_request': statement, 'reason': 'Set up The Jobfather workspace.',
                    'summary': {'before': 'No workspace history.',
                                'after': 'Registered onboarding-reviewed context (%d files).' % len(texts)}}
        hashes = {t['path']: {'before': 'missing', 'after': t['after_sha256']} for t in targets}
        event = _event(ws, proposal, revision, revision, {}, hashes=hashes)
        journal = directory / 'journal.json'
        # INIT records existing files; nothing is rewritten, so targets stay out of the forward-write list.
        atomic_json(journal, {'phase': 'prepared', 'targets': [], 'snapshots': targets, 'event': event,
                              'proposal_sha256': sha(json.dumps(proposal, sort_keys=True))})
        result = _finish(ws, journal)
        receipt['phase'] = 'complete'
        receipt['initialized_at'] = now()
        atomic_json(receipt_path, receipt)
        return result


# ------------------------------------------------------------------ proposals
def _set_path(value, keys, new, create):
    target = value
    for key in keys[:-1]:
        if key not in target:
            if not create:
                raise ValueError('Unknown key path: ' + '/'.join(map(str, keys)))
            target[key] = {}
        target = target[key]
    if keys[-1] not in target and not create:
        raise ValueError('Unknown property: ' + '/'.join(map(str, keys)))
    target[keys[-1]] = new


def _edit(ws, path, old, operation):
    kinds = set(operation) & {'replace', 'append', 'json_set', 'json_add', 'write'}
    if len(kinds) != 1 or set(operation) != {'path', 'before_sha256'} | kinds:
        raise ValueError('Each change must be exactly one replace, append, json_set, json_add or write')
    kind = kinds.pop()
    spec = managed.MANAGED.get(path, {'type': 'record'})
    if kind == 'write':
        source = operation['write']
        if spec['type'] != 'generated' and not spec.get('rewritable'):
            raise ValueError('Whole-file writes are only for generated or summary files')
        if set(source) == {'from_render', 'sha256'}:
            if not source['from_render'].startswith(layout.RENDERS + '/'):
                raise ValueError('Generated files must come from a render directory')
            text = read_text(contained(ws, source['from_render']))
        elif set(source) == {'text', 'sha256'}:
            text = source['text']
        else:
            raise ValueError('write needs from_render or text, plus sha256')
        if sha(text) != source['sha256']:
            raise ValueError('write content does not match its sha256')
        return text
    if old is None:
        raise ValueError('%s does not exist; only generated/summary files can be created with write' % path)
    if kind == 'replace':
        pair = operation[kind]
        if set(pair) != {'old', 'new'} or not pair['old'] or old.count(pair['old']) != 1:
            raise ValueError('Replacement text must match exactly once')
        return old.replace(pair['old'], pair['new'], 1)
    if kind == 'append':
        if not isinstance(operation[kind], str) or not operation[kind].strip() or len(operation[kind]) > 12000:
            raise ValueError('Append needs a concise lesson or example')
        return old.rstrip() + '\n\n' + operation[kind].strip() + '\n'
    value = json.loads(old)
    for assignment in operation[kind]:
        if set(assignment) != {'keys', 'value'} or not assignment['keys']:
            raise ValueError('JSON assignment needs keys and value')
        keys = assignment['keys']
        if kind == 'json_add':
            if path == layout.HR and (len(keys) != 2 or keys[0] != 'custom'):
                raise ValueError('New HR facts are added as ["custom", "<name>"]')
            parent = value
            for key in keys[:-1]:
                parent = parent.setdefault(key, {})
            if keys[-1] in parent:
                raise ValueError('json_add cannot overwrite ' + '/'.join(keys))
            _set_path(value, keys, assignment['value'], create=True)
        else:
            _set_path(value, keys, assignment['value'], create=spec.get('policy') == 'open' or spec['type'] == 'record')
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def _authorizing(text, ws, scope):
    words = list(VERBS) + [w for w in config(ws).get('authorization_keywords', []) if isinstance(w, str) and w.strip()]
    if re.search(r'\b(%s)\b' % '|'.join(re.escape(w) for w in words), text, re.I):
        return True
    return scope == 'application' and re.search(r'\b(focus|emphasi[sz]e|for this)\b', text, re.I)


def _validate_proposal(proposal, ws):
    required = {'transaction_id', 'intent', 'user_request', 'platform', 'scope', 'change_type',
                'summary', 'reason', 'review', 'changes'}
    allowed = required | {'expected_revision', 'dependency_review', 'accepted_output_ref', 'surface', 'version',
                          'application_id', 'test_application', 'sensitive', 'learning_ids'}
    if not required <= set(proposal) or set(proposal) - allowed:
        raise ValueError('Missing or unrecognized proposal fields')
    if not re.fullmatch(r'TX-[A-Za-z0-9_-]{6,80}', proposal['transaction_id']):
        raise ValueError('Supply a stable unique TX- id so a retry is safe')
    if proposal['intent'] != 'explicit-update' or not proposal['user_request'].strip():
        raise ValueError('Saved changes need an explicit user request; acceptance alone is not enough')
    if not _authorizing(proposal['user_request'], ws, proposal['scope']):
        raise ValueError('The request does not ask to save or change anything')
    if proposal['scope'] not in ('global', 'application') or proposal['change_type'] not in TYPES:
        raise ValueError('Invalid scope or change type')
    platform(proposal['platform'])
    review = proposal['review']
    if any(review.get(k) is not True for k in ('user_authorized', 'facts_checked', 'scope_checked')):
        raise ValueError('Record explicit authorization, factual and scope review')
    if set(proposal['summary']) != {'before', 'after'} or not all(proposal['summary'].values()) or not proposal['reason'].strip():
        raise ValueError('Concise before/after summary and reason required')
    if not proposal['changes']:
        raise ValueError('No proposed changes')
    for key, limit in (('user_request', 4000), ('reason', 2000)):
        if not isinstance(proposal[key], str) or len(proposal[key]) > limit:
            raise ValueError('Store concise feedback and reason, not the whole chat')
    if any(not isinstance(v, str) or len(v) > 1000 for v in proposal['summary'].values()):
        raise ValueError('Before/after summaries must be short text')
    if proposal['change_type'] == 'policy' and any(c.get('path') != layout.POLICY for c in proposal['changes']):
        raise ValueError('Policy changes touch config/policy.json only')


def _sensitive_values(ws, changes):
    found = []
    for path, (old, new) in changes.items():
        if path != layout.HR:
            continue
        before, after = json.loads(old) if old else {}, json.loads(new)
        for key in managed.SENSITIVE_HR:
            if key in after and after.get(key) != before.get(key):
                value = after[key].get('value') if isinstance(after[key], dict) else after[key]
                if value not in (None, ''):
                    found.append(str(value))
        for key, item in after.get('custom', {}).items():
            if item.get('sensitive') and item != before.get('custom', {}).get(key):
                found.append(str(item.get('value')))
    return found


def _commit_locked(ws, proposal, changes, proposal_hash, status='committed'):
    before = managed.raw_revision(ws)
    files = dict(before['files'])
    for p, (_, new) in changes.items():
        if p in files:
            files[p] = sha(new)
    after = {'files': files, 'revision': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}
    event = _event(ws, proposal, before, after, changes, status)
    tx_dir = layout.TRANSACTIONS + '/' + proposal['transaction_id']
    targets = []
    for p, (old, new) in changes.items():
        before_rel = tx_dir + '/before/' + p
        after_rel = tx_dir + '/after/' + p
        if old is not None:
            atomic_text(contained(ws, before_rel), old)
        atomic_text(contained(ws, after_rel), new)
        targets.append({'path': p, 'before_snapshot': before_rel, 'after_snapshot': after_rel,
                        'before_sha256': sha(old) if old is not None else 'missing', 'after_sha256': sha(new)})
    journal = contained(ws, tx_dir + '/journal.json')
    atomic_json(journal, {'phase': 'prepared', 'targets': targets, 'event': event, 'proposal_sha256': proposal_hash})
    _checkpoint(ws, 'prepared')
    result = _finish(ws, journal)
    for p, (_, new) in changes.items():
        if digest(contained(ws, p)) != sha(new):
            raise RuntimeError('Commit integrity failed')
    managed.remember_revision(ws, managed.raw_revision(ws))
    return result


def _dependency_review(ws, proposal, paths, post_texts):
    dependents = {}
    for target, spec in managed.MANAGED.items():
        for dep in spec['depends_on']:
            dependents.setdefault(dep, set()).add(target)
    required = set()
    for p in paths:
        required |= dependents.get(p, set())
    review = proposal.get('dependency_review', {})
    for dependent in sorted(required):
        if not contained(ws, dependent).exists() and dependent not in paths:
            continue
        expected = 'updated' if dependent in paths else 'unchanged'
        if review.get(dependent) != expected:
            raise ValueError('Review dependent %s and record it as %s' % (dependent, expected))
        if expected == 'unchanged' and managed.MANAGED[dependent]['type'] == 'generated':
            try:
                import cv_init
            except ImportError:
                continue
            cv_init.verify_unchanged(ws, dependent, post_texts)


def apply(proposal, ws=None):
    ws = require_workspace(ws)
    _validate_proposal(proposal, ws)
    proposal_hash = sha(json.dumps(proposal, sort_keys=True))
    app_lock = contextlib.nullcontext()
    app_rel = None
    if proposal['scope'] == 'application':
        from applications import Store
        store = Store(ws, test=proposal.get('test_application', False))
        app_dir = store.directory(proposal['application_id'])
        app_rel = Path(app_dir.relative_to(ws)).as_posix() + '/record.json'
        app_lock = locked(app_dir)  # same lock order as application writers
    with app_lock, root_lock(ws):
        recover_locked(ws)
        _record_external_edits(ws)
        journal = contained(ws, layout.TRANSACTIONS + '/' + proposal['transaction_id'] + '/journal.json')
        if journal.exists():
            prior = read_json(journal)
            if prior['proposal_sha256'] != proposal_hash:
                raise ValueError('Transaction ID reused for a different proposal')
            if prior['event'] not in events(ws):
                raise RuntimeError('Committed journal disagrees with the change log')
            return prior['event']
        if not events(ws):
            raise ValueError('Finish onboarding first; the workspace has no history yet')
        current_revision = managed.raw_revision(ws)['revision']
        if proposal['scope'] == 'global' and proposal.get('expected_revision') != current_revision:
            raise ValueError('Stale revision; reread the files and rebase the change')
        paths = [c['path'] for c in proposal['changes']]
        if len(set(paths)) != len(paths):
            raise ValueError('Duplicate target paths')
        if proposal.get('accepted_output_ref'):
            from applications import Store
            ref = proposal['accepted_output_ref']
            result = Store(ws, test=ref.get('test', False)).output_path(ref['application_id'], ref['output_id'])
            if result['state'] != 'user-approved':
                raise ValueError('Referenced output must actually be approved by the user')
        changes = {}
        for op in proposal['changes']:
            p = op['path']
            if proposal['scope'] == 'global':
                p = managed.resolve(p)
            elif p != app_rel or 'json_set' not in op:
                raise ValueError('Application scope changes only that application\'s local_preferences')
            else:
                for a in op['json_set']:
                    if len(a['keys']) != 2 or a['keys'][0] != 'local_preferences':
                        raise ValueError('Application preferences cannot change global context or status')
            active = contained(ws, p)
            old = read_text(active) if active.exists() else None
            if (sha(old) if old is not None else 'missing') != op['before_sha256']:
                raise ValueError('Stale target hash: ' + p)
            new = _edit(ws, p, old, op)
            if proposal['scope'] == 'global':
                managed.validate_file(p, old, new)
            if old == new:
                raise ValueError('No-op change: ' + p)
            if proposal['scope'] == 'application':
                value = json.loads(new)
                value['interactions'].append({'type': 'local-preference', 'text': proposal['user_request'],
                                              'basis': 'user-reported', 'recorded_at': now(),
                                              'platform': proposal['platform'],
                                              'transaction_id': proposal['transaction_id']})
                new = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
            changes[p] = (old, new)
        if proposal['scope'] == 'global':
            post = managed.read_all(ws)
            post.update({p: v[1] for p, v in changes.items()})
            managed.validate_references(post)
            _dependency_review(ws, proposal, list(changes), post)
            secrets = _sensitive_values(ws, changes)
            if secrets or proposal.get('sensitive'):
                logged = ' '.join([proposal['summary']['before'], proposal['summary']['after'], proposal['reason']])
                if any(s and s in logged for s in secrets):
                    raise ValueError('Keep sensitive values (pay, notice, address) out of the summary and reason')
                proposal = dict(proposal, user_request=WITHHELD)
        return _commit_locked(ws, proposal, changes, proposal_hash)


def rollback(event_id, user_request, host, transaction_id, ws=None):
    if not re.search(r'\b(roll\s?back|undo|restore|revert)\b', user_request, re.I):
        raise ValueError('A rollback must be requested by the user')
    if not re.fullmatch(r'TX-[A-Za-z0-9_-]{6,80}', transaction_id):
        raise ValueError('Invalid transaction ID')
    platform(host)
    ws = require_workspace(ws)
    with root_lock(ws):
        recover_locked(ws)
        _record_external_edits(ws)
        original = next((e for e in events(ws) if e['event_id'] == event_id), None)
        if not original or original['change_type'] == 'initialization':
            raise ValueError('No reversible update with that ID')
    guard = contextlib.nullcontext()
    if original['scope'] == 'application':
        guard = locked(contained(ws, original['affected_paths'][0]).parent)
    with guard, root_lock(ws):
        recover_locked(ws)
        proposal = {'transaction_id': transaction_id, 'platform': host, 'scope': original['scope'],
                    'change_type': original['change_type'] if original['change_type'] in TYPES else 'factual-correction',
                    'user_request': user_request, 'reason': 'User-requested rollback of ' + event_id,
                    'rollback_of': event_id,
                    'summary': {'before': original['summary']['after'], 'after': original['summary']['before']}}
        if original.get('application_id'):
            proposal['application_id'] = original['application_id']
        proposal_hash = sha(json.dumps(proposal, sort_keys=True))
        prior = contained(ws, layout.TRANSACTIONS + '/' + transaction_id + '/journal.json')
        if prior.exists():
            value = read_json(prior)
            if value['proposal_sha256'] != proposal_hash:
                raise ValueError('Transaction ID reused')
            return value['event']
        manifest = read_json(contained(ws, layout.TRANSACTIONS + '/' + original['transaction_id'] + '/journal.json'))
        changes = {}
        for item in manifest['targets']:
            p = item['path']
            active = contained(ws, p)
            if original['scope'] == 'application':
                before = contained(ws, item['before_snapshot'])
                after = contained(ws, item['after_snapshot'])
                if digest(before) != item['before_sha256'] or digest(after) != item['after_sha256']:
                    raise ValueError('Rollback snapshot integrity failed')
                previous = read_json(before)['local_preferences']
                resulting = read_json(after)['local_preferences']
                old = read_text(active)
                value = json.loads(old)
                missing = object()
                keys = {k for k in set(previous) | set(resulting) if previous.get(k, missing) != resulting.get(k, missing)}
                for key in keys:
                    if value['local_preferences'].get(key, missing) != resulting.get(key, missing):
                        raise ValueError('A later application preference conflicts with the rollback')
                    if key in previous:
                        value['local_preferences'][key] = previous[key]
                    else:
                        value['local_preferences'].pop(key, None)
                value['interactions'].append({'type': 'local-preference', 'text': user_request, 'basis': 'user-reported',
                                              'recorded_at': now(), 'platform': host,
                                              'transaction_id': transaction_id, 'rollback_of': event_id})
                changes[p] = (old, json.dumps(value, ensure_ascii=False, indent=2) + '\n')
                continue
            if _hash_or_missing(active) != item['after_sha256']:
                raise ValueError('Later changes conflict with the rollback; make a targeted correction instead')
            if item['before_sha256'] == 'missing':
                raise ValueError('Cannot roll back the creation of ' + p)
            old = read_text(active) if active.exists() else None
            new = read_text(contained(ws, item['before_snapshot']))
            if sha(new) != item['before_sha256']:
                raise ValueError('Rollback snapshot integrity failed')
            managed.validate_file(p, old, new, rollback=True)
            changes[p] = (old, new)
        if original['scope'] == 'global':
            post = managed.read_all(ws)
            post.update({p: v[1] for p, v in changes.items()})
            managed.validate_references(post)
        return _commit_locked(ws, proposal, changes, proposal_hash, status='rolled-back')


def status(ws=None):
    ws = require_workspace(ws)
    with root_lock(ws):
        recovered = recover_locked(ws)
        observed = _record_external_edits(ws)
        value = events(ws)
        return {'workspace': str(ws), 'context_revision': managed.raw_revision(ws), 'event_count': len(value),
                'latest_event': value[-1] if value else None,
                'recovered_transactions': [e['transaction_id'] for e in recovered],
                'external_edits_logged': bool(observed), 'changelog': str(ws / layout.CHANGELOG)}


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('current', 'status', 'history', 'recover'):
        sub.add_parser(name)
    sub.add_parser('initialize').add_argument('--platform', required=True)
    sub.choices['initialize'].add_argument('--confirmation', help="the user's confirming words")
    sub.add_parser('read').add_argument('--files', nargs='+', required=True)
    sub.add_parser('apply').add_argument('--file', type=Path, required=True)
    child = sub.add_parser('rollback')
    child.add_argument('event_id')
    for name in ('request', 'platform', 'transaction-id'):
        child.add_argument('--' + name, required=True)
    args = parser.parse_args()
    ws = args.workspace
    if args.command == 'current':
        result = current(ws)
    elif args.command == 'read':
        result = read_context(args.files, ws)
    elif args.command in ('status', 'recover'):
        result = status(ws)
    elif args.command == 'history':
        status(ws)
        result = events(require_workspace(ws))
    elif args.command == 'initialize':
        result = initialize(ws, args.platform, args.confirmation)
    elif args.command == 'apply':
        result = apply(read_json(args.file), ws)
    else:
        result = rollback(args.event_id, args.request, args.platform, args.transaction_id, ws)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        raise SystemExit('Workspace update failed: ' + str(error))
