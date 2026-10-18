"""Local job-listing registry. Captured listings are data; nothing here drafts, sends or applies.

Storage (workspace-relative; --test keeps fictional data under .state/sandbox/):
    jobs/registry.json                         every captured job (schema_version 1)
    jobs/sources/<sha256>.<ext>                supplied files, stored by content hash
    jobs/sources/<source_key>.json             immutable source captures
    jobs/history/rows/<job_id>/<sha256>.json   every committed version of a row, content-addressed
    jobs/history/journal.jsonl                 one line per committed row change

Input schema, commands and outcome rules: references/JOB-INTAKE.md.
"""
import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
import uuid

# CLI execution and imports from tracker.py/job_pipeline.py must share one module.
if __name__ == '__main__':
    sys.modules['job_intake'] = sys.modules[__name__]

from applications import SUBMISSION_EVIDENCE, Store
import layout
from local import (PLATFORMS, add_workspace_arg, atomic_bytes, atomic_json, config, contained, digest, locked, now,
                   platform, read_json, read_text, require_workspace)

SCALAR_FIELDS = (
    'company', 'role', 'recruiter_name', 'recruiter_company', 'recruiter_title',
    'email', 'phone', 'recruiter_linkedin', 'post_author', 'location', 'work_mode',
    'employment_type', 'experience', 'salary', 'skills', 'jd_summary', 'full_jd',
    'responsibilities', 'requirements', 'benefits', 'shift', 'working_days',
    'joining_timeline', 'posted_date', 'deadline', 'application_url',
    'application_method', 'requisition_id', 'notes',
)
INPUT_FIELDS = set(SCALAR_FIELDS) | {
    'job_id', 'additional_contacts', 'uncertain_fields', 'workflow_status', 'application_id', 'sources',
}
BATCH_KEYS = {'jobs', 'report'}  # boards.py may print its fetch report beside the jobs; it is ignored
CONTACT_FIELDS = ('name', 'email', 'phone', 'linkedin', 'role')
SOURCE_FIELDS = {'type', 'label', 'url', 'text', 'file', 'access'}
SOURCE_TYPES = ('screenshot', 'linkedin', 'url', 'text', 'document')
ACCESS = ('readable', 'partial', 'blocked')
IDENTITY_FIELDS = ('company', 'role', 'location', 'requisition_id')
TRACKING_PARAMS = {
    'fbclid', 'gclid', 'dclid', 'gbraid', 'wbraid', 'msclkid', 'yclid', 'twclid', 'ttclid', 'li_fat_id',
    'mc_cid', 'mc_eid', 'igshid', '_hsenc', '_hsmi', 'hsctatracking', 'trk', 'trkinfo', 'trackingid',
    'refid', 'lipi', 'midtoken', 'midsig', 'ebp', 'recommendedflavor', 'originalsubdomain',
}
JOB_ID = re.compile(r'JOB-[0-9a-f]{12}')
APP_ID = re.compile(r'APP-[0-9a-f]{12}')
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                   r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+")
LINKEDIN_HOST = re.compile(r'(?:^|\.)linkedin\.com$')
MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_NOTES = 10000
SUBMITTED_STAGE = 'Applied'
SUBMISSION_SCHEMA = 'the-jobfather/submission@1'
ATS_RECEIPT_SCHEMA = 'the-jobfather/ats-receipt@1'  # ats/*.py receipts hold the submission record
SUBMISSION_FIELDS = {'schema', 'channel', 'submitted_at', 'authorized_by_user', 'user_request', 'evidence',
                     'cv_output_id', 'approval_id'}
SUBMISSION_EVIDENCE_FIELDS = {'kind', 'url', 'quote', 'capture_sha256', 'message_id'}
BEYOND_SUBMITTED = ('interview', 'offer', 'rejected', 'withdrawn')
CLOCK_SKEW = timedelta(minutes=5)


# ------------------------------------------------------------------ shared helpers
def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                       allow_nan=False) + '\n').encode('utf-8')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


_FRACTION = re.compile(r'(\.\d+)(?=(?:[+-]\d{2}:?\d{2})?$)')
_COMPACT_OFFSET = re.compile(r'([+-]\d{2})(\d{2})$')


def parse_time(value, what='Timestamp'):
    """An ISO 8601 timestamp WITH a timezone, as an aware datetime (accepts a trailing Z)."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError('%s needs an ISO timestamp with a timezone' % what)
    text = value.strip()
    if text[-1] in 'Zz':
        text = text[:-1] + '+00:00'
    if 'T' in text or ' ' in text:
        text = _COMPACT_OFFSET.sub(r'\1:\2', text)
    text = _FRACTION.sub(lambda m: (m.group(1) + '000000')[:7], text)
    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError('%s is not an ISO timestamp: %r' % (what, value))
    if result.tzinfo is None:
        raise ValueError('%s needs a timezone (for example -04:00 or Z): %r' % (what, value))
    return result


def valid_email(value):
    if not isinstance(value, str) or not EMAIL.fullmatch(value):
        return False
    local_part = value.split('@')[0]
    return not ('..' in local_part or local_part.startswith('.') or local_part.endswith('.'))


def stages(ws):
    """The user's tracker stages in order (config.json tracker.stages)."""
    value = config(ws).get('tracker', {}).get('stages')
    if not isinstance(value, list) or not value or any(not isinstance(s, str) or not s.strip() for s in value):
        raise ValueError('config.json tracker.stages must be a non-empty list of stage names')
    names = [s.strip() for s in value]
    if len({s.casefold() for s in names}) != len(names):
        raise ValueError('config.json tracker.stages has duplicate names')
    return names


def canonical_stage(value, allowed):
    """The configured spelling of a stage name (case-insensitive), or a clear error."""
    if isinstance(value, str):
        wanted = value.strip().casefold()
        for stage in allowed:
            if stage.casefold() == wanted:
                return stage
    raise ValueError('Stage must be one of: ' + ', '.join(allowed))


def _scalar(value, field):
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        # Full descriptions and notes retain every supplied character.
        return value if field.endswith(('.full_jd', '.notes')) else value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(field + ' must be finite')
        return value
    raise ValueError(field + ' must be text, a finite number, or null')


def _text(value, field, required=False, exact=False):
    if exact and isinstance(value, str) and not required:
        return value
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(field + ' requires nonempty text')
        return None
    if not isinstance(value, str):
        raise ValueError(field + ' must be text')
    return value if exact else value.strip()


def _email(value, field):
    if value is not None and not valid_email(value):
        raise ValueError(field + ' has invalid email syntax; keep it in the source text and uncertain_fields instead')


def normalize_url(value):
    """Canonical form for duplicate detection: tracking parameters removed, LinkedIn posts and jobs by ID."""
    if not isinstance(value, str):
        raise ValueError('URL must be text')
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Source/application URL must be an absolute http(s) URL')
    if parsed.username or parsed.password:
        raise ValueError('URLs containing credentials are not accepted')
    try:
        parsed.port
    except ValueError as error:
        raise ValueError('Invalid URL port') from error
    if LINKEDIN_HOST.search(parsed.hostname.lower()):
        activity = re.search(r'(?:activity[-:]|urn:li:activity:)(\d+)', unquote(value))
        if activity:
            return 'linkedin:activity:' + activity.group(1)
        job = (re.search(r'/jobs/view/(?:[^/?#]*-)?(\d{6,})(?:[/?#]|$)', parsed.path + '/')
               or re.search(r'(?:^|&)currentJobId=(\d+)', parsed.query))
        if job:
            return 'linkedin:job:' + job.group(1)
    query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith('utm_') and key.lower() not in TRACKING_PARAMS]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path,
                       urlencode(sorted(query)), parsed.fragment))


def _norm(value):
    return ' '.join(str(value).split()).casefold() if value is not None else None


def _identity(row):
    return tuple(_norm(row.get(name)) for name in IDENTITY_FIELDS)


def _source_evidence(row):
    urls, hashes = set(), set()
    for source in row['sources']:
        if source.get('normalized_url'):
            urls.add(source['normalized_url'])
        for key in ('text_sha256', 'file_sha256'):
            if source.get(key):
                hashes.add(source[key])
    return urls, hashes


def _same_listing(left, right):
    if _identity(left) != _identity(right):
        return False
    left_urls, left_hashes = _source_evidence(left)
    right_urls, right_hashes = _source_evidence(right)
    return bool(left_urls & right_urls or left_hashes & right_hashes)


def _advance(row, target, allowed):
    """Move the stage forward to `target` unless it is already there or further along."""
    order = [s.casefold() for s in allowed]
    if target.casefold() not in order:
        return False
    goal = order.index(target.casefold())
    current = row.get('workflow_status')
    position = order.index(current.casefold()) if isinstance(current, str) and current.casefold() in order else -1
    if position >= goal:
        return False
    row['workflow_status'] = allowed[goal]
    return True


# ------------------------------------------------------------------ submission receipts
def validate_submission(value):
    """Check a the-jobfather/submission@1 record (or an ATS receipt that holds one); returns a clean copy."""
    if not isinstance(value, dict):
        raise ValueError('Submission must be a JSON object')
    if value.get('schema') == ATS_RECEIPT_SCHEMA and isinstance(value.get('submission'), dict):
        inner = dict(value['submission'])
        if value.get('approval_id') and 'approval_id' not in inner:
            inner['approval_id'] = value['approval_id']
        value = inner
    unknown = set(value) - SUBMISSION_FIELDS
    if unknown:
        raise ValueError('Unrecognized submission fields: ' + ', '.join(sorted(unknown)))
    if value.get('schema') != SUBMISSION_SCHEMA:
        raise ValueError('Submission needs "schema": "%s"' % SUBMISSION_SCHEMA)
    channel = _text(value.get('channel'), 'channel', required=True)
    submitted = parse_time(value.get('submitted_at'), 'submitted_at')
    if submitted > datetime.now(timezone.utc) + CLOCK_SKEW:
        raise ValueError('submitted_at is in the future')
    if value.get('authorized_by_user') is not True:
        raise ValueError('Record a submission only when the user authorized it (authorized_by_user: true)')
    request = _text(value.get('user_request'), 'user_request', required=True)
    if len(request) > 2000:
        raise ValueError('user_request is limited to 2000 characters')
    proof = value.get('evidence')
    if not isinstance(proof, dict) or set(proof) - SUBMISSION_EVIDENCE_FIELDS:
        raise ValueError('evidence may hold only: ' + ', '.join(sorted(SUBMISSION_EVIDENCE_FIELDS)))
    if proof.get('kind') not in SUBMISSION_EVIDENCE:
        raise ValueError('evidence.kind must be one of: ' + ', '.join(SUBMISSION_EVIDENCE))
    clean = {'kind': proof['kind']}
    if proof.get('url') not in (None, ''):
        normalize_url(proof['url'])
        clean['url'] = proof['url'].strip()
    for key in ('quote', 'message_id'):
        found = _text(proof.get(key), 'evidence.' + key)
        if found is not None:
            clean[key] = found
    if proof.get('capture_sha256') not in (None, ''):
        if not isinstance(proof['capture_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', proof['capture_sha256']):
            raise ValueError('evidence.capture_sha256 must be a lowercase SHA-256 hex digest')
        clean['capture_sha256'] = proof['capture_sha256']
    if clean['kind'] != 'user-reported' and not ({'url', 'quote', 'message_id'} & set(clean)):
        raise ValueError('Submission evidence needs the confirmation url, quote or message_id')
    if clean['kind'] in ('confirmation-email', 'sent-email') and 'message_id' not in clean:
        raise ValueError('Email evidence needs the message_id')
    result = {'schema': SUBMISSION_SCHEMA, 'channel': channel, 'submitted_at': value['submitted_at'].strip(),
              'authorized_by_user': True, 'user_request': request, 'evidence': clean}
    for key in ('cv_output_id', 'approval_id'):
        if value.get(key) is not None:
            result[key] = _text(value[key], key, required=True)
    return result


class JobStore:
    """The job registry with its immutable sources and row history. One lock: the jobs folder."""

    def __init__(self, ws=None, test=False):
        self.ws = require_workspace(ws)
        self.test = bool(test)
        self.base = contained(self.ws, layout.sandboxed(layout.JOBS, self.test))
        self.path = contained(self.ws, layout.sandboxed(layout.REGISTRY, self.test))
        self.sources_rel = layout.sandboxed(layout.JOB_SOURCES, self.test)
        self.history_rel = layout.sandboxed(layout.JOB_HISTORY, self.test)
        self.journal = contained(self.ws, self.history_rel + '/journal.jsonl')

    # -------------------------------------------------------------- reading
    def _stored(self, relative):
        if not isinstance(relative, str) or not relative.startswith(self.sources_rel + '/'):
            raise RuntimeError('Stored source path is outside the job sources folder: %r' % (relative,))
        return contained(self.ws, relative)

    def _load(self):
        if not self.path.exists():
            return {'schema_version': 1, 'jobs': []}
        registry = read_json(self.path)
        if (not isinstance(registry, dict) or registry.get('schema_version') != 1
                or not isinstance(registry.get('jobs'), list)):
            raise ValueError('Unsupported or malformed job registry')
        ids = []
        for row in registry['jobs']:
            if not isinstance(row, dict) or not JOB_ID.fullmatch(str(row.get('job_id', ''))):
                raise ValueError('Registry contains an invalid job ID')
            if not isinstance(row.get('sources'), list) or not row['sources']:
                raise ValueError('Registry job %s has no source' % row['job_id'])
            ids.append(row['job_id'])
        if len(ids) != len(set(ids)):
            raise ValueError('Registry contains duplicate job IDs')
        for row in registry['jobs']:
            for source in row['sources']:
                snapshot = self._stored(source.get('snapshot'))
                if not snapshot.is_file() or snapshot.read_bytes() != _bytes(source):
                    raise RuntimeError('Stored source snapshot integrity failed: ' + source['snapshot'])
                if source.get('file'):
                    path = self._stored(source['file'])
                    if not path.is_file() or _sha(path.read_bytes()) != source.get('file_sha256'):
                        raise RuntimeError('Stored source file integrity failed: ' + source['file'])
        return registry

    def show(self, job_id=None):
        with locked(self.base):
            registry = self._load()
        if job_id is None:
            return registry
        row = next((row for row in registry['jobs'] if row['job_id'] == job_id), None)
        if row is None:
            raise ValueError('Unknown job_id: ' + str(job_id))
        return row

    def _receipt(self, added=0, updated=0, duplicates=0, needs_review=0, jobs=None):
        return {'added': added, 'updated': updated, 'duplicates': duplicates, 'needs_review': needs_review,
                'registry_path': str(self.path), 'jobs': jobs or []}

    # -------------------------------------------------------------- immutable files and history
    def _immutable(self, relative, data):
        path = contained(self.ws, relative)
        if path.exists():
            if path.read_bytes() != data:
                raise RuntimeError('Immutable source/snapshot integrity failed: ' + relative)
            return False
        atomic_bytes(path, data)
        return True

    def _row_rel(self, job_id, sha):
        return '%s/rows/%s/%s.json' % (self.history_rel, job_id, sha)

    def _journal_entries(self):
        if not self.journal.is_file():
            return []
        data = self.journal.read_bytes()
        end = data.rfind(b'\n') + 1  # a torn final line (interrupted append) is ignored
        return [json.loads(line) for line in data[:end].decode('utf-8').splitlines() if line.strip()]

    def _journal_last(self):
        if not self.journal.is_file():
            return None
        with self.journal.open('rb') as stream:
            size = stream.seek(0, os.SEEK_END)
            window = 4096
            while True:
                start = max(0, size - window)
                stream.seek(start)
                data = stream.read(size - start)
                end = data.rfind(b'\n')
                if end >= 0:
                    begin = data.rfind(b'\n', 0, end)
                    if begin >= 0 or start == 0:
                        line = data[begin + 1:end]
                        return json.loads(line.decode('utf-8')) if line.strip() else None
                elif start == 0:
                    return None
                window *= 4

    def _journal_append(self, entries):
        if not entries:
            return
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        payload = b''.join(_bytes(entry) for entry in entries)
        with self.journal.open('r+b' if self.journal.exists() else 'wb') as stream:
            size = stream.seek(0, os.SEEK_END)
            if size:
                stream.seek(size - 1)
                if stream.read(1) != b'\n':  # drop a torn final line left by an interrupted append
                    stream.seek(0)
                    size = stream.read().rfind(b'\n') + 1
                    stream.truncate(size)
            stream.seek(size)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    def _catch_up(self, registry):
        """Log registry changes that bypassed the journal (a hand edit or an interrupted commit)."""
        if not self.path.is_file():
            return
        current = digest(self.path)
        last = self._journal_last()
        if last is not None and last.get('registry_sha256') == current:
            return
        known = {}
        for entry in self._journal_entries():
            if entry.get('job_id'):
                known[entry['job_id']] = entry.get('after_sha256')
        entries, present = [], set()
        for job in registry['jobs']:
            present.add(job['job_id'])
            data = _bytes(job)
            sha = _sha(data)
            if known.get(job['job_id']) != sha:
                self._immutable(self._row_rel(job['job_id'], sha), data)
                entries.append({'action': 'observed', 'job_id': job['job_id'],
                                'before_sha256': known.get(job['job_id']), 'after_sha256': sha})
        for job_id, sha in sorted(known.items()):
            if job_id not in present and sha is not None:
                entries.append({'action': 'observed-removed', 'job_id': job_id, 'before_sha256': sha,
                                'after_sha256': None})
        at = now()
        self._journal_append([dict(entry, at=at, platform='unknown', registry_sha256=current)
                              for entry in entries or [{'action': 'observed'}]])

    def _commit(self, before, after, actions, host, files=None):
        """Immutable captures and row versions first, then the registry, then the journal."""
        referenced = {source[key] for job in after['jobs'] for source in job['sources']
                      for key in ('file', 'snapshot') if source.get(key)}
        for relative, data in sorted((files or {}).items()):
            if relative in referenced:  # duplicates create no extra files
                self._immutable(relative, data)
        old = {job['job_id']: job for job in before['jobs']}
        changes = []
        for job in after['jobs']:
            previous = old.get(job['job_id'])
            if previous == job:
                continue
            data = _bytes(job)
            after_sha, before_sha = _sha(data), None
            if previous is not None:
                prior = _bytes(previous)
                before_sha = _sha(prior)
                self._immutable(self._row_rel(job['job_id'], before_sha), prior)
            self._immutable(self._row_rel(job['job_id'], after_sha), data)
            changes.append({'action': actions.get(job['job_id'], 'updated'), 'job_id': job['job_id'],
                            'before_sha256': before_sha, 'after_sha256': after_sha})
        created = not self.path.exists()
        atomic_json(self.path, after)
        registry_sha = digest(self.path)
        if created and not changes:
            changes.append({'action': 'registry-created'})
        at = now()
        self._journal_append([dict(change, at=at, platform=host, registry_sha256=registry_sha)
                              for change in changes])

    def history(self, job_id=None):
        """Journal entries (oldest first), optionally for one job."""
        with locked(self.base):
            entries = self._journal_entries()
        return [e for e in entries if job_id is None or e.get('job_id') == job_id]

    # -------------------------------------------------------------- writing
    def init(self, host='unknown'):
        platform(host)
        with locked(self.base):
            registry = self._load()
            if not self.path.exists():
                self._commit(registry, registry, {}, host)
        return self._receipt()

    def _source(self, value, location, source_base, captured, host):
        if not isinstance(value, dict) or set(value) - SOURCE_FIELDS:
            raise ValueError(location + ': unrecognized source fields')
        if value.get('type') not in SOURCE_TYPES or value.get('access') not in ACCESS:
            raise ValueError(location + ': source requires a type (%s) and access (%s)'
                             % (', '.join(SOURCE_TYPES), ', '.join(ACCESS)))
        source = {'type': value['type'], 'access': value['access'],
                  'label': _text(value.get('label'), location + '.label', required=True),
                  'url': _text(value.get('url'), location + '.url'),
                  'text': _text(value.get('text'), location + '.text', exact=True)}
        source['normalized_url'] = normalize_url(source['url']) if source['url'] else None
        source['text_sha256'] = _sha(source['text'].encode('utf-8')) if source['text'] is not None else None
        files = {}
        supplied = _text(value.get('file'), location + '.file')
        source.update(file=None, file_sha256=None, supplied_file=supplied)
        if supplied:
            original = Path(supplied).expanduser()
            if not original.is_absolute():
                original = source_base / original
            if not original.is_file():
                raise ValueError(location + '.file is not a readable local file: ' + str(original))
            if original.stat().st_size > MAX_SOURCE_BYTES:
                raise ValueError(location + '.file is larger than 50 MB; supply a screenshot or the text instead')
            data = original.read_bytes()
            file_hash = _sha(data)
            suffix = original.suffix.lower()
            if not re.fullmatch(r'\.[a-z0-9]{1,10}', suffix):
                suffix = '.bin'
            source['file'] = self.sources_rel + '/' + file_hash + suffix
            source['file_sha256'] = file_hash
            files[source['file']] = data
        if not (source['url'] or source['file'] or (source['text'] is not None and source['text'].strip())):
            raise ValueError(location + ': supply a URL, exact source text, or local file')
        # Source identity excludes the original local path and capture metadata.
        key_data = {key: val for key, val in source.items() if key != 'supplied_file'}
        source['source_key'] = _sha(_bytes(key_data))
        source.update(source_id='SRC-' + source['source_key'][:16], captured_at=captured, platform=host,
                      snapshot=self.sources_rel + '/' + source['source_key'] + '.json')
        snapshot = contained(self.ws, source['snapshot'])
        if snapshot.exists():
            stored = read_json(snapshot)
            if (not isinstance(stored, dict) or set(stored) != set(source)
                    or {key: stored.get(key) for key in key_data} != key_data
                    or stored['source_key'] != source['source_key']
                    or stored['source_id'] != source['source_id'] or stored['snapshot'] != source['snapshot']
                    or stored['platform'] not in PLATFORMS or not isinstance(stored['captured_at'], str)
                    or snapshot.read_bytes() != _bytes(stored)):
                raise RuntimeError('Immutable source snapshot integrity failed: ' + source['snapshot'])
            # Reuse the first capture, including one left by an interrupted registry commit.
            source = stored
        files[source['snapshot']] = _bytes(source)
        return source, files

    def _application_exists(self, application_id):
        record = Store(self.ws, test=self.test).directory(application_id) / 'record.json'
        linked = read_json(record) if record.is_file() else None
        return isinstance(linked, dict) and linked.get('application_id') == application_id

    def _row(self, value, index, source_base, captured, host, allowed, existing=None):
        prefix = 'jobs[%d]' % index
        if not isinstance(value, dict):
            raise ValueError(prefix + ' must be an object')
        unknown = set(value) - INPUT_FIELDS
        if unknown:
            raise ValueError(prefix + ': unrecognized fields: ' + ', '.join(sorted(unknown)))
        row = {field: _scalar(value.get(field), prefix + '.' + field) for field in SCALAR_FIELDS}
        _email(row['email'], prefix + '.email')
        if row['application_url'] is not None:
            if not isinstance(row['application_url'], str):
                raise ValueError(prefix + '.application_url must be text')
            normalize_url(row['application_url'])
        status = _text(value.get('workflow_status'), prefix + '.workflow_status')
        try:
            row['workflow_status'] = canonical_stage(status, allowed) if status else allowed[0]
        except ValueError as error:
            raise ValueError(prefix + '.workflow_status: ' + str(error))
        row['application_id'] = _text(value.get('application_id'), prefix + '.application_id')
        if row['application_id']:
            if not APP_ID.fullmatch(row['application_id']):
                raise ValueError(prefix + ': invalid application_id')
            if not self._application_exists(row['application_id']):
                raise ValueError(prefix + ': application_id must link to an existing local application')
        row['job_id'] = _text(value.get('job_id'), prefix + '.job_id')
        if row['job_id'] and not JOB_ID.fullmatch(row['job_id']):
            raise ValueError(prefix + ': invalid job_id')
        contacts = value.get('additional_contacts', [])
        if not isinstance(contacts, list):
            raise ValueError(prefix + '.additional_contacts must be an array')
        row['additional_contacts'] = []
        for contact_index, contact in enumerate(contacts):
            label = '%s.additional_contacts[%d]' % (prefix, contact_index)
            if not isinstance(contact, dict) or set(contact) - set(CONTACT_FIELDS):
                raise ValueError(label + ': unrecognized contact fields')
            normalized = {key: _text(contact.get(key), label + '.' + key) for key in CONTACT_FIELDS}
            if not any(normalized.values()):
                raise ValueError(label + ' is empty')
            _email(normalized['email'], label + '.email')
            row['additional_contacts'].append(normalized)
        uncertain = value.get('uncertain_fields', {})
        if not isinstance(uncertain, dict) or set(uncertain) - (INPUT_FIELDS - {'job_id', 'uncertain_fields'}):
            raise ValueError(prefix + '.uncertain_fields contains unrecognized fields')
        row['uncertain_fields'] = {field: _text(reason, prefix + '.uncertain_fields.' + field, required=True)
                                   for field, reason in uncertain.items()}
        if not isinstance(value.get('sources'), list) or not value['sources']:
            raise ValueError(prefix + ' requires at least one source')
        row['sources'], files = [], {}
        for source_index, source in enumerate(value['sources']):
            normalized, captured_files = self._source(source, '%s.sources[%d]' % (prefix, source_index),
                                                      source_base, captured, host)
            if not any(s['source_key'] == normalized['source_key'] for s in row['sources']):
                row['sources'].append(normalized)
                files.update(captured_files)
        has_description = any(row[field] is not None or (existing is not None and existing.get(field) is not None)
                              for field in ('role', 'jd_summary', 'full_jd'))
        if not has_description and not any(source['access'] in ('partial', 'blocked') for source in row['sources']):
            raise ValueError(prefix + ' needs role/JD text or a partial/blocked source')
        row['_provided'] = set(value)
        hash_row = {key: val for key, val in row.items() if key != '_provided'}
        hash_row['sources'] = [s['source_key'] for s in row['sources']]
        row['_input_sha256'] = _sha(_bytes(hash_row))
        return row, files

    @staticmethod
    def _review(row):
        reasons = []
        if row['uncertain_fields']:
            reasons.append('Uncertain extracted fields')
        if row['conflicts']:
            reasons.append('Conflicting supplied values retained for review')
        if row['duplicate_candidates']:
            reasons.append('Possible duplicate from a different source')
        if any(source['access'] != 'readable' for source in row['sources']):
            reasons.append('Source access is partial or blocked')
        row['review_reasons'] = reasons
        row['review_status'] = 'needs_review' if reasons else 'ready'

    @staticmethod
    def _conflict(record, item, captured, host):
        if not any(all(c.get(key) == val for key, val in item.items()) for c in record['conflicts']):
            record['conflicts'].append(dict(item, captured_at=captured, platform=host))

    def _merge(self, record, row, captured, host):
        before = copy.deepcopy(record)
        for field in SCALAR_FIELDS + ('workflow_status', 'application_id'):
            if field not in row['_provided'] or row[field] is None:
                continue
            if record.get(field) is None:
                record[field] = row[field]
            elif record[field] != row[field]:
                self._conflict(record, {'field': field, 'existing': record[field], 'incoming': row[field]},
                               captured, host)
        for contact in row['additional_contacts']:
            if contact in record['additional_contacts']:
                continue
            match = next((stored for stored in record['additional_contacts'] if any(
                contact[key] and stored[key] and _norm(contact[key]) == _norm(stored[key])
                for key in ('email', 'phone', 'linkedin'))), None)
            if match is None:
                record['additional_contacts'].append(contact)
                continue
            for field in CONTACT_FIELDS:
                if match[field] is None:
                    match[field] = contact[field]
                elif contact[field] is not None and contact[field] != match[field]:
                    name = 'additional_contacts[%d].%s' % (record['additional_contacts'].index(match), field)
                    self._conflict(record, {'field': name, 'existing': match[field], 'incoming': contact[field]},
                                   captured, host)
        for field, reason in row['uncertain_fields'].items():
            previous = record['uncertain_fields'].get(field)
            if previous is None:
                record['uncertain_fields'][field] = reason
            elif reason not in previous.split('\n'):
                record['uncertain_fields'][field] = previous + '\n' + reason
        keys = {source['source_key'] for source in record['sources']}
        record['sources'].extend(source for source in row['sources'] if source['source_key'] not in keys)
        self._review(record)
        if record == before:
            return False
        self._capture(record, row, captured, host, 'enriched')
        return True

    @staticmethod
    def _capture(record, row, captured, host, action):
        capture = {'captured_at': captured, 'platform': host, 'input_sha256': row['_input_sha256'],
                   'source_ids': [source['source_id'] for source in row['sources']]}
        record['captures'].append(capture)
        record['history'].append(dict(capture, action=action))
        record['updated_at'], record['updated_by'] = captured, host

    def ingest(self, payload, host, source_base=None):
        platform(host)
        if (not isinstance(payload, dict) or not isinstance(payload.get('jobs'), list)
                or set(payload) - BATCH_KEYS):
            raise ValueError('Input must be an object with a jobs array (boards.py output is accepted as is)')
        allowed = stages(self.ws)
        source_base = Path(source_base).resolve() if source_base is not None else self.ws
        captured = now()
        with locked(self.base):
            before = self._load()
            self._catch_up(before)
            # Read and validate the entire batch before writing any source or registry.
            rows, files = [], {}
            by_id = {job['job_id']: job for job in before['jobs']}
            for index, value in enumerate(payload['jobs']):
                key = value.get('job_id') if isinstance(value, dict) else None
                existing = by_id.get(key) if isinstance(key, str) else None
                row, captured_files = self._row(value, index, source_base, captured, host, allowed, existing)
                rows.append(row)
                files.update(captured_files)
            if any(row['job_id'] and row['job_id'] not in by_id for row in rows):
                raise ValueError('Explicit job_id must refer to an existing registry row')
            registry = copy.deepcopy(before)
            added = updated = duplicates = 0
            affected, actions, results = set(), {}, []
            for row in rows:
                if row['job_id']:
                    matches = [job for job in registry['jobs'] if job['job_id'] == row['job_id']]
                else:
                    matches = [job for job in registry['jobs'] if _same_listing(job, row)]
                    if len(matches) > 1:
                        exact = [job for job in matches if any(
                            capture['input_sha256'] == row['_input_sha256'] for capture in job['captures'])]
                        if len(exact) == 1:
                            matches = exact
                # Ambiguous existing matches are kept as separate candidates.
                if len(matches) == 1:
                    record = matches[0]
                    changed = self._merge(record, row, captured, host)
                    updated += int(changed)
                    duplicates += int(not changed)
                    result = 'updated' if changed else 'duplicate'
                    if changed:
                        actions.setdefault(record['job_id'], 'enriched')
                else:
                    taken = {job['job_id'] for job in registry['jobs']}
                    job_id = 'JOB-' + uuid.uuid4().hex[:12]
                    while job_id in taken:
                        job_id = 'JOB-' + uuid.uuid4().hex[:12]
                    record = {key: val for key, val in row.items() if not key.startswith('_')}
                    record.update(job_id=job_id, fictional=self.test, created_at=captured, created_by=host,
                                  user_notes=None, conflicts=[], duplicate_candidates=[], captures=[], history=[])
                    self._review(record)
                    self._capture(record, row, captured, host, 'created')
                    registry['jobs'].append(record)
                    added += 1
                    result = 'added'
                    actions[job_id] = 'created'
                affected.add(record['job_id'])
                results.append((record, result))
            for index, left in enumerate(registry['jobs']):
                for right in registry['jobs'][index + 1:]:
                    if (left['company'] is None or left['role'] is None
                            or tuple(_norm(left.get(key)) for key in ('company', 'role', 'location')) !=
                            tuple(_norm(right.get(key)) for key in ('company', 'role', 'location'))):
                        continue
                    for record, candidate in ((left, right), (right, left)):
                        if candidate['job_id'] not in record['duplicate_candidates']:
                            record['duplicate_candidates'].append(candidate['job_id'])
                            record['history'].append({'action': 'duplicate_candidate', 'captured_at': captured,
                                                      'platform': host, 'candidate_job_id': candidate['job_id']})
                            record['updated_at'], record['updated_by'] = captured, host
                            affected.add(record['job_id'])
                            actions.setdefault(record['job_id'], 'duplicate-candidate')
                            self._review(record)
            if registry != before or not self.path.exists():
                self._commit(before, registry, actions, host, files)
            reviews = sum(job['job_id'] in affected and job['review_status'] == 'needs_review'
                          for job in registry['jobs'])
            jobs = [{'job_id': record['job_id'], 'result': result, 'review_status': record['review_status']}
                    for record, result in results]
        return self._receipt(added, updated, duplicates, reviews, jobs)

    def _update(self, job_id, host, change, action):
        """Apply change(row) under the jobs lock; commit only when the row changed."""
        with locked(self.base):
            before = self._load()
            self._catch_up(before)
            registry = copy.deepcopy(before)
            row = next((r for r in registry['jobs'] if r['job_id'] == job_id), None)
            if row is None:
                raise ValueError('Unknown job_id: ' + str(job_id))
            result = change(row)
            if registry != before:
                row['updated_at'], row['updated_by'] = now(), host
                self._commit(before, registry, {job_id: action}, host)
            return row, result

    def set_stage(self, job_id, stage, host='unknown', basis='user'):
        """The user's stage for a job; it must be one of config.json tracker.stages."""
        platform(host)
        stage = canonical_stage(stage, stages(self.ws))

        def change(row):
            previous = row['workflow_status']
            if previous != stage:
                row['workflow_status'] = stage
                row['history'].append({'action': 'stage', 'at': now(), 'platform': host, 'basis': basis,
                                       'from': previous, 'to': stage})
            return previous

        row, previous = self._update(job_id, host, change, 'stage')
        return {'job_id': job_id, 'stage': row['workflow_status'], 'previous': previous,
                'changed': previous != row['workflow_status']}

    def set_notes(self, job_id, text, host='unknown', basis='user'):
        """The user's own notes for a job (kept apart from notes captured from the listing)."""
        platform(host)
        if text is not None and not isinstance(text, str):
            raise ValueError('Notes must be text')
        value = text if text is not None and text.strip() else None
        if value is not None and len(value) > MAX_NOTES:
            raise ValueError('Notes are limited to %d characters' % MAX_NOTES)

        def change(row):
            previous = row.get('user_notes')
            if previous != value:
                row['user_notes'] = value
                row['history'].append({'action': 'notes', 'at': now(), 'platform': host, 'basis': basis,
                                       'sha256': _sha(value.encode('utf-8')) if value is not None else None})
            return previous != value

        row, changed = self._update(job_id, host, change, 'notes')
        return {'job_id': job_id, 'user_notes': row.get('user_notes'), 'changed': changed}

    def record_submission(self, job_id, application_id, submission, host):
        """Record an authorized, evidenced submission on the job and its application record."""
        platform(host)
        clean = validate_submission(submission)
        if not isinstance(application_id, str) or not APP_ID.fullmatch(application_id):
            raise ValueError('Invalid application_id')
        store = Store(self.ws, test=self.test)
        directory = store.directory(application_id)
        if not self._application_exists(application_id):
            raise ValueError('application_id must link to an existing local application')
        if clean.get('approval_id'):
            import approvals
            try:
                approvals.load(self.ws, clean['approval_id'])  # exists and untampered
            except (OSError, ValueError) as error:
                raise ValueError('approval_id does not name a valid approval record: %s' % error)
        allowed = stages(self.ws)
        digest_value = _sha(_bytes(clean))
        event_id = 'SUB-' + _sha(('%s\0%s\0%s' % (job_id, application_id, digest_value)).encode())[:24]
        outcome = {}

        def change(row):
            if row.get('application_id') not in (None, application_id):
                raise ValueError('%s is linked to %s; record the submission there or fix the link first'
                                 % (job_id, row['application_id']))
            record = store.load(application_id)
            if clean.get('cv_output_id'):
                output = next((o for o in record['outputs'] if o['output_id'] == clean['cv_output_id']), None)
                if output is None or output['kind'] != 'cv-pdf' or output.get('validation_status') != 'passed':
                    raise ValueError('cv_output_id must name a validated CV PDF of this application')
            status_before = record['status']
            if status_before in BEYOND_SUBMITTED:
                outcome['application_event'] = 'skipped: application is already ' + status_before
            else:
                proof = dict(clean['evidence'], channel=clean['channel'], submitted_at=clean['submitted_at'])
                event = {'type': 'status', 'text': 'Application submitted via ' + clean['channel'],
                         'channel': clean['channel'], 'basis': 'authorized-submission', 'status': 'submitted',
                         'event_id': event_id, 'evidence': proof}
                seen = any(e.get('event_id') == event_id for e in record['interactions'])
                store.record_event(application_id, event, host)  # idempotent through event_id
                outcome['application_event'] = 'already-recorded' if seen else 'recorded'
            outcome['application_status'] = store.load(application_id)['status']
            if any(h.get('action') == 'application-submitted' and h.get('submission_sha256') == digest_value
                   for h in row['history']):
                outcome['duplicate'] = True
                return
            outcome['duplicate'] = False
            row['application_id'] = application_id
            entry = {'action': 'application-submitted', 'at': now(), 'platform': host,
                     'application_id': application_id, 'channel': clean['channel'],
                     'submitted_at': clean['submitted_at'], 'authorized_by_user': True,
                     'user_request': clean['user_request'], 'evidence': clean['evidence'],
                     'cv_output_id': clean.get('cv_output_id'), 'approval_id': clean.get('approval_id'),
                     'submission_sha256': digest_value, 'application_event_id': event_id,
                     'application_status_before': status_before}
            row['history'].append(entry)
            previous = row['workflow_status']
            if _advance(row, SUBMITTED_STAGE, allowed):
                row['history'].append({'action': 'stage', 'at': entry['at'], 'platform': host,
                                       'basis': 'authorized-submission', 'from': previous,
                                       'to': row['workflow_status']})
            elif SUBMITTED_STAGE.casefold() not in [s.casefold() for s in allowed]:
                outcome['stage_note'] = '"%s" is not one of the tracker stages; stage left as %s' % (
                    SUBMITTED_STAGE, previous)

        # Lock order: application, then the jobs registry.
        with locked(directory):
            row, _ = self._update(job_id, host, change, 'application-submitted')
        return dict({'job_id': job_id, 'application_id': application_id, 'stage': row['workflow_status'],
                     'submission_sha256': digest_value}, **outcome)


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    parser.add_argument('--test', action='store_true', help='use fictional sandbox storage, never the real registry')
    sub = parser.add_subparsers(dest='command', required=True)
    helps = {'init': 'create an empty registry', 'ingest': 'validate and save a batch of listings',
             'show': 'print the registry or one job', 'export': 'refresh jobs/outputs/Tracker.xlsx',
             'set-stage': "set the user's stage for a job", 'set-notes': "set the user's notes for a job",
             'record-submission': 'record an authorized, evidenced application submission',
             'history': 'print the row-change journal'}
    for name, text in helps.items():
        child = sub.add_parser(name, help=text)
        # Accept storage flags before or after the subcommand.
        child.add_argument('--workspace', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        child.add_argument('--test', action='store_true', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        if name in ('ingest', 'record-submission'):
            child.add_argument('--file', type=Path, required=True)
            child.add_argument('--platform', choices=PLATFORMS, required=True)
        elif name in ('init', 'export', 'set-stage', 'set-notes'):
            child.add_argument('--platform', choices=PLATFORMS, default='unknown')
        if name in ('show', 'history'):
            child.add_argument('--job-id')
        if name in ('set-stage', 'set-notes', 'record-submission'):
            child.add_argument('--job-id', required=True)
        if name == 'set-stage':
            child.add_argument('--stage', required=True)
        if name == 'set-notes':
            child.add_argument('--text', required=True, help="the notes; an empty string clears them")
        if name == 'record-submission':
            child.add_argument('--application-id', required=True)
    args = parser.parse_args()
    store = JobStore(args.workspace, args.test)
    if args.command == 'init':
        result = store.init(args.platform)
    elif args.command == 'show':
        result = store.show(args.job_id)
    elif args.command == 'history':
        result = store.history(args.job_id)
    elif args.command == 'export':
        import tracker
        result = tracker.export(store.ws, host=args.platform, test=store.test)
    elif args.command == 'set-stage':
        result = store.set_stage(args.job_id, args.stage, args.platform)
    elif args.command == 'set-notes':
        result = store.set_notes(args.job_id, args.text, args.platform)
    elif args.command == 'record-submission':
        result = store.record_submission(args.job_id, args.application_id, json.loads(read_text(args.file)),
                                         args.platform)
    else:
        result = store.ingest(json.loads(read_text(args.file)), args.platform, args.file.resolve().parent)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        raise SystemExit('Job intake failed: ' + str(error))
