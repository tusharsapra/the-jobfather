"""Outreach records (jobs/outreach.json): what was drafted, sent, bounced, deleted or cancelled.

Nothing here sends mail. A draft is recorded from an applications.py draft receipt or the host's
verified read-back. A send needs the mailbox's own record of the message: the provider's link,
the SENT label and the user's account (profile/identity.json mail.account; there is no default).
Generated is not sent, SENT is not delivered, and a deleted draft is never a send.

Lock: the jobs folder (shared with the job registry). Lock order across the job tools:
pipeline, then an application, then the jobs folder (registry and outreach).
"""
import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import sys
from urllib.parse import quote, urlsplit
import uuid

# CLI execution and imports from job_pipeline.py/tracker.py must share one module.
if __name__ == '__main__':
    sys.modules['outreach'] = sys.modules[__name__]

from applications import PROVIDERS, Store
import job_intake
from job_intake import APP_ID, CLOCK_SKEW, JOB_ID, parse_time, valid_email
import layout
from local import (PLATFORMS, add_workspace_arg, atomic_json, contained, digest, identity, locked, now, platform,
                   read_json, read_text, require_workspace)

STATUSES = ('drafted', 'sent', 'bounced', 'draft-deleted', 'cancelled')
TYPES = ('application', 'enquiry', 'follow_up', 'referral')
CHANNELS = ('email',)
OUTREACH_ID = re.compile(r'OUT-[0-9a-f]{12}')
DETAIL_FIELDS = ('channel', 'company', 'recipient', 'roles', 'job_ids', 'application_id', 'outreach_type', 'subject')
DRAFT_FIELDS = ('provider', 'draft_id', 'message_id', 'thread_id')
DRAFTED_INPUT = set(DETAIL_FIELDS) | {'draft', 'application_draft', 'notes', 'next_action'}
SENT_INPUT = set(DETAIL_FIELDS) | {'outreach_id', 'evidence', 'notes', 'next_action'}
EVIDENCE_FIELDS = {'provider', 'message_id', 'thread_id', 'sent_at', 'url', 'sender', 'recipients', 'labels',
                   'capture_sha256'}
SET_FIELDS = ('next_action', 'follow_up_override', 'notes')
SET_BASES = ('user', 'tracker-edit')
DELETE_BASES = ('user-reported', 'connector-verified')
LIMITS = {'next_action': 1000, 'notes': job_intake.MAX_NOTES}
DRAFT_KINDS = {'email': 'application', 'cover-letter': 'application', 'follow-up': 'follow_up'}


def mail_account(ws):
    """(account, provider) the user sends from. Refuses when identity.json has no mail.account."""
    mail = identity(ws).get('mail') or {}
    account = mail.get('account')
    if not isinstance(account, str) or not valid_email(account.strip()):
        raise ValueError('Set mail.account in profile/identity.json (the mailbox you send from) before '
                         'recording or reconciling sent mail; there is no default account')
    provider = mail.get('provider') or 'gmail'
    if provider not in PROVIDERS:
        raise ValueError('Unsupported mail provider %r; supported: %s' % (provider, ', '.join(PROVIDERS)))
    return account.strip(), provider


def mail_link(account, message_id, provider='gmail'):
    """The provider's own link to one message in the right signed-in account (never u/0)."""
    if provider != 'gmail':
        raise ValueError('No message links for provider %r' % provider)
    return 'https://mail.google.com/mail/?authuser=%s#all/%s' % (quote(account, safe='@'), quote(message_id, safe=''))


def provider_link(url, provider):
    parts = urlsplit(url) if isinstance(url, str) else None
    return bool(parts and parts.scheme == 'https' and parts.hostname in PROVIDERS[provider]['hosts'])


def follow_up_value(value):
    """None, a calendar date (YYYY-MM-DD, due from local midnight) or a timestamp with a timezone."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError('A follow-up time needs a timezone')
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValueError('Follow-up date must be YYYY-MM-DD or an ISO timestamp with a timezone')
    text = value.strip()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            raise ValueError('Follow-up date is not a real date: ' + text)
    return parse_time(text, 'Follow-up date').isoformat()


def _text(value, what, required=False, limit=None):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(what + ' is required')
        return None
    if not isinstance(value, str):
        raise ValueError(what + ' must be text')
    if limit and len(value) > limit:
        raise ValueError('%s is limited to %d characters' % (what, limit))
    return value.strip()


def _same(left, right):
    return ' '.join(str(left or '').split()).casefold() == ' '.join(str(right or '').split()).casefold()


class OutreachStore:
    """jobs/outreach.json under the jobs-folder lock."""

    def __init__(self, ws=None, test=False):
        self.ws = require_workspace(ws)
        self.test = bool(test)
        self.base = contained(self.ws, layout.sandboxed(layout.JOBS, self.test))
        self.path = contained(self.ws, layout.sandboxed(layout.OUTREACH, self.test))
        self.jobs = job_intake.JobStore(self.ws, self.test)
        self.applications = Store(self.ws, test=self.test)

    # -------------------------------------------------------------- reading
    def load(self):
        if not self.path.exists():
            return {'schema_version': 1, 'outreach': []}
        value = read_json(self.path)
        if not isinstance(value, dict) or value.get('schema_version') != 1 or not isinstance(value.get('outreach'), list):
            raise ValueError('Unsupported or malformed outreach store')
        ids = []
        for record in value['outreach']:
            if not isinstance(record, dict) or not OUTREACH_ID.fullmatch(str(record.get('outreach_id', ''))):
                raise ValueError('Outreach store contains an invalid outreach ID')
            if record.get('status') not in STATUSES:
                raise ValueError('Outreach %s has an unknown status' % record['outreach_id'])
            ids.append(record['outreach_id'])
        if len(ids) != len(set(ids)):
            raise ValueError('Outreach store contains duplicate IDs')
        return value

    def get(self, outreach_id):
        return self._find(self.load()['outreach'], outreach_id)

    @staticmethod
    def _find(records, outreach_id):
        if not isinstance(outreach_id, str) or not OUTREACH_ID.fullmatch(outreach_id):
            raise ValueError('Invalid outreach ID: %r' % (outreach_id,))
        record = next((r for r in records if r['outreach_id'] == outreach_id), None)
        if record is None:
            raise ValueError('Unknown outreach ID: ' + outreach_id)
        return record

    def list(self, company=None, recipient=None, job_id=None, status=None, outreach_type=None):
        result = []
        for record in self.load()['outreach']:
            if company and ' '.join(company.split()).casefold() not in ' '.join(record['company'].split()).casefold():
                continue
            if recipient and record['recipient'].casefold() != recipient.strip().casefold():
                continue
            if job_id and job_id not in record['job_ids']:
                continue
            if status and record['status'] != status:
                continue
            if outreach_type and record['outreach_type'] != outreach_type:
                continue
            result.append(record)
        return result

    # -------------------------------------------------------------- validation
    def _details(self, data):
        channel = data.get('channel') or 'email'
        if channel not in CHANNELS:
            raise ValueError('channel must be one of: ' + ', '.join(CHANNELS))
        recipient = _text(data.get('recipient'), 'recipient', required=True)
        if not valid_email(recipient):
            raise ValueError('recipient must be one verified email address')
        roles = data.get('roles') or []
        if not isinstance(roles, list) or any(not isinstance(r, str) or not r.strip() for r in roles):
            raise ValueError('roles must be a list of role titles')
        job_ids = data.get('job_ids') or []
        if not isinstance(job_ids, list) or any(not isinstance(j, str) or not JOB_ID.fullmatch(j) for j in job_ids):
            raise ValueError('job_ids must be a list of JOB-… IDs')
        known = {row['job_id'] for row in self.jobs.show()['jobs']} if job_ids else set()
        missing = [j for j in job_ids if j not in known]
        if missing:
            raise ValueError('Unknown job IDs: ' + ', '.join(missing))
        application_id = data.get('application_id')
        if application_id is not None:
            if not isinstance(application_id, str) or not APP_ID.fullmatch(application_id):
                raise ValueError('Invalid application_id')
            if not (self.applications.directory(application_id) / 'record.json').is_file():
                raise ValueError('application_id must link to an existing local application')
        if data.get('outreach_type') not in TYPES:
            raise ValueError('outreach_type must be one of: ' + ', '.join(TYPES))
        return {'channel': channel, 'company': _text(data.get('company'), 'company', required=True),
                'recipient': recipient, 'roles': [r.strip() for r in roles], 'job_ids': list(dict.fromkeys(job_ids)),
                'application_id': application_id, 'outreach_type': data['outreach_type'],
                'subject': _text(data.get('subject'), 'subject', required=True)}

    @staticmethod
    def _draft(value):
        if not isinstance(value, dict) or set(value) - set(DRAFT_FIELDS):
            raise ValueError('draft needs exactly: ' + ', '.join(DRAFT_FIELDS))
        if value.get('provider') not in PROVIDERS:
            raise ValueError('draft.provider must be one of: ' + ', '.join(PROVIDERS))
        return {key: _text(value.get(key), 'draft.' + key, required=True) for key in DRAFT_FIELDS}

    def _from_receipt(self, data):
        """Fill and cross-check a draft from an applications.py draft receipt."""
        ref = data['application_draft']
        if not isinstance(ref, dict) or set(ref) != {'application_id', 'output_id'}:
            raise ValueError('application_draft needs application_id and output_id')
        app_id = ref['application_id']
        if not isinstance(app_id, str) or not APP_ID.fullmatch(app_id):
            raise ValueError('application_draft.application_id is invalid')
        record = self.applications.load(app_id)
        output = next((o for o in record['outputs'] if o['output_id'] == ref['output_id']), None)
        if output is None or output['kind'] not in DRAFT_KINDS:
            raise ValueError('application_draft.output_id must be an email, follow-up or cover-letter output of ' + app_id)
        supplied = data.get('draft') or {}
        drafts = output.get('provider_drafts') or []
        if supplied.get('draft_id'):
            drafts = [d for d in drafts if d['draft_id'] == supplied['draft_id']
                      and d['provider'] == supplied.get('provider', d['provider'])]
        if len(drafts) != 1:
            raise ValueError('Record the mail draft with applications.py record-draft first, and name its draft_id '
                             'when the output has several drafts')
        item = drafts[0]
        relative = self.applications.directory(app_id).relative_to(self.ws).as_posix() + '/' + item['receipt']
        path = contained(self.ws, relative)
        if not path.is_file() or digest(path) != item['receipt_sha256']:
            raise ValueError('Draft receipt integrity failed: ' + relative)
        receipt = read_json(path)
        draft = {key: receipt.get(key) for key in DRAFT_FIELDS}
        for key, value in supplied.items():
            if key in DRAFT_FIELDS and value != draft[key]:
                raise ValueError('draft.%s does not match the recorded receipt' % key)
        to = [address.casefold() for address in receipt.get('to') or []]
        if data.get('recipient') and data['recipient'].strip().casefold() not in to:
            raise ValueError('recipient is not an addressee of the recorded draft')
        if data.get('subject') and not _same(data['subject'], receipt.get('subject')):
            raise ValueError('subject does not match the recorded draft')
        if data.get('application_id') not in (None, app_id):
            raise ValueError('application_id does not match application_draft')
        job_ids = data.get('job_ids')
        if job_ids is None:
            job_ids = [row['job_id'] for row in self.jobs.show()['jobs'] if row.get('application_id') == app_id]
        merged = dict(data, application_id=app_id, job_ids=job_ids,
                      recipient=data.get('recipient') or (receipt['to'][0] if len(receipt.get('to') or []) == 1 else None),
                      subject=data.get('subject') or receipt.get('subject'),
                      company=data.get('company') or record['company'],
                      roles=data.get('roles') if data.get('roles') is not None else [record['role']],
                      outreach_type=data.get('outreach_type') or DRAFT_KINDS[output['kind']])
        return merged, draft, relative, item['receipt_sha256']

    def _evidence(self, value, account, provider):
        if not isinstance(value, dict) or set(value) != EVIDENCE_FIELDS:
            raise ValueError('Sent evidence needs exactly: ' + ', '.join(sorted(EVIDENCE_FIELDS)))
        if value['provider'] != provider:
            raise ValueError('Sent evidence must come from the configured mail provider (%s)' % provider)
        for key in ('message_id', 'thread_id'):
            _text(value[key], 'evidence.' + key, required=True)
        sent_at = parse_time(value['sent_at'], 'evidence.sent_at')
        if sent_at > datetime.now(timezone.utc) + CLOCK_SKEW:
            raise ValueError('evidence.sent_at is in the future')
        if not provider_link(value['url'], provider):
            raise ValueError("Sent evidence needs the provider's own https link to the message")
        if not isinstance(value['sender'], str) or value['sender'].strip().casefold() != account.casefold():
            raise ValueError('The sent message must come from the configured account (identity.json mail.account)')
        recipients = value['recipients']
        if not isinstance(recipients, list) or not recipients or any(not valid_email(r) for r in recipients):
            raise ValueError('evidence.recipients must list the real recipient addresses')
        labels = value['labels']
        if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
            raise ValueError('evidence.labels must be the label list from the mailbox')
        if PROVIDERS[provider]['sent_label'] not in labels or 'DRAFT' in labels:
            raise ValueError('Only a message with the %s label (not a draft) proves a send'
                             % PROVIDERS[provider]['sent_label'])
        if not isinstance(value['capture_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['capture_sha256']):
            raise ValueError('evidence.capture_sha256 must be the SHA-256 of the captured message')
        return {'message_id': value['message_id'].strip(), 'thread_id': value['thread_id'].strip(),
                'sent_at': value['sent_at'].strip(), 'url': value['url'], 'capture_sha256': value['capture_sha256']}

    @staticmethod
    def _warnings(records, details, outreach_id=None):
        notes = []
        for other in records:
            if other['outreach_id'] == outreach_id or other['recipient'].casefold() != details['recipient'].casefold():
                continue
            if other['status'] == 'bounced':
                notes.append('Mail to %s bounced before (%s); verify a new contact first' % (other['recipient'], other['outreach_id']))
            elif other['status'] == 'draft-deleted' and _same(other['company'], details['company']):
                notes.append('The user deleted an earlier draft to this recipient (%s)' % other['outreach_id'])
            elif other['status'] == 'sent' and set(other['job_ids']) & set(details['job_ids']):
                notes.append('Already sent to this recipient for the same job (%s); do not repeat a send' % other['outreach_id'])
        return notes

    def _save(self, store):
        atomic_json(self.path, store)

    # -------------------------------------------------------------- writing
    def record_drafted(self, data, host):
        """Record an unsent provider draft. Idempotent on provider + draft_id."""
        platform(host)
        if not isinstance(data, dict) or set(data) - DRAFTED_INPUT:
            raise ValueError('Draft record fields: ' + ', '.join(sorted(DRAFTED_INPUT)))
        receipt, receipt_sha = None, None
        if data.get('application_draft') is not None:
            data, draft, receipt, receipt_sha = self._from_receipt(data)
        else:
            draft = self._draft(data.get('draft'))
        draft = self._draft(draft)
        notes = _text(data.get('notes'), 'notes', limit=LIMITS['notes'])
        next_action = _text(data.get('next_action'), 'next_action', limit=LIMITS['next_action'])
        with locked(self.base):
            details = self._details(data)
            store = self.load()
            records = store['outreach']
            existing = next((r for r in records if r.get('draft') and r['draft']['provider'] == draft['provider']
                             and r['draft']['draft_id'] == draft['draft_id']), None)
            draft_value = dict(draft, receipt=receipt, receipt_sha256=receipt_sha)
            if existing:
                if existing['status'] != 'drafted':
                    raise ValueError('Draft %s is already recorded as %s (%s)' % (draft['draft_id'], existing['status'],
                                                                                existing['outreach_id']))
                for key in ('channel', 'company', 'recipient', 'outreach_type', 'application_id'):
                    if not _same(existing[key], details[key]):
                        raise ValueError('Draft %s is recorded for a different %s; record a new draft instead'
                                         % (draft['draft_id'], key))
                changes = {key: details[key] for key in ('roles', 'job_ids', 'subject') if existing[key] != details[key]}
                if existing['draft'] != draft_value:
                    changes['draft'] = draft_value
                if not changes:
                    return {'outreach': existing, 'created': False, 'changed': False,
                            'warnings': self._warnings(records, details, existing['outreach_id'])}
                existing.update(changes)
                existing['history'].append({'at': now(), 'action': 'draft-updated', 'platform': host,
                                            'basis': 'draft-receipt' if receipt else 'host-verified',
                                            'fields': sorted(changes)})
                self._save(store)
                return {'outreach': existing, 'created': False, 'changed': True,
                        'warnings': self._warnings(records, details, existing['outreach_id'])}
            taken = {r['outreach_id'] for r in records}
            outreach_id = 'OUT-' + uuid.uuid4().hex[:12]
            while outreach_id in taken:
                outreach_id = 'OUT-' + uuid.uuid4().hex[:12]
            at = now()
            record = dict({'outreach_id': outreach_id, 'created_at': at}, **details)
            record.update(status='drafted', draft=draft_value, sent=None, next_action=next_action,
                          follow_up_override=None, notes=notes,
                          history=[{'at': at, 'action': 'drafted', 'platform': host,
                                    'basis': 'draft-receipt' if receipt else 'host-verified',
                                    'draft_id': draft['draft_id']}])
            warnings = self._warnings(records, details)
            records.append(record)
            self._save(store)
        return {'outreach': record, 'created': True, 'changed': True, 'warnings': warnings}

    def record_sent(self, data, host):
        """Record a send proven by the mailbox (provider link, SENT label, the user's account)."""
        platform(host)
        account, provider = mail_account(self.ws)
        if not isinstance(data, dict) or set(data) - SENT_INPUT:
            raise ValueError('Sent record fields: ' + ', '.join(sorted(SENT_INPUT)))
        sent = self._evidence(data.get('evidence'), account, provider)
        recipients = {r.strip().casefold() for r in data['evidence']['recipients']}
        history = {'action': 'sent', 'platform': host, 'basis': 'connector-verified', 'provider': provider,
                   'labels': sorted(set(data['evidence']['labels']))}
        with locked(self.base):
            store = self.load()
            records = store['outreach']
            same = next((r for r in records if (r.get('sent') or {}).get('message_id') == sent['message_id']), None)
            if data.get('outreach_id') is not None:
                record = self._find(records, data['outreach_id'])
                for key in DETAIL_FIELDS:
                    if key in data and not _same(record[key], data[key]):
                        raise ValueError('%s does not match %s' % (key, record['outreach_id']))
                if record['status'] in ('sent', 'bounced'):
                    if record['sent']['message_id'] == sent['message_id']:
                        return {'outreach': record, 'created': False, 'changed': False, 'warnings': []}
                    raise ValueError('%s is already recorded as sent (message %s)'
                                     % (record['outreach_id'], record['sent']['message_id']))
                if record['status'] != 'drafted':
                    raise ValueError('%s is %s: a deleted or cancelled draft is not a send. Record the message that '
                                     'was actually sent without outreach_id.' % (record['outreach_id'], record['status']))
                if same is not None:
                    raise ValueError('Message %s is already recorded for %s' % (sent['message_id'], same['outreach_id']))
                if record['recipient'].casefold() not in recipients:
                    raise ValueError('The sent message is not addressed to ' + record['recipient'])
                record.update(status='sent', sent=sent)
                for key in ('notes', 'next_action'):
                    if data.get(key) is not None:
                        record[key] = _text(data[key], key, limit=LIMITS[key])
                record['history'].append(dict(history, at=now(), message_id=sent['message_id']))
                self._save(store)
                return {'outreach': record, 'created': False, 'changed': True, 'warnings': []}
            details = self._details(data)
            if same is not None:
                if any(not _same(same[key], details[key]) for key in ('company', 'recipient', 'outreach_type')):
                    raise ValueError('Message %s is already recorded for %s with other details'
                                     % (sent['message_id'], same['outreach_id']))
                return {'outreach': same, 'created': False, 'changed': False, 'warnings': []}
            if details['recipient'].casefold() not in recipients:
                raise ValueError('The sent message is not addressed to ' + details['recipient'])
            taken = {r['outreach_id'] for r in records}
            outreach_id = 'OUT-' + uuid.uuid4().hex[:12]
            while outreach_id in taken:
                outreach_id = 'OUT-' + uuid.uuid4().hex[:12]
            at = now()
            record = dict({'outreach_id': outreach_id, 'created_at': at}, **details)
            record.update(status='sent', draft=None, sent=sent,
                          next_action=_text(data.get('next_action'), 'next_action', limit=LIMITS['next_action']),
                          follow_up_override=None, notes=_text(data.get('notes'), 'notes', limit=LIMITS['notes']),
                          history=[dict(history, at=at, message_id=sent['message_id'])])
            warnings = self._warnings(records, details)
            records.append(record)
            self._save(store)
        return {'outreach': record, 'created': True, 'changed': True, 'warnings': warnings}

    def record_deleted(self, outreach_id, host='unknown', reason='deleted', basis='user-reported', note=None):
        """A draft that was deleted (or an outreach the user cancelled) before sending."""
        platform(host)
        if reason not in ('deleted', 'cancelled'):
            raise ValueError('reason must be deleted or cancelled')
        if basis not in DELETE_BASES:
            raise ValueError('basis must be one of: ' + ', '.join(DELETE_BASES))
        note = _text(note, 'note', limit=1000)
        status = 'draft-deleted' if reason == 'deleted' else 'cancelled'
        with locked(self.base):
            store = self.load()
            record = self._find(store['outreach'], outreach_id)
            if record['status'] == status:
                return {'outreach': record, 'changed': False}
            if record['status'] != 'drafted':
                raise ValueError('%s is %s; only an unsent draft can be deleted or cancelled'
                                 % (record['outreach_id'], record['status']))
            record['status'] = status
            record['history'].append({'at': now(), 'action': status, 'platform': host, 'basis': basis, 'note': note})
            self._save(store)
        return {'outreach': record, 'changed': True}

    def set(self, outreach_id, host='unknown', basis='user', **fields):
        """The user's next action, follow-up date and notes; an empty value clears a field."""
        platform(host)
        if basis not in SET_BASES:
            raise ValueError('basis must be one of: ' + ', '.join(SET_BASES))
        unknown = set(fields) - set(SET_FIELDS)
        if unknown or not fields:
            raise ValueError('Set one or more of: ' + ', '.join(SET_FIELDS))
        values = {}
        for key, value in fields.items():
            values[key] = follow_up_value(value) if key == 'follow_up_override' else _text(value, key, limit=LIMITS[key])
        with locked(self.base):
            store = self.load()
            record = self._find(store['outreach'], outreach_id)
            changed = sorted(key for key, value in values.items() if record.get(key) != value)
            if changed:
                record.update({key: values[key] for key in changed})
                record['history'].append({'at': now(), 'action': 'set', 'platform': host, 'basis': basis,
                                          'fields': changed})
                self._save(store)
        return {'outreach': record, 'changed': changed}

    def mark_bounced(self, outreach_id, bounce, host):
        """A verified delivery failure for a sent message (from job_pipeline.py). Idempotent."""
        platform(host)
        with locked(self.base):
            store = self.load()
            record = self._find(store['outreach'], outreach_id)
            if any(h.get('action') == 'bounced' and h.get('message_id') == bounce['message_id']
                   for h in record['history']):
                return {'outreach': record, 'changed': False}
            if record['status'] != 'sent':
                return {'outreach': record, 'changed': False}
            record['status'] = 'bounced'
            record['history'].append({'at': now(), 'action': 'bounced', 'platform': host, 'basis': 'connector-verified',
                                      'message_id': bounce['message_id'], 'occurred_at': bounce['occurred_at'],
                                      'quote': bounce['quote'], 'url': bounce['url']})
            self._save(store)
        return {'outreach': record, 'changed': True}


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    parser.add_argument('--test', action='store_true', help='use fictional sandbox storage')
    sub = parser.add_subparsers(dest='command', required=True)
    helps = {'record-drafted': 'record an unsent provider draft (--file JSON)',
             'record-sent': 'record a send proven by the mailbox (--file JSON)',
             'record-deleted': 'record that a draft was deleted or the outreach cancelled',
             'set': "set the user's next action, follow-up date or notes",
             'list': 'list outreach records', 'show': 'print one outreach record'}
    for name, text in helps.items():
        child = sub.add_parser(name, help=text)
        child.add_argument('--workspace', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        child.add_argument('--test', action='store_true', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        if name in ('record-drafted', 'record-sent'):
            child.add_argument('--file', type=Path, required=True)
            child.add_argument('--platform', choices=PLATFORMS, required=True)
        if name in ('record-deleted', 'set', 'show'):
            child.add_argument('--outreach-id', required=True)
        if name in ('record-deleted', 'set'):
            child.add_argument('--platform', choices=PLATFORMS, default='unknown')
        if name == 'record-deleted':
            child.add_argument('--cancelled', action='store_true', help='the user cancelled it rather than deleting a draft')
            child.add_argument('--basis', choices=DELETE_BASES, default='user-reported')
            child.add_argument('--note')
        if name == 'set':
            child.add_argument('--next-action', default=argparse.SUPPRESS)
            child.add_argument('--follow-up-date', dest='follow_up_override', default=argparse.SUPPRESS,
                               help='YYYY-MM-DD or an ISO timestamp with a timezone; empty clears it')
            child.add_argument('--notes', default=argparse.SUPPRESS)
        if name == 'list':
            child.add_argument('--company')
            child.add_argument('--recipient')
            child.add_argument('--job-id')
            child.add_argument('--status', choices=STATUSES)
            child.add_argument('--type', dest='outreach_type', choices=TYPES)
    args = parser.parse_args()
    store = OutreachStore(args.workspace, args.test)
    if args.command == 'record-drafted':
        result = store.record_drafted(json.loads(read_text(args.file)), args.platform)
    elif args.command == 'record-sent':
        result = store.record_sent(json.loads(read_text(args.file)), args.platform)
    elif args.command == 'record-deleted':
        result = store.record_deleted(args.outreach_id, args.platform, 'cancelled' if args.cancelled else 'deleted',
                                      args.basis, args.note)
    elif args.command == 'set':
        fields = {key: getattr(args, key) for key in SET_FIELDS if hasattr(args, key)}
        result = store.set(args.outreach_id, args.platform, **fields)
    elif args.command == 'show':
        result = store.get(args.outreach_id)
    else:
        result = store.list(args.company, args.recipient, args.job_id, args.status, args.outreach_type)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        raise SystemExit('Outreach operation failed: ' + str(error))
