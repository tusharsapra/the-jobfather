"""Pending-learning queue, end-of-task sweep, outcome stats and LEARNED.md compaction.

    learn.py queue --kind KIND --text TEXT --source SOURCE [--scope global|application]
                   [--application-id APP-...] [--target FILE] [--evidence TEXT] [--sensitive]
    learn.py list [--pending]
    learn.py resolve --event EV-... [--ids LRN-... ...]
    learn.py dismiss --ids LRN-... --reason TEXT [--forget]
    learn.py sweep [--json]
    learn.py stats [--days 30]
    learn.py compact [--request "<the user's words>"] [--platform HOST] [--exclude EV-... ...]

What the user confirms mid-task (a degree title, pay for a city, a tool they never used, an
exclusion, a form answer, a voice edit) is queued in learning/inbox.jsonl: one JSON line per item,
new items appended, a resolved item's line updated in place. Saving still goes through memory.py:
the proposal lists the items' learning_ids and `resolve` checks the logged event before marking
them saved. Nothing here edits a profile file; `compact` only builds a proposal.
"""
import argparse
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
import uuid

if __name__ == '__main__':
    sys.modules['learn'] = sys.modules[__name__]

import layout
import managed
import market
import memory
from local import (add_workspace_arg, atomic_json, atomic_text, contained, digest, now, platform, read_json,
                   read_text, require_workspace, sha_text, shared_lock)

KINDS = ('fact', 'preference', 'exclusion', 'form-answer', 'market', 'voice', 'policy')
SOURCES = ('chat', 'draft-edit', 'outcome', 'recruiter', 'research')
STATUSES = ('pending', 'saved', 'dismissed')
ITEM_ID = re.compile(r'^LRN-[0-9a-f]{12}$')
EVENT_ID = re.compile(r'^EV-[0-9a-f]{32}$')
APP_ID = re.compile(r'^APP-[0-9a-f]{12}$')
DEFAULT_TARGETS = {'preference': layout.LEARNED, 'exclusion': layout.MARKET_JSON, 'form-answer': layout.FORM_ANSWERS,
                   'market': layout.MARKET_JSON, 'voice': layout.VOICE, 'policy': layout.POLICY}
LOCK = layout.LOCKS + '/learning-inbox.lock'
PROPOSALS = getattr(layout, 'PROPOSALS', layout.STATE + '/proposals')
SAVED_STATUSES = ('committed', 'recovered')
STALE_DAYS = 14
FORGOTTEN = '[forgotten at the user\'s request]'

CHECKLIST = (
    ('fact', 'Did the user confirm or correct a fact: a date, title, degree name, number, or a tool they have '
             'or have never used?'),
    ('exclusion', 'Did they rule something out: a title, company, sector, location, shift, seniority or '
                  'contract type?'),
    ('form-answer', 'Did they answer a recurring form or recruiter question: notice, pay for a location, '
                    'authorization, availability, "how did you hear about us"?'),
    ('voice', 'Did they edit a draft in a way that should become a default: greeting, sign-off, length, words '
              'added or removed?'),
    ('market', 'Did research or replies show a market signal: titles employers use, a platform quirk, a pay '
               'band, how recruiters respond?'),
    ('preference', 'Did they say how they want things done: what to skip, how many jobs to show, when to '
                   'follow up?'),
    ('policy', 'Did they ask for more or less autonomy (sending, submitting)? That is a logged policy change.'),
)


# ------------------------------------------------------------------ inbox store
def _entries(ws):
    """[(raw_line, item_or_None)] in file order; unreadable lines are kept verbatim, never dropped."""
    path = contained(ws, layout.INBOX)
    if not path.is_file():
        return []
    out = []
    for raw in read_text(path).splitlines():
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        ok = isinstance(value, dict) and ITEM_ID.match(str(value.get('id', ''))) is not None
        out.append((raw, value if ok else None))
    return out


def _write(ws, entries):
    """Rewrite atomically, so a reader never sees a half-written line."""
    lines = [raw if item is None else json.dumps(item, ensure_ascii=False, sort_keys=True) for raw, item in entries]
    atomic_text(contained(ws, layout.INBOX), ''.join(line + '\n' for line in lines))


def _lock(ws):
    return shared_lock(contained(ws, LOCK))


def items(ws=None, pending_only=False):
    ws = require_workspace(ws)
    found = [item for _, item in _entries(ws) if item is not None]
    return [i for i in found if i.get('status') == 'pending'] if pending_only else found


def pending_count(ws=None):
    return len(items(ws, pending_only=True))


def _text(value, what, limit):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('%s is required' % what)
    if len(value) > limit:
        raise ValueError('%s must be under %d characters; queue the short reusable lesson' % (what, limit))
    return value.strip()


def queue(ws, kind, text, source='chat', scope='global', application_id=None, target=None, evidence=None,
          sensitive=False, test=False):
    """Append one pending item (an identical pending item is returned instead of a duplicate)."""
    ws = require_workspace(ws)
    if kind not in KINDS:
        raise ValueError('Kind must be one of: ' + ', '.join(KINDS))
    if source not in SOURCES:
        raise ValueError('Source must be one of: ' + ', '.join(SOURCES))
    if scope not in ('global', 'application'):
        raise ValueError('Scope is global or application')
    text = _text(text, 'Text', 2000)
    if scope == 'application':
        if not APP_ID.match(application_id or ''):
            raise ValueError('Application scope needs --application-id APP-...')
        record = contained(ws, layout.sandboxed(layout.APPLICATIONS, test) + '/' + application_id + '/record.json')
        if not record.is_file():
            raise ValueError('Unknown application ' + application_id)
    elif application_id:
        raise ValueError('--application-id needs --scope application')
    hint = managed.resolve(target) if target else (DEFAULT_TARGETS.get(kind) if scope == 'global' else None)
    item = OrderedDict([('id', 'LRN-' + uuid.uuid4().hex[:12]), ('at', now()), ('kind', kind), ('text', text),
                        ('source', source), ('scope', scope), ('status', 'pending')])
    if application_id:
        item['application_id'] = application_id
    if hint:
        item['target_hint'] = hint
    if evidence:
        item['evidence'] = _text(evidence, 'Evidence', 1000)
    if sensitive:
        item['sensitive'] = True
    norm = ' '.join(text.split()).casefold()
    with _lock(ws):
        entries = _entries(ws)
        for _, old in entries:
            if (old and old.get('status') == 'pending' and old.get('kind') == kind and old.get('scope') == scope and
                    old.get('application_id') == application_id and ' '.join(old.get('text', '').split()).casefold() == norm):
                return dict(old, duplicate=True)
        entries.append((None, item))
        _write(ws, entries)
    return dict(item)


def _event_index(ws):
    memory.current(ws)  # recovery barrier: finish interrupted writes before reading the log
    return memory.events(ws)


def resolve(ws, event_id, ids=None):
    """Mark items saved after memory.py logged an event whose learning_ids include them."""
    ws = require_workspace(ws)
    if not EVENT_ID.match(event_id or ''):
        raise ValueError('Expected an event id like EV-<32 hex>')
    event = next((e for e in _event_index(ws) if e['event_id'] == event_id), None)
    if event is None:
        raise ValueError('No logged event %s; apply the proposal with memory.py first' % event_id)
    if event.get('status') not in SAVED_STATUSES:
        raise ValueError('Event %s is %s, not a saved change' % (event_id, event.get('status')))
    listed = list(event.get('learning_ids') or [])
    wanted = list(OrderedDict.fromkeys(ids)) if ids else listed
    if not wanted:
        raise ValueError('Event %s lists no learning_ids; include them in the proposal' % event_id)
    missing = [i for i in wanted if i not in listed]
    if missing:
        raise ValueError('Event %s does not list %s in learning_ids' % (event_id, ', '.join(missing)))
    saved, already, unknown = [], [], []
    with _lock(ws):
        entries = _entries(ws)
        by_id = {item['id']: item for _, item in entries if item}
        unknown = [i for i in wanted if i not in by_id]
        if unknown and ids:
            raise ValueError('Unknown learning items: ' + ', '.join(unknown))
        for i in wanted:
            item = by_id.get(i)
            if item is None:
                continue
            if item.get('status') == 'saved':
                already.append(i)
                continue
            item.pop('reason', None)
            item.update(status='saved', resolved_at=now(), event_id=event_id)
            saved.append(i)
        if saved:
            _write(ws, entries)
    return {'event_id': event_id, 'saved': saved, 'already_saved': already, 'not_in_inbox': unknown}


def dismiss(ws, ids, reason, forget=False):
    """Close items the user declined; --forget also blanks their text (and evidence) in the inbox."""
    ws = require_workspace(ws)
    reason = _text(reason, 'Reason', 500)
    if not ids:
        raise ValueError('Name the items to dismiss')
    with _lock(ws):
        entries = _entries(ws)
        by_id = {item['id']: item for _, item in entries if item}
        unknown = [i for i in ids if i not in by_id]
        if unknown:
            raise ValueError('Unknown learning items: ' + ', '.join(unknown))
        for i in ids:
            if by_id[i].get('status') == 'saved':
                raise ValueError('%s was saved by %s; undo that with memory.py rollback instead'
                                 % (i, by_id[i].get('event_id')))
        for i in ids:
            item = by_id[i]
            if item.get('status') != 'dismissed':
                item.update(status='dismissed', resolved_at=now(), reason=reason)
            if forget:
                item['text'] = FORGOTTEN
                item.pop('evidence', None)
                item['forgotten'] = True
        _write(ws, entries)
    return {'dismissed': list(ids), 'forgotten': bool(forget)}


def reconcile(ws):
    """Mark pending items saved when a logged event already lists them (the host forgot to resolve)."""
    saved_by = {}
    for event in _event_index(ws):
        if event.get('status') in SAVED_STATUSES:
            for item_id in event.get('learning_ids') or []:
                saved_by.setdefault(item_id, event['event_id'])
    if not saved_by:
        return []
    changed = []
    with _lock(ws):
        entries = _entries(ws)
        for _, item in entries:
            if item and item.get('status') == 'pending' and item['id'] in saved_by:
                item.update(status='saved', resolved_at=now(), event_id=saved_by[item['id']])
                changed.append({'id': item['id'], 'event_id': saved_by[item['id']]})
        if changed:
            _write(ws, entries)
    return changed


def _age_days(item):
    when = _when(item.get('at'))
    return (datetime.now(timezone.utc) - when).days if when else None


def listing(ws=None, pending_only=False):
    ws = require_workspace(ws)
    found = items(ws, pending_only)
    try:
        log = memory.events(ws)
    except RuntimeError:
        log = None  # an interrupted write; memory.py current repairs it
    undone = _rolled_back(log) if log is not None else set()
    out = []
    for item in found:
        row = dict(item)
        if item.get('event_id') and log is not None:
            row['event_active'] = item['event_id'] not in undone
        out.append(row)
    bad = sum(1 for _, item in _entries(ws) if item is None)
    return {'items': out, 'counts': dict(Counter(i.get('status') for i in items(ws))),
            'unreadable_lines': bad}


def sweep(ws=None):
    ws = require_workspace(ws)
    reconciled = reconcile(ws)
    pending = []
    for item in items(ws, pending_only=True):
        age = _age_days(item)
        pending.append({'id': item['id'], 'kind': item['kind'], 'text': item['text'],
                        'target': item.get('target_hint') or (item.get('application_id') and
                                                              'application ' + item['application_id']),
                        'scope': item['scope'], 'age_days': age, 'stale': age is not None and age >= STALE_DAYS,
                        'sensitive': bool(item.get('sensitive'))})
    return {'checklist': [{'kind': k, 'question': q} for k, q in CHECKLIST], 'pending': pending,
            'reconciled': reconciled}


def sweep_text(result):
    lines = ['End-of-task learning sweep. Check only what came up in this task:']
    lines += ['%d. [%s] %s' % (n, item['kind'], item['question']) for n, item in enumerate(result['checklist'], 1)]
    lines.append('Queue each new one: learn.py queue --kind KIND --text "..." --source chat|draft-edit|outcome|'
                 'recruiter|research')
    for item in result['reconciled']:
        lines.append('Already saved by %s (marked saved now): %s' % (item['event_id'], item['id']))
    if not result['pending']:
        lines.append('Pending: none.')
        return '\n'.join(lines) + '\n'
    lines.append('Pending (%d):' % len(result['pending']))
    for item in result['pending']:
        target = ' -> ' + item['target'] if item['target'] else ''
        flags = (' [sensitive: keep values out of summaries]' if item['sensitive'] else '') + \
            (' [waiting %d days]' % item['age_days'] if item['stale'] else '')
        lines.append('- %s [%s%s] %s%s' % (item['id'], item['kind'], target, item['text'], flags))
    count = len(result['pending'])
    ask = 'Save this for next time?' if count == 1 else 'Save these %d things for next time?' % count
    lines.append('Next: ask once, in plain words, "%s". On yes: one memory.py proposal per owning file with '
                 '"learning_ids": [...], apply it, then learn.py resolve --event EV-... On no: learn.py dismiss '
                 '--ids ... --reason "...".' % ask)
    return '\n'.join(lines) + '\n'


# ------------------------------------------------------------------ outcome stats
REPLY_STATUSES = {'interview', 'rejected', 'offer'}
SENT_STATUSES = {'submitted', 'interview', 'rejected', 'offer'}
REPLY_SIGNALS = {'interview', 'offer', 'rejection', 'hr_question', 'assessment', 'other_reply'}


def _when(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        when = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        try:
            when = datetime.strptime(value.strip()[:10], '%Y-%m-%d')
        except ValueError:
            return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _family(role, market_value):
    text = ' '.join((role or '').split()).casefold()
    if not text:
        return None
    best = None
    for family in (market_value or {}).get('role_families', []):
        names = [t.get('title', '') for t in family.get('titles', []) if isinstance(t, dict)]
        names += list(family.get('synonyms', []))
        for name in names:
            key = ' '.join(str(name).split()).casefold()
            if not key:
                continue
            if key == text:
                return family['id']
            if key in text and (best is None or len(key) > best[0]):
                best = (len(key), family['id'])
    return best[1] if best else None


def _company_type(company, market_value):
    for item in (market_value or {}).get('target_companies', []):
        if isinstance(item, dict) and item.get('type') and str(item.get('name', '')).casefold() == (company or '').casefold():
            return item['type']
    return None


def _location(text, market_value):
    text = (text or '').casefold()
    if not text:
        return None
    for loc in (market_value or {}).get('geography', {}).get('locations', []):
        keys = [loc.get('name', '')] + list(loc.get('match', []))
        if any(k and k.casefold() in text for k in keys):
            return loc['name']
    return ' '.join(text.split())[:40]


def _group(table, key, outcome):
    row = table.setdefault(key or 'unknown', Counter())
    row['applications'] += 1
    for name in ('sent', 'replied', 'interviews', 'rejections'):
        row[name] += outcome[name]


def _rates(table):
    out = {}
    for key, row in sorted(table.items()):
        sent = row['sent']
        out[key] = dict(row, silent=max(0, sent - row['replied']),
                        reply_rate=round(row['replied'] / sent, 2) if sent else None, low_sample=sent < 5)
    return out


def _application_stats(ws, cutoff, test, market_value):
    base = contained(ws, layout.sandboxed(layout.APPLICATIONS, test))
    if not base.is_dir():
        return {'available': False, 'note': 'No application records yet.'}
    totals, by_status = Counter(), Counter()
    tables = {'role_family': {}, 'company_type': {}, 'location': {}}
    unreadable = 0
    for path in sorted(base.glob('APP-*/record.json')):
        try:
            record = read_json(path)
        except (ValueError, OSError, UnicodeDecodeError):
            unreadable += 1
            continue
        history = [h for h in record.get('status_history') or [] if isinstance(h, dict)]
        statuses = {h.get('status') for h in history} | {record.get('status')}
        submitted = next((h.get('at') for h in history if h.get('status') == 'submitted'), None)
        when = _when(submitted) or _when(record.get('created_at'))
        if cutoff and when and when < cutoff:
            continue
        signals = {(i.get('evidence') or {}).get('signal') for i in record.get('interactions') or []
                   if isinstance(i, dict) and isinstance(i.get('evidence'), dict)}
        outcome = {'sent': int(bool(statuses & SENT_STATUSES)),
                   'replied': int(bool(statuses & REPLY_STATUSES or signals & REPLY_SIGNALS)),
                   'interviews': int(bool(statuses & {'interview', 'offer'} or 'interview' in signals)),
                   'rejections': int('rejected' in statuses or 'rejection' in signals)}
        totals['applications'] += 1
        for key, value in outcome.items():
            totals[key] += value
        by_status[record.get('status') or 'unknown'] += 1
        analysis = {}
        if isinstance(record.get('analysis'), dict) and record['analysis'].get('path'):
            try:
                analysis = read_json(contained(path.parent, record['analysis']['path']))
            except (ValueError, OSError, UnicodeDecodeError):
                analysis = {}
        family = _family(record.get('role'), market_value) or analysis.get('role_track')
        company = _company_type(record.get('company'), market_value) or analysis.get('employer_type')
        job = record.get('job') if isinstance(record.get('job'), dict) else {}
        place = _location(record.get('location') or job.get('location'), market_value)
        _group(tables['role_family'], family, outcome)
        _group(tables['company_type'], company if company not in (None, 'unclear') else None, outcome)
        _group(tables['location'], place, outcome)
    sent = totals['sent']
    return {'available': True, 'applications': totals['applications'], 'by_status': dict(by_status),
            'sent': sent, 'replied': totals['replied'], 'interviews': totals['interviews'],
            'rejections': totals['rejections'], 'silent': max(0, sent - totals['replied']),
            'reply_rate': round(totals['replied'] / sent, 2) if sent else None,
            'by_role_family': _rates(tables['role_family']), 'by_company_type': _rates(tables['company_type']),
            'by_location': _rates(tables['location']), 'unreadable_records': unreadable}


STORE_META = ('schema', 'schema_version', 'version', 'updated_at', 'generated_at')


def _records(value):
    """A list of dict records from an unknown store shape (list, {"items": [...]}, or {id: record})."""
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)]
    if not isinstance(value, dict):
        return None
    for key in ('items', 'records', 'contacts', 'messages', 'threads', 'outreach', 'entries'):
        inner = value.get(key)
        if isinstance(inner, list):
            return [r for r in inner if isinstance(r, dict)]
        if isinstance(inner, dict) and inner and all(isinstance(v, dict) for v in inner.values()):
            return list(inner.values())
    rest = [v for k, v in value.items() if k not in STORE_META]
    return rest if rest and all(isinstance(v, dict) for v in rest) else None


def _first_time(record, keys):
    for key in keys:
        when = _when(record.get(key))
        if when:
            return when
    return None


def _outreach_stats(ws, cutoff):
    path = contained(ws, layout.OUTREACH)
    if not path.is_file():
        return {'available': False, 'note': 'No outreach log yet.'}
    try:
        records = _records(read_json(path))
    except (ValueError, OSError, UnicodeDecodeError):
        return {'available': False, 'note': 'jobs/outreach.json is not readable JSON.'}
    if records is None:
        return {'available': False, 'note': 'jobs/outreach.json has a shape this version does not read.'}
    by_status, sent, replied, total = Counter(), 0, 0, 0
    for record in records:
        when = _first_time(record, ('sent_at', 'created_at', 'at', 'updated_at'))
        if cutoff and when and when < cutoff:
            continue
        total += 1
        status = str(record.get('status') or record.get('state') or 'unknown').casefold()
        by_status[status] += 1
        got_reply = bool(record.get('replied_at') or record.get('reply_at') or record.get('replied') is True or
                         status in ('replied', 'reply', 'responded', 'interview', 'referred', 'answered'))
        if got_reply or record.get('sent_at') or status in ('sent', 'delivered', 'bounced', 'followed-up', 'follow-up-sent'):
            sent += 1
            replied += int(got_reply)
    return {'available': True, 'records': total, 'by_status': dict(by_status), 'sent': sent, 'replied': replied,
            'reply_rate': round(replied / sent, 2) if sent else None}


SKIP_KEYS = ('priority', 'stage', 'status', 'decision', 'verdict', 'label')
REASON_KEYS = ('skip_reason', 'reason', 'why', 'rationale', 'reasons', 'recommendation', 'gaps')


def _skips(value, found, depth=0):
    if depth > 6:
        return
    if isinstance(value, dict):
        marker = any(isinstance(value.get(k), str) and value[k].strip().casefold() in ('skip', 'skipped')
                     for k in SKIP_KEYS)
        if marker:
            reason = next((value[k] for k in REASON_KEYS if value.get(k)), None)
            if isinstance(reason, list):
                reason = '; '.join(x for x in reason if isinstance(x, str))
            key = value.get('job_id') or value.get('id') or id(value)
            found.setdefault(key, (' '.join(str(reason).split())[:100] if reason else 'no reason recorded',
                                   _first_time(value, ('at', 'saved_at', 'updated_at', 'decided_at', 'created_at'))))
        for item in value.values():
            _skips(item, found, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _skips(item, found, depth + 1)


def _skip_stats(ws, cutoff):
    found, seen = {}, False
    for rel in (layout.REGISTRY, layout.PIPELINE_STATE):
        path = contained(ws, rel)
        if path.is_file():
            seen = True
            try:
                _skips(read_json(path), found)
            except (ValueError, OSError, UnicodeDecodeError):
                continue
    if not seen:
        return {'available': False, 'note': 'No saved jobs or priorities yet.'}
    reasons = Counter(reason for reason, when in found.values() if not (cutoff and when and when < cutoff))
    return {'available': True, 'skipped': sum(reasons.values()),
            'top_reasons': [{'reason': r, 'count': c} for r, c in reasons.most_common(10)]}


def stats(ws=None, days=30, test=False):
    """Outcomes by status, role family, company type and location; robust to missing stores."""
    ws = require_workspace(ws)
    if days < 0:
        raise ValueError('--days must be 0 (all time) or more')
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    try:
        market_value = market.load(ws) or {}
    except (ValueError, OSError):
        market_value = {}
    learning = Counter(i.get('status') for i in items(ws))
    result = {'window_days': days or None, 'since': cutoff.isoformat() if cutoff else None,
              'applications': _application_stats(ws, cutoff, test, market_value),
              'outreach': _outreach_stats(ws, cutoff), 'skips': _skip_stats(ws, cutoff),
              'learning': {s: learning.get(s, 0) for s in STATUSES}}
    notes = [section['note'] for section in (result['applications'], result['outreach'], result['skips'])
             if section.get('note')]
    if result['applications'].get('available') and (result['applications']['sent'] or 0) < 5:
        notes.append('Fewer than 5 sent applications in this window: treat rates as anecdotes, not trends.')
    result['notes'] = notes
    return result


# ------------------------------------------------------------------ LEARNED.md compaction
COMPACT_TX = 'TX-learn-compact-'
RULE_TYPES = ('preference', 'workflow', 'market', 'form-answer', 'example')
AREAS = OrderedDict([('voice', 'Writing and voice'), ('forms', 'Forms and recruiter answers'),
                     ('market', 'Market and search'), ('workflow', 'Workflow'), ('preference', 'Preferences')])
HEADER = ('# Learned rules\n\n'
          '<!-- A compact list of active preferences The Jobfather has learned from you, each with the\n'
          '     event ID that introduced it. Loaded at the start of every task. The full history is in\n'
          '     learning/CHANGELOG.md. -->\n')


def _rolled_back(log):
    """Event ids whose effect is currently undone (a rollback of a rollback restores the original)."""
    undone_by = {}
    for event in log:
        if event.get('rollback_of'):
            undone_by.setdefault(event['rollback_of'], []).append(event['event_id'])
    memo = {}

    def active(event_id, depth=0):
        if event_id not in memo:
            memo[event_id] = True  # guards against a malformed cycle
            memo[event_id] = depth > 100 or not any(active(r, depth + 1) for r in undone_by.get(event_id, []))
        return memo[event_id]

    return {e['event_id'] for e in log if not active(e['event_id'])}


def _area(event):
    paths = set(event.get('affected_paths') or [])
    kind = event.get('change_type')
    if layout.VOICE in paths:
        return 'voice'
    if kind == 'example':
        return None  # stories and examples outside VOICE.md are not rules
    if kind == 'form-answer' or layout.FORM_ANSWERS in paths:
        return 'forms'
    if kind == 'market' or paths & {layout.MARKET_JSON, layout.MARKET_MD}:
        return 'market'
    return 'workflow' if kind == 'workflow' else 'preference'


def active_rules(log, exclude=()):
    """Global preference/workflow/market/form-answer/voice events that are saved and not rolled back."""
    undone = _rolled_back(log)
    rules = []
    for event in log:
        if (event.get('rollback_of') or event.get('scope') != 'global' or event.get('change_type') not in RULE_TYPES or
                event.get('status') not in SAVED_STATUSES or event['transaction_id'].startswith(COMPACT_TX) or
                event['event_id'] in undone or event['event_id'] in exclude):
            continue
        area = _area(event)
        if area:
            rules.append({'event_id': event['event_id'], 'area': area, 'change_type': event['change_type'],
                          'date': str(event.get('timestamp_local') or event.get('timestamp_utc') or '')[:10],
                          'text': ' '.join(str(event['summary']['after']).split())})
    return rules


def render_learned(rules):
    if not rules:
        return HEADER
    parts = [HEADER]
    for key, title in AREAS.items():
        chosen = [r for r in rules if r['area'] == key]
        if chosen:
            parts.append('\n## %s\n\n' % title + ''.join('- %s (%s, %s)\n' % (r['text'], r['event_id'], r['date'])
                                                          for r in chosen))
    return ''.join(parts)


def compact(ws=None, request=None, host='unknown', exclude=()):
    """Preview LEARNED.md rebuilt from the change log; with the user's words, stage a memory.py proposal."""
    ws = require_workspace(ws)
    platform(host)
    exclude = set(exclude or ())
    revision = memory.current(ws)
    log = memory.events(ws)
    known = {e['event_id'] for e in log}
    strangers = sorted(exclude - known)
    if strangers:
        raise ValueError('Unknown events to exclude: ' + ', '.join(strangers))
    rules = active_rules(log, exclude)
    path = contained(ws, layout.LEARNED)
    old = read_text(path) if path.is_file() else None
    new = render_learned(rules)
    old_ids = set(re.findall(r'EV-[0-9a-f]{32}', old or ''))
    body = re.sub(r'<!--.*?-->', '', old or '', flags=re.S)
    unlinked = [line.strip() for line in body.splitlines()
                if line.strip() and not line.lstrip().startswith('#') and not re.search(r'EV-[0-9a-f]{32}', line)]
    result = {'rules': len(rules), 'added': [r['event_id'] for r in rules if r['event_id'] not in old_ids],
              'dropped': sorted(old_ids - {r['event_id'] for r in rules}), 'unlinked_lines': unlinked,
              'excluded': sorted(exclude)}
    if old == new:
        return dict(result, status='up-to-date')
    managed.validate_file(layout.LEARNED, old, new)
    texts = managed.read_all(ws)
    texts[layout.LEARNED] = new
    managed.validate_references(texts)
    result['preview'] = new
    if unlinked:
        result['warning'] = ('These lines have no event id and would be removed. If the user wants to keep one, '
                             'save it as a preference first (memory.py), then compact again.')
    if not request or not request.strip():
        return dict(result, status='preview', next='Show the preview. If the user agrees, run compact again with '
                                                   '--request "<their words>" and apply the staged proposal.')
    proposal = {'transaction_id': COMPACT_TX + sha_text(new + revision['revision'])[:20],
                'intent': 'explicit-update', 'user_request': request.strip()[:4000], 'platform': host,
                'scope': 'global', 'change_type': 'workflow', 'expected_revision': revision['revision'],
                'summary': {'before': 'LEARNED.md listed %d linked rules.' % len(old_ids),
                            'after': 'LEARNED.md lists the %d active rules from the change log, grouped by area.'
                                     % len(rules)},
                'reason': 'Rebuilt from logged, not rolled back preference, workflow, market, form-answer and '
                          'voice events.',
                'review': {'user_authorized': True, 'facts_checked': True, 'scope_checked': True},
                'changes': [{'path': layout.LEARNED, 'before_sha256': digest(path) if old is not None else 'missing',
                             'write': {'text': new, 'sha256': sha_text(new)}}]}
    staged = PROPOSALS + '/' + proposal['transaction_id'] + '.json'
    atomic_json(contained(ws, staged), proposal)
    return dict(result, status='proposal-ready', proposal=staged,
                next='python3 SKILL/scripts/memory.py apply --file "%s"' % contained(ws, staged))


# ------------------------------------------------------------------ CLI
def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    sub = parser.add_subparsers(dest='command', required=True)
    child = sub.add_parser('queue', help='queue something the user confirmed, to offer saving it')
    child.add_argument('--kind', choices=KINDS, required=True)
    child.add_argument('--text', required=True, help='the short reusable lesson or fact, in plain words')
    child.add_argument('--source', choices=SOURCES, default='chat')
    child.add_argument('--scope', choices=('global', 'application'), default='global')
    child.add_argument('--application-id')
    child.add_argument('--target', help='managed file that should own it, e.g. VOICE.md (default by kind)')
    child.add_argument('--evidence', help='where it came from (a quote, a diff run id)')
    child.add_argument('--sensitive', action='store_true', help='pay, notice, address: keep values out of summaries')
    child.add_argument('--test', action='store_true', help='application lives in the fictional sandbox')
    sub.add_parser('list', help='queued items').add_argument('--pending', action='store_true')
    child = sub.add_parser('resolve', help='mark items saved once memory.py logged their event')
    child.add_argument('--event', required=True)
    child.add_argument('--ids', nargs='+', help='default: every pending item the event lists')
    child = sub.add_parser('dismiss', help='close items the user did not want saved')
    child.add_argument('--ids', nargs='+', required=True)
    child.add_argument('--reason', required=True)
    child.add_argument('--forget', action='store_true', help='also blank the text of these items')
    sub.add_parser('sweep', help='end-of-task checklist and pending items').add_argument('--json', action='store_true')
    child = sub.add_parser('stats', help='outcomes by status, role family, company type and location')
    child.add_argument('--days', type=int, default=30, help='window in days (0 = all time)')
    child.add_argument('--test', action='store_true', help='read the fictional sandbox applications')
    child = sub.add_parser('compact', help='rebuild LEARNED.md from the change log (proposal only)')
    child.add_argument('--request', help="the user's words asking to update the learned rules")
    child.add_argument('--platform', default='unknown')
    child.add_argument('--exclude', nargs='+', default=[], help='event ids the user says are superseded')
    args = parser.parse_args()
    ws = args.workspace
    if args.command == 'queue':
        result = queue(ws, args.kind, args.text, args.source, args.scope, args.application_id, args.target,
                       args.evidence, args.sensitive, args.test)
    elif args.command == 'list':
        result = listing(ws, args.pending)
    elif args.command == 'resolve':
        result = resolve(ws, args.event, args.ids)
    elif args.command == 'dismiss':
        result = dismiss(ws, args.ids, args.reason, args.forget)
    elif args.command == 'sweep':
        result = sweep(ws)
        if not args.json:
            sys.stdout.write(sweep_text(result))
            return
    elif args.command == 'stats':
        result = stats(ws, args.days, args.test)
    else:
        result = compact(ws, args.request, args.platform, args.exclude)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        raise SystemExit('Learning step failed: ' + str(error))
