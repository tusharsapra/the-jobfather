"""Versioned local application records. The host assistant does the writing; this file stores it safely."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid
from urllib.parse import urlsplit

if __name__ == '__main__':
    sys.modules['applications'] = sys.modules[__name__]

import evidence
import layout
import managed
import market
from check_text import check
from local import (add_workspace_arg, atomic_json, atomic_text, contained, context_revision, digest, locked, now,
                   platform, read_json, read_text, require_workspace, sha_json)

KINDS = ('fit', 'forms', 'cover-letter', 'linkedin', 'email', 'follow-up', 'humanize',
         'cv-content', 'cv-review', 'application-pack')
STATUSES = ('draft', 'user-approved', 'submitted', 'interview', 'rejected', 'withdrawn', 'offer')
BASES = ('user-reported', 'authorized-send-result', 'connector-verified', 'authorized-submission')
KIND_GROUP = {'cv-content': 'cv', 'cv-review': 'cv', 'forms': 'forms', 'fit': 'fit'}
PROVIDERS = {'gmail': {'hosts': ('mail.google.com',), 'sent_label': 'SENT'}}
SIGNALS = {'acknowledgement': 'submitted', 'interview': 'interview', 'offer': 'offer', 'rejection': 'rejected',
           'hr_question': None, 'assessment': None, 'bounce': None, 'follow_up': None, 'sent_reply': None,
           'other_reply': None}
SUBMISSION_EVIDENCE = ('confirmation-page', 'confirmation-email', 'sent-email', 'user-reported')


def connector_event_id(application_id, message_id):
    return 'MAIL-' + hashlib.sha256((application_id + '\0' + message_id).encode()).hexdigest()[:24]


def _aware(value, what):
    date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if date.tzinfo is None:
        raise ValueError(what + ' needs a timestamp with a timezone')
    return date


def validate_connector_event(application_id, event):
    """The host supplies real mailbox captures; a draft never proves a send."""
    e = event.get('evidence')
    required = {'provider', 'message_id', 'thread_id', 'url', 'occurred_at', 'signal', 'quote', 'capture_sha256'}
    if not isinstance(e, dict) or set(e) != required or e['provider'] not in PROVIDERS:
        raise ValueError('Connector event needs complete mailbox evidence from a supported provider')
    if any(not isinstance(e[k], str) or not e[k].strip() for k in required):
        raise ValueError('Connector evidence fields must be non-empty strings')
    url = urlsplit(e['url'])
    if url.scheme != 'https' or url.hostname not in PROVIDERS[e['provider']]['hosts']:
        raise ValueError('Connector evidence needs the provider\'s own https link')
    if not re.fullmatch('[a-f0-9]{64}', e['capture_sha256']):
        raise ValueError('Connector evidence needs a capture checksum')
    _aware(e['occurred_at'], 'Connector evidence')
    if event.get('event_id') != connector_event_id(application_id, e['message_id']):
        raise ValueError('Connector event identity does not match application/message')
    if e['signal'] not in SIGNALS or (event.get('status') and event['status'] != SIGNALS[e['signal']]):
        raise ValueError('Connector signal cannot set this application status')


def validate_submission_event(event):
    e = event.get('evidence')
    required = {'kind', 'channel', 'submitted_at'}
    if not isinstance(e, dict) or not required <= set(e) or e['kind'] not in SUBMISSION_EVIDENCE:
        raise ValueError('Submission evidence needs kind (%s), channel and submitted_at' % ', '.join(SUBMISSION_EVIDENCE))
    _aware(e['submitted_at'], 'Submission evidence')
    if e['kind'] != 'user-reported' and not (e.get('url') or e.get('quote') or e.get('message_id')):
        raise ValueError('Submission evidence needs a confirmation url, quote or message id')
    if event.get('status') != 'submitted':
        raise ValueError('An authorized submission sets status submitted')


class Store:
    def __init__(self, ws=None, test=False):
        self.ws = require_workspace(ws)
        self.test = test
        self.base = contained(self.ws, layout.sandboxed(layout.APPLICATIONS, test))

    def directory(self, application_id):
        if not re.fullmatch(r'APP-[0-9a-f]{12}', application_id):
            raise ValueError('Invalid application ID')
        return contained(self.base, application_id)

    def load(self, application_id):
        context_revision(self.ws)  # recover a pending application/context transaction first
        return read_json(self.directory(application_id) / 'record.json')

    def create(self, company, role, jd, source, host):
        platform(host)
        if not all(isinstance(x, str) and x.strip() for x in (company, role, jd, source)):
            raise ValueError('Company, role, JD and source are required')
        self.base.mkdir(parents=True, exist_ok=True)
        while True:
            app_id = 'APP-' + uuid.uuid4().hex[:12]
            directory = self.directory(app_id)
            try:
                directory.mkdir()
                break
            except FileExistsError:
                continue
        captured = now()
        atomic_text(directory / 'sources/jd-v001.txt', jd)
        record = {'schema_version': 1, 'application_id': app_id, 'fictional': self.test,
                  'company': company, 'role': role, 'created_at': captured, 'created_by': host, 'status': 'draft',
                  'contacts': [], 'analysis': None,
                  'jd_versions': [{'version': 1, 'path': 'sources/jd-v001.txt', 'source': source,
                                   'captured_at': captured, 'platform': host,
                                   'sha256': digest(directory / 'sources/jd-v001.txt')}],
                  'outputs': [], 'interactions': [], 'next_action': None, 'local_preferences': {},
                  'status_history': [{'status': 'draft', 'at': captured, 'platform': host, 'basis': 'record-created'}]}
        atomic_json(directory / 'record.json', record)
        return record

    def jd(self, app_id):
        record = self.load(app_id)
        path = contained(self.directory(app_id), record['jd_versions'][-1]['path'])
        if digest(path) != record['jd_versions'][-1]['sha256']:
            raise ValueError('Stored JD integrity failed')
        return read_text(path)

    def output_path(self, app_id, output_id):
        record = self.load(app_id)
        output = next((x for x in record['outputs'] if x['output_id'] == output_id), None)
        if not output:
            raise ValueError('Output does not belong to this application')
        base = self.ws if output.get('path_root') == 'workspace' else self.directory(app_id)
        path = contained(base, output['path'])
        if not path.is_file() or digest(path) != output['sha256']:
            raise ValueError('Saved output integrity failed')
        return {'path': str(path), 'kind': output['kind'], 'state': output['state'], 'version': output['version'],
                'validation_status': output.get('validation_status'), 'sha256': output['sha256']}

    def revise_jd(self, app_id, text, source, host):
        platform(host)
        if not text.strip() or not source.strip():
            raise ValueError('JD text and source are required')
        directory = self.directory(app_id)
        with locked(directory):
            record = self.load(app_id)
            version = len(record['jd_versions']) + 1
            relative = 'sources/jd-v%03d-%s.txt' % (version, uuid.uuid4().hex[:8])
            path = contained(directory, relative)
            atomic_text(path, text)
            record['jd_versions'].append({'version': version, 'path': relative, 'source': source,
                                          'captured_at': now(), 'platform': host, 'sha256': digest(path)})
            record['analysis'] = None
            atomic_json(directory / 'record.json', record)
        return record

    def save_analysis(self, app_id, analysis, host, expected_revision=None):
        platform(host)
        basis = context_revision(self.ws)
        if expected_revision:
            managed.assert_fresh(self.ws, expected_revision, 'fit', 'This analysis')
        required = {'employer_type', 'role_track', 'supporting_excerpts', 'requirements', 'fit_summary',
                    'selected_evidence_ids', 'gaps', 'emphasis'}
        if set(analysis) != required:
            raise ValueError('Analysis fields must be: ' + ', '.join(sorted(required)))
        tax = market.taxonomy(self.ws)
        for dim in ('employer_type', 'role_track'):
            allowed = set(tax['dimensions'][dim]) | {'unclear'}
            if analysis[dim] not in allowed:
                raise ValueError('Invalid %s %r; allowed (%s): %s' % (dim, analysis[dim], tax['source'], ', '.join(sorted(allowed))))
        for key in ('supporting_excerpts', 'requirements', 'selected_evidence_ids', 'gaps'):
            if not isinstance(analysis[key], list):
                raise ValueError(key + ' must be a list')
        for key in ('fit_summary', 'emphasis'):
            if not isinstance(analysis[key], str) or not analysis[key].strip():
                raise ValueError(key + ' must be non-empty text')
        cat = evidence.load(self.ws)
        if not analysis['selected_evidence_ids']:
            raise ValueError('Select at least one evidence ID')
        evidence.require_known(analysis['selected_evidence_ids'], cat)
        jd = self.jd(app_id)
        if not analysis['supporting_excerpts'] or any(not isinstance(x, str) or not x or x not in jd
                                                     for x in analysis['supporting_excerpts']):
            raise ValueError('Classification excerpts must occur in this stored JD')
        for req in analysis['requirements']:
            if set(req) != {'requirement', 'evidence_ids', 'assessment'} or not req['requirement']:
                raise ValueError('Requirement needs requirement, evidence_ids and assessment')
            if req['assessment'] not in ('supported', 'partial', 'gap'):
                raise ValueError('Invalid requirement assessment')
            if not isinstance(req['evidence_ids'], list):
                raise ValueError('Requirement evidence_ids must be a list')
            evidence.require_known(req['evidence_ids'], cat, what='Requirement evidence')
        directory = self.directory(app_id)
        with locked(directory):
            record = self.load(app_id)
            if jd != self.jd(app_id):
                raise ValueError('JD changed; redo the analysis')
            if basis != context_revision(self.ws):
                raise ValueError('Profile changed during the analysis; reread')
            relative = 'analysis/' + uuid.uuid4().hex + '.json'
            payload = dict(analysis, generated_at=now(), platform=host, context_revision=basis,
                           context_group='fit', taxonomy={'source': tax['source'], 'sha256': tax['sha256']},
                           jd_sha256=record['jd_versions'][-1]['sha256'])
            atomic_json(contained(directory, relative), payload)
            record['analysis'] = {'path': relative, 'sha256': digest(directory / relative)}
            atomic_json(directory / 'record.json', record)
        return payload

    def analysis(self, app_id):
        record = self.load(app_id)
        if not record['analysis']:
            return {'valid': False, 'reason': 'JD needs analysis'}
        path = contained(self.directory(app_id), record['analysis']['path'])
        if digest(path) != record['analysis']['sha256']:
            raise ValueError('Stored analysis integrity failed')
        value = read_json(path)
        current = context_revision(self.ws)
        group = managed.GROUPS[value.get('context_group', 'fit')]
        fit_same = all(value['context_revision']['files'].get(p) == current['files'].get(p) for p in group)
        tax_same = value.get('taxonomy', {}).get('sha256') == market.taxonomy(self.ws)['sha256']
        valid = value['jd_sha256'] == record['jd_versions'][-1]['sha256'] and fit_same and tax_same
        reason = None if valid else ('Fit categories changed; redo the analysis' if not tax_same
                                     else 'Profile or JD changed; refresh the evidence')
        return {'valid': valid, 'reason': reason, 'analysis': value}

    def save_output(self, app_id, kind, text, host, evidence_ids=(), limits=None, review=None):
        platform(host)
        basis = context_revision(self.ws)
        if kind not in KINDS:
            raise ValueError('Unsupported text output kind')
        limits = limits or {}
        cat = evidence.load(self.ws)
        checks = check(text, catalog=cat, ws=self.ws, **limits)
        if not text.strip() or not checks['ok']:
            raise ValueError('Outgoing text failed checks: ' + str(checks['errors']))
        evidence.require_known(evidence_ids, cat, active_only=False)
        if not review or review.get('facts_preserved') is not True or review.get('voice_checked') is not True:
            raise ValueError('Record the factual and editorial review before saving')
        if review.get('context_revision'):
            managed.assert_fresh(self.ws, review['context_revision'], KIND_GROUP.get(kind, 'writing'), 'This draft')
        directory = self.directory(app_id)
        with locked(directory):
            record = self.load(app_id)
            if record['analysis'] and not self.analysis(app_id)['valid']:
                raise ValueError('Stored evidence mapping is stale; refresh it before saving a new output')
            if basis != context_revision(self.ws):
                raise ValueError('Profile changed during draft validation; reread')
            version = 1 + sum(x['kind'] == kind for x in record['outputs'])
            relative = 'outputs/%s/v%03d-%s/draft.md' % (kind, version, uuid.uuid4().hex[:8])
            atomic_text(contained(directory, relative), text)
            item = {'output_id': uuid.uuid4().hex, 'kind': kind, 'version': version, 'path': relative,
                    'sha256': digest(directory / relative), 'generated_at': now(), 'platform': host,
                    'context_revision': basis, 'context_group': KIND_GROUP.get(kind, 'writing'),
                    'jd_sha256': record['jd_versions'][-1]['sha256'], 'analysis_ref': record['analysis'],
                    'evidence_ids': list(evidence_ids), 'state': 'generated', 'limits': limits, 'checks': checks,
                    'review': review}
            record['outputs'].append(item)
            atomic_json(directory / 'record.json', record)
        return item

    def record_draft(self, app_id, output_id, receipt, host):
        """Store a provider draft receipt (one schema). The draft must be unsent and verified."""
        platform(host)
        required = {'provider', 'account', 'draft_id', 'message_id', 'thread_id', 'url', 'to', 'subject',
                    'attachments', 'signature', 'checks', 'verified_at'}
        if not isinstance(receipt, dict) or not required <= set(receipt) or receipt['provider'] not in PROVIDERS:
            raise ValueError('Draft receipt needs: ' + ', '.join(sorted(required)))
        if urlsplit(receipt['url']).hostname not in PROVIDERS[receipt['provider']]['hosts']:
            raise ValueError('Draft receipt needs the provider link')
        checks = receipt['checks']
        for key in ('recipient', 'subject', 'body_matches_output', 'signature_once', 'unsent'):
            if checks.get(key) is not True:
                raise ValueError('Draft receipt check failed or missing: ' + key)
        if not isinstance(receipt['to'], list) or not receipt['to']:
            raise ValueError('A draft needs a verified recipient')
        _aware(receipt['verified_at'], 'Draft receipt')
        record = self.load(app_id)
        output = next((o for o in record['outputs'] if o['output_id'] == output_id), None)
        if output is None or output['kind'] not in ('email', 'follow-up', 'cover-letter'):
            raise ValueError('Draft receipts attach to an email, follow-up or cover-letter output')
        cvs = {o['sha256']: o for o in record['outputs'] if o['kind'] == 'cv-pdf' and o.get('validation_status') == 'passed'}
        for attachment in receipt['attachments']:
            if attachment.get('sha256') not in cvs:
                raise ValueError('Attachment does not match a validated CV for this application: %s' % attachment.get('filename'))
        directory = self.directory(app_id)
        with locked(directory):
            record = self.load(app_id)
            output = next(o for o in record['outputs'] if o['output_id'] == output_id)
            drafts = output.setdefault('provider_drafts', [])
            existing = next((d for d in drafts if d['provider'] == receipt['provider'] and d['draft_id'] == receipt['draft_id']), None)
            if existing:
                return existing
            relative = str(Path(output['path']).parent.as_posix()) + '/draft-receipt-%s.json' % uuid.uuid4().hex[:8]
            atomic_json(contained(directory, relative), dict(receipt, schema='the-jobfather/draft-receipt@1'))
            item = {'provider': receipt['provider'], 'draft_id': receipt['draft_id'], 'receipt': relative,
                    'receipt_sha256': digest(directory / relative), 'recorded_at': now(), 'platform': host}
            drafts.append(item)
            atomic_json(directory / 'record.json', record)
        return item

    def record_event(self, app_id, event, host):
        platform(host)
        allowed = {'type', 'text', 'channel', 'reported_at', 'basis', 'status', 'next_action', 'contacts',
                   'local_preferences', 'output_id', 'event_id', 'evidence'}
        if not isinstance(event, dict) or set(event) - allowed:
            raise ValueError('Unrecognized event fields')
        if event.get('type') not in ('interaction', 'status', 'local-preference', 'accepted'):
            raise ValueError('Invalid event type')
        if not isinstance(event.get('text'), str) or not event['text'].strip():
            raise ValueError('Supply event text')
        if event.get('basis') not in BASES:
            raise ValueError('Events need a user report or real send/submission evidence')
        if event['basis'] == 'connector-verified':
            validate_connector_event(app_id, event)
        elif event['basis'] == 'authorized-submission':
            validate_submission_event(event)
        elif event.get('evidence'):
            raise ValueError('Evidence needs a connector-verified or authorized-submission basis')
        if event.get('status') and event['status'] not in STATUSES:
            raise ValueError('Invalid reported status')
        directory = self.directory(app_id)
        with locked(directory):
            record = self.load(app_id)
            if event.get('event_id'):
                previous = next((e for e in record['interactions'] if e.get('event_id') == event['event_id']), None)
                if previous:
                    if any(previous.get(k) != v for k, v in event.items()):
                        raise ValueError('Event identity already exists with different evidence')
                    return record
            if event['basis'] == 'connector-verified' and event.get('status'):
                if record['status'] in ('rejected', 'withdrawn') and event['status'] in ('submitted', 'interview', 'offer'):
                    raise ValueError('Connector event contradicts a resolved application; review the match')
                if record['status'] == 'offer' and event['status'] in ('interview', 'rejected'):
                    raise ValueError('Connector event contradicts an offer; review the match')
            if event['type'] == 'accepted':
                found = [x for x in record['outputs'] if x['output_id'] == event.get('output_id')]
                if not found:
                    raise ValueError('Accepted output must belong to this application')
                found[0]['state'] = 'user-approved'
            if event.get('local_preferences'):
                if not isinstance(event['local_preferences'], dict):
                    raise ValueError('Local preferences must be an object')
                record['local_preferences'].update(event['local_preferences'])
            if 'contacts' in event:
                if not isinstance(event['contacts'], list):
                    raise ValueError('Contacts must be a list of supplied application contacts')
                record['contacts'] = event['contacts']
            if 'next_action' in event:
                record['next_action'] = event['next_action']
            stored = dict(event, recorded_at=now(), platform=host)
            record['interactions'].append(stored)
            if event.get('status'):
                record['status'] = event['status']
                record['status_history'].append({'status': event['status'], 'at': stored['recorded_at'],
                                                 'platform': host, 'basis': event['basis'], 'text': event['text']})
            atomic_json(directory / 'record.json', record)
        return record

    def pack_index(self, app_id, kinds, host):
        """Index requested existing artifacts; never label missing or old-JD work complete."""
        platform(host)
        if not kinds or not set(kinds) <= (set(KINDS) | {'cv-pdf'}) - {'application-pack'}:
            raise ValueError('Select concrete requested pack modules')
        record = self.load(app_id)
        if not self.analysis(app_id)['valid']:
            raise ValueError('Pack needs a current JD/evidence mapping')
        revision = context_revision(self.ws)
        selected = []
        for kind in dict.fromkeys(kinds):
            matches = [o for o in record['outputs'] if o['kind'] == kind]
            if not matches:
                raise ValueError('Pack incomplete: missing ' + kind)
            output = max(matches, key=lambda o: o['version'])
            if output['jd_sha256'] != record['jd_versions'][-1]['sha256']:
                raise ValueError('Pack artifact belongs to an earlier JD: ' + kind)
            if any(output['context_revision']['files'].get(p) != revision['files'].get(p) for p in managed.GROUPS['fit']):
                raise ValueError('Pack artifact has stale career/HR context: ' + kind)
            resolved = self.output_path(app_id, output['output_id'])
            if kind == 'cv-pdf' and resolved['validation_status'] != 'passed':
                raise ValueError('Pack needs a validated CV PDF')
            selected.append((kind, output, resolved))
        text = '# Application pack: ' + record['company'] + '\n\nRole: ' + record['role'] + '\n\n'
        text += 'Stored status: ' + record['status'] + '. Generated artifacts are local drafts.\n\n'
        text += '\n'.join('- ' + k + ': ' + Path(p['path']).name for k, o, p in selected)
        text += '\n\nArtifacts keep their original generation context and review metadata.\n'
        return self.save_output(app_id, 'application-pack', text, host, review={
            'facts_preserved': True, 'voice_checked': True, 'context_revision': revision['revision'],
            'notes': 'Deterministic index: requested modules, checksums, same JD and validated PDF checked.'})


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__))
    parser.add_argument('--test', action='store_true', help='use fictional sandbox storage, never real applications')
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create')
    for name in ('company', 'role', 'source', 'platform'):
        create.add_argument('--' + name, required=True)
    create.add_argument('--jd-file', type=Path, required=True)
    for name in ('show', 'status', 'thread', 'pack', 'jd', 'analysis', 'output', 'save-analysis', 'save-output',
                 'event', 'revise-jd', 'record-draft'):
        child = sub.add_parser(name)
        child.add_argument('application_id')
        if name in ('output', 'record-draft'):
            child.add_argument('output_id')
        if name in ('save-analysis', 'save-output', 'event', 'revise-jd', 'record-draft'):
            child.add_argument('--platform', required=True)
            child.add_argument('--file', type=Path, required=True)
        if name == 'revise-jd':
            child.add_argument('--source', required=True)
        if name == 'save-output':
            child.add_argument('--kind', choices=KINDS, required=True)
            child.add_argument('--evidence', nargs='*', default=[])
            child.add_argument('--review-file', type=Path, required=True)
            child.add_argument('--max-chars', type=int)
            child.add_argument('--max-words', type=int)
            child.add_argument('--one-line', action='store_true')
            child.add_argument('--exclude-spaces', action='store_true')
        if name == 'save-analysis':
            child.add_argument('--context-revision', required=True)
        if name == 'pack':
            child.add_argument('--platform', required=True)
            child.add_argument('--kinds', nargs='+', required=True)
    args = parser.parse_args()
    store = Store(args.workspace, test=args.test)
    load = lambda p: json.loads(read_text(p))
    if args.command == 'create':
        result = store.create(args.company, args.role, read_text(args.jd_file), args.source, args.platform)
    elif args.command == 'show':
        result = store.load(args.application_id)
    elif args.command == 'status':
        value = store.load(args.application_id)
        result = {'application_id': value['application_id'], 'company': value['company'], 'role': value['role'],
                  'fictional': value['fictional'], 'status': value['status'], 'next_action': value['next_action'],
                  'outputs': [{'output_id': o['output_id'], 'kind': o['kind'], 'version': o['version'],
                               'state': o['state']} for o in value['outputs']],
                  'latest_interaction': value['interactions'][-1] if value['interactions'] else None,
                  'analysis': store.analysis(args.application_id)}
    elif args.command == 'thread':
        result = [e for e in store.load(args.application_id)['interactions'] if e['type'] == 'interaction']
    elif args.command == 'pack':
        result = store.pack_index(args.application_id, args.kinds, args.platform)
    elif args.command == 'jd':
        result = store.jd(args.application_id)
    elif args.command == 'analysis':
        result = store.analysis(args.application_id)
    elif args.command == 'output':
        result = store.output_path(args.application_id, args.output_id)
    elif args.command == 'save-analysis':
        result = store.save_analysis(args.application_id, load(args.file), args.platform, args.context_revision)
    elif args.command == 'save-output':
        limits = {'max_chars': args.max_chars, 'max_words': args.max_words, 'one_line': args.one_line,
                  'include_spaces': not args.exclude_spaces}
        review = load(args.review_file)
        if not review.get('context_revision'):
            raise ValueError('Review needs the context_revision from the task read')
        result = store.save_output(args.application_id, args.kind, read_text(args.file).rstrip('\n'), args.platform,
                                   args.evidence, limits, review)
    elif args.command == 'event':
        result = store.record_event(args.application_id, load(args.file), args.platform)
    elif args.command == 'record-draft':
        result = store.record_draft(args.application_id, args.output_id, load(args.file), args.platform)
    else:
        result = store.revise_jd(args.application_id, read_text(args.file), args.source, args.platform)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        raise SystemExit('Application operation failed: ' + str(error))
