"""On-demand job triage, company-research reuse and evidenced reply and follow-up tracking.

The host assistant makes every language judgement. This module never reads mail, calls a model,
sends anything, changes career facts or submits applications: it selects, validates, stores and
dates what the host supplies. The workflow concepts (ranking, mail sync, outcome tracking) are
adapted from MadsLorentzen/ai-job-search (MIT licence).

State lives in jobs/registry.json (job_intake.py), jobs/outreach.json (outreach.py) and
jobs/pipeline/state.json (this module: priorities, research, message signals, pending matches,
handled actions and the completed-sync cursor). Payloads: references/JOB-PIPELINE.md.
"""
import argparse
import copy
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

# CLI execution and imports from tracker.py must share one module.
if __name__ == '__main__':
    sys.modules['job_pipeline'] = sys.modules[__name__]

from applications import PROVIDERS, Store, connector_event_id
import evidence
from job_intake import JobStore, parse_time
import layout
import managed
import market
from outreach import OutreachStore, mail_account, mail_link, provider_link
from local import (PLATFORMS, add_workspace_arg, atomic_json, config, contained, context_revision, local_now, locked,
                   now, platform, read_json, read_text, require_workspace, tz)

PRIORITIES = ('Strong', 'Review', 'Stretch', 'Skip')
SIGNALS = ('acknowledgement', 'hr_question', 'assessment', 'interview', 'offer',
           'rejection', 'bounce', 'follow_up', 'sent_reply', 'other_reply', 'noise')
FINAL = {'rejected', 'withdrawn', 'hired', 'offer_declined', 'closed'}
CONTACTED = ('drafted', 'sent', 'bounced')
EXCLUDE_STAGES = ('Drafted', 'Applied', 'Interviewing', 'Offer', 'Rejected', 'Withdrawn', 'Closed', 'Skipped')
CONTACT_STAGES = ('Drafted',)
SETTINGS = {'rank_limit': 1, 'top': 1, 'research_days': 1, 'follow_up_days': 1, 'max_follow_ups': 0,
            'sync_limit': 1, 'overlap_days': 0}
TIE_BREAKS = ('priority', 'closing_soon', 'location_rank', 'network', 'published_contact', 'job_id')
FIRST_SYNC_DAYS = 30
MESSAGE_FIELDS = {'message_id', 'thread_id', 'occurred_at', 'sender', 'recipients', 'subject', 'body', 'signal',
                  'quote', 'direction', 'labels', 'url', 'original_message_id', 'requisition_id', 'promised_at',
                  'promise_quote', 'promise_note', 'action'}
ACTIONS = {'hr_question': 'Reply to HR', 'assessment': 'Complete assessment',
           'interview': 'Review interview details', 'offer': 'Review offer and decide'}


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def stamp(value):
    """An ISO timestamp with a timezone, as UTC."""
    return parse_time(value).astimezone(timezone.utc)


def date_only(value):
    try:
        return datetime.fromisoformat(str(value).strip()[:10]).date()
    except (ValueError, TypeError):
        return None


def norm(value):
    return ' '.join(re.findall(r'\w+', str(value or '').casefold()))


def address(value):
    return str(value or '').strip().casefold()


def url(value):
    p = urlsplit(str(value or ''))
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Expected a public http(s) URL without credentials')
    return value


def _settings(ws):
    value = config(ws).get('pipeline') or {}
    out = {}
    for key, minimum in SETTINGS.items():
        item = value.get(key)
        if not isinstance(item, int) or isinstance(item, bool) or item < minimum:
            raise ValueError('config.json pipeline.%s must be a whole number of at least %d' % (key, minimum))
        out[key] = item
    for key, default in (('exclude_stages', EXCLUDE_STAGES), ('contact_stages', CONTACT_STAGES)):
        item = value.get(key, list(default))
        if not isinstance(item, list) or any(not isinstance(s, str) for s in item):
            raise ValueError('config.json pipeline.%s must be a list of stage names' % key)
        out[key] = [s.strip().casefold() for s in item]
    return out


def _route(record):
    """The fields the matcher and follow-up rules use, from one outreach.json record."""
    sent, draft = record.get('sent') or {}, record.get('draft') or {}
    return {'outreach_id': record['outreach_id'], 'company': record['company'], 'recipient': record['recipient'],
            'roles': list(record['roles']), 'reference_ids': list(record['job_ids']),
            'application_id': record.get('application_id'), 'status': record['status'],
            'outreach_type': record['outreach_type'], 'subject': record['subject'],
            'thread_id': sent.get('thread_id') or draft.get('thread_id'), 'message_id': sent.get('message_id'),
            'sent_at': sent.get('sent_at'), 'sent_url': sent.get('url'), 'next_action': record.get('next_action'),
            'follow_up_override': record.get('follow_up_override')}


class Pipeline:
    def __init__(self, ws=None, test=False):
        self.ws = require_workspace(ws)
        self.test = bool(test)
        self.settings = _settings(self.ws)
        self.base = contained(self.ws, layout.sandboxed(layout.PIPELINE, self.test))
        self.path = contained(self.ws, layout.sandboxed(layout.PIPELINE_STATE, self.test))
        self.store = Store(self.ws, test=self.test)
        self.registry = JobStore(self.ws, test=self.test)
        self.outreach = OutreachStore(self.ws, test=self.test)
        self.zone = tz(self.ws)[0]

    # -------------------------------------------------------------- state
    def state(self):
        value = read_json(self.path) if self.path.exists() else {'schema_version': 1}
        if not isinstance(value, dict) or value.get('schema_version') != 1:
            raise ValueError('Unsupported pipeline state')
        for key, default in (('priorities', {}), ('research', {}), ('signals', {}), ('pending', {}),
                             ('resolved_actions', []), ('ignored_messages', {}), ('last_complete_sync', None)):
            value.setdefault(key, copy.deepcopy(default))
        return value

    def save(self, value):
        atomic_json(self.path, value)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def routes(self):
        return {r['outreach_id']: _route(r) for r in self.outreach.load()['outreach']}

    def jobs(self):
        return self.registry.show()['jobs']

    def basis(self):
        """Hashes of the files triage depends on (VOICE-only edits keep triage valid)."""
        revision = context_revision(self.ws)
        return {path: revision['files'].get(path) for path in managed.GROUPS['triage']}

    @staticmethod
    def fingerprint(job):
        fields = ('company', 'role', 'full_jd', 'location', 'work_mode', 'experience', 'salary',
                  'joining_timeline', 'deadline', 'requisition_id')
        return sha({k: job.get(k) for k in fields})

    def _application(self, app_id, cache):
        if app_id not in cache:
            cache[app_id] = self.store.load(app_id)
        return cache[app_id]

    def _contacted(self):
        return {job_id for record in self.outreach.load()['outreach'] if record['status'] in CONTACTED
                for job_id in record['job_ids']}

    def _blocked(self, job, contacted, include_sent, cache):
        """Already contacted, applied, closed or skipped jobs leave the ranking."""
        blocked = set(self.settings['exclude_stages'])
        if include_sent:
            blocked -= set(self.settings['contact_stages'])
        if address(job.get('workflow_status')) in blocked:
            return True
        if not include_sent and job['job_id'] in contacted:
            return True
        app_id = job.get('application_id')
        return bool(app_id and self._application(app_id, cache)['status'] not in ('draft', 'user-approved'))

    # -------------------------------------------------------------- triage
    def candidates(self, limit=None, focus=None, include_sent=False, today=None):
        limit = self.settings['rank_limit'] if limit is None else limit
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('Rank limit must be 1-100')
        state, basis, contacted, cache = self.state(), self.basis(), self._contacted(), {}
        selected, excluded, missing = [], 0, []
        today = today or local_now(self.ws).date()
        for j in self.jobs():
            if focus and norm(focus) not in norm(' '.join(str(j.get(k) or '') for k in ('company', 'role', 'location'))):
                continue
            if self._blocked(j, contacted, include_sent, cache):
                excluded += 1
                continue
            prior = state['priorities'].get(j['job_id'])
            if prior and prior['job_sha256'] == self.fingerprint(j) and prior['context_hashes'] == basis:
                continue
            if not j.get('full_jd') or not any(s.get('access') == 'readable' for s in j['sources']):
                missing.append(j['job_id'])
                continue
            deadline = date_only(j.get('deadline'))
            if deadline and deadline < today:
                excluded += 1
                continue
            selected.append({'job_id': j['job_id'], 'company': j['company'], 'role': j['role'],
                             'location': j.get('location'), 'posted_date': j.get('posted_date'),
                             'deadline': j.get('deadline'), 'job_sha256': self.fingerprint(j),
                             'jd_sha256': sha(j['full_jd']), 'context_hashes': basis,
                             'created_at': j.get('created_at'), 'stage': j.get('workflow_status')})
        selected.sort(key=lambda j: str(j.get('created_at') or ''), reverse=True)
        return {'candidates': selected[:limit], 'eligible': len(selected),
                'deferred': max(0, len(selected) - limit), 'excluded': excluded, 'needs_readable_jd': missing}

    def job(self, job_id):
        return self.registry.show(job_id)

    def save_priority(self, data, host):
        platform(host)
        allowed = {'job_id', 'priority', 'strengths', 'gaps', 'recommendation', 'requirements',
                   'job_sha256', 'jd_sha256', 'context_hashes'}
        if not isinstance(data, dict) or set(data) != allowed:
            raise ValueError('Triage fields must be exactly: ' + ', '.join(sorted(allowed)))
        j = self.job(data['job_id'])
        if not j.get('full_jd'):
            raise ValueError('A readable full JD is required for triage')
        if data['priority'] not in PRIORITIES:
            raise ValueError('Priority must be Strong, Review, Stretch or Skip')
        basis = self.basis()
        if data['job_sha256'] != self.fingerprint(j) or data['context_hashes'] != basis:
            raise ValueError('JD/job facts or career context changed; reassess this job')
        if data['jd_sha256'] != sha(j['full_jd']):
            raise ValueError('Triage belongs to a different JD')
        for field in ('strengths', 'gaps'):
            if not isinstance(data[field], list) or any(not isinstance(x, str) or not x.strip() for x in data[field]):
                raise ValueError('Supply concrete strength/gap lists')
        if not isinstance(data['recommendation'], str) or not data['recommendation'].strip():
            raise ValueError('Supply a short recommendation')
        catalog = evidence.load(self.ws)
        requirements = data['requirements']
        if not isinstance(requirements, list) or not requirements:
            raise ValueError('Map actual JD requirements to career evidence')
        for req in requirements:
            if not isinstance(req, dict) or set(req) != {'quote', 'assessment', 'critical', 'evidence_ids'}:
                raise ValueError('Requirement needs quote, assessment, critical and evidence_ids')
            if not isinstance(req['quote'], str) or not req['quote'].strip() or req['quote'] not in j['full_jd']:
                raise ValueError('Requirement quote must occur in the actual JD')
            if req['assessment'] not in ('supported', 'partial', 'gap') or not isinstance(req['critical'], bool):
                raise ValueError('Requirement needs assessment and critical flag')
            if not isinstance(req['evidence_ids'], list) or any(not isinstance(x, str) for x in req['evidence_ids']):
                raise ValueError('Requirement evidence_ids must be a list of claim IDs')
            evidence.require_known(req['evidence_ids'], catalog, what='Requirement evidence')
            if req['assessment'] == 'supported' and not req['evidence_ids']:
                raise ValueError('Supported requirements need evidence')
        if data['priority'] == 'Strong' and any(r['critical'] and r['assessment'] != 'supported' for r in requirements):
            raise ValueError('A Strong priority cannot hide a critical requirement gap')
        with locked(self.base):
            if basis != self.basis() or self.fingerprint(j) != self.fingerprint(self.job(j['job_id'])):
                raise ValueError('Triage inputs changed during save')
            state = self.state()
            state['priorities'][j['job_id']] = dict(data, updated_at=now(), platform=host)
            self.save(state)
        return {'job_id': j['job_id'], 'priority': data['priority']}

    # -------------------------------------------------------------- company research
    def research_key(self, company, website):
        url(website)
        if not isinstance(company, str) or not norm(company):
            raise ValueError('Research needs the company or brand name')
        host = urlsplit(website).hostname.casefold()
        return sha([norm(company), host[4:] if host.startswith('www.') else host])

    def save_research(self, data, host):
        platform(host)
        if not isinstance(data, dict) or set(data) - {'company', 'website', 'findings', 'notes'}:
            raise ValueError('Research needs company, website and findings (optional notes)')
        key = self.research_key(data.get('company'), data.get('website'))
        if not isinstance(data.get('findings'), list) or not data['findings']:
            raise ValueError('Research needs source-backed findings')
        for f in data['findings']:
            if not isinstance(f, dict) or set(f) - {'kind', 'summary', 'url', 'text', 'email', 'name'}:
                raise ValueError('Finding fields: kind, summary, url, text, email, name')
            url(f.get('url'))
            if not f.get('text') or not f.get('summary') or f.get('kind') not in ('company', 'careers', 'contact'):
                raise ValueError('Finding needs source text, summary and kind')
            if f.get('email') and address(f['email']) not in f['text'].casefold():
                raise ValueError('Recruitment email must occur literally in its source')
            if f.get('name') and norm(f['name']) not in norm(f['text']):
                raise ValueError('Contact name must occur in its source')
        with locked(self.base):
            state = self.state()
            state['research'][key] = dict(data, captured_at=now(), platform=host)
            self.save(state)
        return {'key': key, 'company': data['company']}

    def research(self, company, website, at=None):
        found = self.state()['research'].get(self.research_key(company, website))
        at = stamp(at) if at else datetime.now(timezone.utc)
        fresh = bool(found and timedelta(0) <= at - stamp(found['captured_at'])
                     <= timedelta(days=self.settings['research_days']))
        return {'fresh': fresh, 'research': found}

    # -------------------------------------------------------------- mail signals
    def normalize_message(self, message, account, provider):
        if not isinstance(message, dict) or set(message) - MESSAGE_FIELDS:
            raise ValueError('Only listing-related message fields are accepted; omit raw account/auth data')
        m = copy.deepcopy(message)
        required = ('message_id', 'thread_id', 'occurred_at', 'sender', 'subject', 'body', 'signal', 'quote')
        if any(not isinstance(m.get(k), str) or not m[k].strip() for k in required):
            raise ValueError('Message needs full body, identity, timestamp and a quoted signal')
        stamp(m['occurred_at'])
        if m['signal'] not in SIGNALS or m['quote'] not in m['body']:
            raise ValueError('Signal must be supported by an exact current-message quote')
        m.setdefault('direction', 'incoming')
        m.setdefault('recipients', [])
        if not isinstance(m['recipients'], list) or any(not isinstance(x, str) for x in m['recipients']):
            raise ValueError('Recipients must be email strings')
        if not isinstance(m.get('labels', []), list) or any(not isinstance(x, str) for x in m.get('labels', [])):
            raise ValueError('Labels must be the label list from the mailbox')
        owner = address(account)
        if m['direction'] not in ('incoming', 'outgoing'):
            raise ValueError('Unknown message direction')
        outgoing = m['signal'] in ('follow_up', 'sent_reply')
        if outgoing != (m['direction'] == 'outgoing'):
            raise ValueError('Sent signals require an outgoing message')
        sent_label = PROVIDERS[provider]['sent_label']
        if outgoing and (address(m['sender']) != owner or sent_label not in m.get('labels', [])):
            raise ValueError('Follow-up counts require the account sender and the %s label' % sent_label)
        if not outgoing and (address(m['sender']) == owner or owner not in map(address, m['recipients'])):
            raise ValueError('Incoming signal must be addressed to this account')
        m['url'] = m.get('url') or mail_link(account, m['message_id'], provider)
        if not provider_link(m['url'], provider):
            raise ValueError("Message evidence needs the mail provider's own https link")
        if m.get('requisition_id') and str(m['requisition_id']).casefold() not in m['body'].casefold():
            raise ValueError('Requisition identity must occur in the current message')
        if m.get('promised_at'):
            stamp(m['promised_at'])
            if not m.get('promise_quote') or m['promise_quote'] not in m['body']:
                raise ValueError('Promised timeline needs source wording')
        if m.get('promise_note') and m['promise_note'] not in m['body']:
            raise ValueError('Unresolved timeline must quote the source')
        return m

    def match(self, m, routes, jobs):
        possible = list(routes.values())
        if m.get('original_message_id'):
            possible = [r for r in possible if r.get('message_id') == m['original_message_id']]
        else:
            by_thread = [r for r in possible if r.get('thread_id') == m['thread_id']]
            if by_thread:
                possible = by_thread
            else:
                possible = [r for r in possible if address(r['recipient']) == address(m['sender'])
                            or (m['direction'] == 'outgoing' and address(r['recipient']) in map(address, m['recipients']))]
                if possible:
                    text = norm(m['subject'] + ' ' + m['body'])

                    def same_subject(r):
                        clean = lambda s: re.sub(r'^(?:(?:re|fw|fwd)\s*:\s*)+', '', str(s or ''), flags=re.I)
                        return norm(clean(r['subject'])) == norm(clean(m['subject']))
                    possible = [r for r in possible if same_subject(r) or
                                any(norm(role) in text for role in r['roles'] if norm(role))]
                # ATS messages on a new thread need exact company AND exact role evidence.
                if not possible:
                    text = norm(m['subject'] + ' ' + m['body'])
                    possible = [r for r in routes.values() if norm(r['company']) and norm(r['company']) in text and
                                any(norm(role) in text for role in r['roles'] if norm(role))]
        if m.get('requisition_id'):
            req = str(m['requisition_id']).casefold()
            possible = [r for r in possible if any(str(jobs.get(ref, {}).get('requisition_id') or '').casefold() == req
                                                   for ref in r['reference_ids'])]
        if len(possible) > 1:
            text = norm(m['subject'] + ' ' + m['body'])
            specific = [r for r in possible if any(norm(role) in text for role in r['roles'] if norm(role))]
            if specific:
                possible = specific
        # Earlier messages for the SAME role/application in one thread are one route.
        identities = {(r.get('application_id'), tuple(sorted(r['reference_ids'])), tuple(r['roles'])) for r in possible}
        if possible and len(identities) == 1:
            return max(possible, key=lambda r: r.get('sent_at') or ''), None
        return None, 'ambiguous' if possible else 'unmatched'

    def target_application(self, route, m):
        app_id = route.get('application_id')
        if not app_id:
            return None
        record = self.store.load(app_id)
        # Never attribute a role-specific decision to a grouped enquiry without a named role.
        if len(route['roles']) > 1 and m['signal'] in ('acknowledgement', 'interview', 'offer', 'rejection'):
            if norm(record['role']) not in norm(m['subject'] + ' ' + m['body']):
                return None
        return record

    @staticmethod
    def conflict(state, route, m, record):
        prior = [s['message'] for s in state['signals'].values() if s['phase'] == 'committed'
                 and s['outreach_id'] == route['outreach_id'] and s['message']['direction'] == 'incoming']
        latest = max(prior, key=lambda p: stamp(p['occurred_at']), default=None)
        if latest and stamp(m['occurred_at']) < stamp(latest['occurred_at']):
            return 'Earlier signal arrived after a newer recorded reply'
        status = record['status'] if record else None
        if status in FINAL and m['signal'] in ('acknowledgement', 'interview', 'offer'):
            return 'Reply contradicts a resolved application'
        if (status == 'offer' or (latest and latest['signal'] == 'offer')) and m['signal'] == 'rejection':
            return 'Rejection conflicts with an existing offer'
        if (status == 'offer' or (latest and latest['signal'] == 'offer')) and m['signal'] == 'interview':
            return 'Interview invitation conflicts with an existing offer'
        if latest and latest['signal'] == 'rejection' and m['signal'] in ('acknowledgement', 'interview', 'offer'):
            return 'Reply contradicts a recorded rejection'
        return None

    def recover_locked(self, state):
        """Finish prepared signals: the application event, a bounce on the outreach, then commit."""
        routes = None
        for identity, signal in list(state['signals'].items()):
            if signal['phase'] == 'committed':
                continue
            routes = routes if routes is not None else self.routes()
            route = routes.get(signal['outreach_id'])
            if signal.get('application_id'):
                record = self.store.load(signal['application_id'])
                applied = any(e.get('event_id') == signal['event']['event_id'] for e in record['interactions'])
                if not applied:
                    problem = self.conflict(state, route, signal['message'], record) if route else 'Original outreach is missing'
                    if problem:
                        state['pending'][signal['message']['message_id']] = {'reason': problem, 'message': signal['message']}
                        del state['signals'][identity]
                        self.save(state)
                        continue
                self.store.record_event(signal['application_id'], signal['event'], signal['platform'])
            m = signal['message']
            if m['signal'] == 'bounce' and route:
                # A bounce changes the outreach route, never the vacancy or the application.
                self.outreach.mark_bounced(signal['outreach_id'], {'message_id': m['message_id'], 'url': m['url'],
                                                                   'occurred_at': m['occurred_at'], 'quote': m['quote']},
                                           signal['platform'])
            signal['phase'] = 'committed'
            self.save(state)

    def recover(self):
        """Replay interrupted signal writes (reconcile and export do this first)."""
        with locked(self.base):
            state = self.state()
            self.recover_locked(state)
        return state

    def reconcile(self, messages, host, completed_through=None, window_complete=False):
        platform(host)
        account, provider = mail_account(self.ws)
        if not isinstance(messages, list):
            raise ValueError('Supply {"messages": [...]}')
        limit = self.settings['sync_limit']
        if len(messages) > limit:
            raise ValueError('Process at most %d messages per batch; do not advance an incomplete window' % limit)
        normalized = [self.normalize_message(m, account, provider) for m in messages]
        if window_complete:
            if not completed_through:
                raise ValueError('--window-complete needs --completed-through (the capture-until time)')
            end = stamp(completed_through)
            if end > datetime.now(timezone.utc) or any(stamp(m['occurred_at']) > end for m in normalized):
                raise ValueError('Sync watermark must be a completed, past capture window')
        elif completed_through:
            raise ValueError('--completed-through only applies with --window-complete')
        counts = {'committed': 0, 'duplicate': 0, 'pending': 0}
        with locked(self.base):
            state = self.state()
            self.recover_locked(state)
            routes = self.routes()
            jobs = {j['job_id']: j for j in self.jobs()}
            backlog = [p['message'] for p in state['pending'].values()]
            batch = {m['message_id']: m for m in backlog + normalized}
            for m in sorted(batch.values(), key=lambda x: stamp(x['occurred_at'])):
                recorded = [s for s in state['signals'].values() if s['message']['message_id'] == m['message_id']]
                if recorded:
                    if any(s['message'] != m for s in recorded):
                        raise ValueError('Recorded message identity has conflicting content')
                    counts['duplicate'] += 1
                    state['pending'].pop(m['message_id'], None)
                    continue
                if m['signal'] == 'noise':
                    state['ignored_messages'][m['message_id']] = {'sha256': sha(m), 'recorded_at': now()}
                    state['pending'].pop(m['message_id'], None)
                    continue
                route, reason = self.match(m, routes, jobs)
                if not route:
                    state['pending'][m['message_id']] = {'reason': reason, 'message': m}
                    counts['pending'] += 1
                    continue
                identity = 'SIG-' + sha([m['message_id'], route['outreach_id']])[:24]
                if identity in state['signals']:
                    if state['signals'][identity]['message'] != m:
                        raise ValueError('Recorded message identity has conflicting content')
                    counts['duplicate'] += 1
                    state['pending'].pop(m['message_id'], None)
                    continue
                record = self.target_application(route, m)
                problem = self.conflict(state, route, m, record)
                if problem:
                    state['pending'][m['message_id']] = {'reason': problem, 'message': m}
                    counts['pending'] += 1
                    continue
                # Grouped, unnamed role decisions stay reviewable rather than closing every role.
                if len(route['roles']) > 1 and m['signal'] in ('interview', 'offer', 'rejection'):
                    named = [role for role in route['roles'] if norm(role) in norm(m['subject'] + ' ' + m['body'])]
                    if len(named) != 1:
                        state['pending'][m['message_id']] = {'reason': 'Grouped enquiry needs an exact role', 'message': m}
                        counts['pending'] += 1
                        continue
                proof = {'provider': provider, 'message_id': m['message_id'], 'thread_id': m['thread_id'],
                         'url': m['url'], 'occurred_at': m['occurred_at'], 'signal': m['signal'],
                         'quote': m['quote'], 'capture_sha256': sha(m)}
                event = {'type': 'interaction', 'basis': 'connector-verified', 'channel': provider,
                         'text': m['signal'].replace('_', ' ') + ': ' + m['quote'],
                         'reported_at': m['occurred_at'], 'evidence': proof, 'next_action': m.get('action') or None}
                if record:
                    event['event_id'] = connector_event_id(record['application_id'], m['message_id'])
                    target = {'acknowledgement': 'submitted', 'interview': 'interview',
                              'offer': 'offer', 'rejection': 'rejected'}.get(m['signal'])
                    if target and not (target == 'submitted' and record['status'] in ('submitted', 'interview', 'offer')):
                        event['status'] = target
                state['signals'][identity] = {'signal_id': identity, 'outreach_id': route['outreach_id'], 'message': m,
                                              'application_id': record['application_id'] if record else None,
                                              'event': event, 'platform': host, 'phase': 'prepared',
                                              'recorded_at': now()}
                self.save(state)  # durable prepare BEFORE the application event
                self.recover_locked(state)  # idempotent replay completes interrupted writes
                if identity not in state['signals']:
                    counts['pending'] += 1
                    continue
                state['pending'].pop(m['message_id'], None)
                counts['committed'] += 1
            if window_complete:
                old = state.get('last_complete_sync')
                if not old or stamp(completed_through) > stamp(old):
                    state['last_complete_sync'] = completed_through
            self.save(state)
        return dict(counts, pending_total=len(state['pending']), last_complete_sync=state['last_complete_sync'])

    def resolve_action(self, message_id, host):
        platform(host)
        with locked(self.base):
            state = self.state()
            if not any(s['message']['message_id'] == message_id for s in state['signals'].values()):
                raise ValueError('Unknown recorded message')
            if message_id not in state['resolved_actions']:
                state['resolved_actions'].append(message_id)
            self.save(state)
        return {'message_id': message_id, 'action_handled': True, 'no_send_recorded': True}

    # -------------------------------------------------------------- follow-ups and the brief
    def _override(self, value):
        """A follow-up override: a calendar date is due from local midnight."""
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(value)):
            day = datetime.strptime(value, '%Y-%m-%d').date()
            return datetime.combine(day, time(0), tzinfo=self.zone).astimezone(timezone.utc)
        return stamp(value)

    def outreach_summary(self, state=None, at=None):
        state = state or self.state()
        at = stamp(at) if at else datetime.now(timezone.utc)
        result, routes, cache = [], self.routes(), {}
        committed = [s for s in state['signals'].values() if s['phase'] == 'committed']
        failed_contacts = {address(r['recipient']) for r in routes.values() if r['status'] == 'bounced'}
        failed_contacts.update(address(routes[s['outreach_id']]['recipient']) for s in committed
                               if s['message']['signal'] == 'bounce' and s['outreach_id'] in routes)
        for r in routes.values():
            events = sorted((s for s in committed if s['outreach_id'] == r['outreach_id']),
                            key=lambda s: stamp(s['message']['occurred_at']))
            incoming = [s for s in events if s['message']['direction'] == 'incoming']
            latest = incoming[-1]['message'] if incoming else None
            siblings = [other for other in routes.values() if other['outreach_id'] == r['outreach_id'] or
                        (r.get('thread_id') and other.get('thread_id') == r['thread_id'])]
            sibling_ids = {other['outreach_id'] for other in siblings}
            conversation = [s for s in committed if s['outreach_id'] in sibling_ids]
            sent = [s['message'] for s in conversation if s['message']['direction'] == 'outgoing']
            sent_dates = [stamp(m['occurred_at']) for m in sent]
            # Only verified sends count: a draft (no sent message) never moves the clock.
            sent_dates += [stamp(other['sent_at']) for other in siblings if other.get('sent_at') and other.get('message_id')]
            last_sent = max(sent_dates, default=None)
            activity = [stamp(r['sent_at'])] if r.get('sent_at') and r.get('message_id') else []
            activity += [stamp(s['message']['occurred_at']) for s in conversation]
            activity += sent_dates
            last = max(activity) if activity else None
            count = len({m['message_id'] for m in sent if m['signal'] == 'follow_up'} |
                        {other['message_id'] for other in siblings
                         if other.get('message_id') and other['outreach_type'] == 'follow_up'})
            action = (latest.get('action') or ACTIONS.get(latest['signal'])) if latest else None
            if latest and (latest['message_id'] in state['resolved_actions']
                           or (last_sent and last_sent > stamp(latest['occurred_at']))):
                action = None
            app = self._application(r['application_id'], cache) if r.get('application_id') else None
            excluded = (not r.get('sent_at') or not r.get('message_id') or r['status'] == 'bounced' or
                        address(r['recipient']) in failed_contacts or r['status'] in ('draft-deleted', 'cancelled') or
                        (app and app['status'] in FINAL) or
                        (latest and latest['signal'] in ('bounce', 'rejection', 'offer')))
            due = last + timedelta(days=self.settings['follow_up_days']) if last and not excluded else None
            if latest and latest.get('promised_at') and due:
                due = max(due, stamp(latest['promised_at']))
            if r.get('follow_up_override') and due:
                due = self._override(r['follow_up_override'])
            if latest and latest.get('promise_note') and not latest.get('promised_at'):
                due = None
                action = action or 'Clarify promised reply timeline'
            if action or count >= self.settings['max_follow_ups']:
                due = None
            result.append(dict(r, latest_reply=latest, reply_action=action, follow_ups_sent=count,
                               last_activity=last.isoformat() if last else None,
                               next_follow_up=due.isoformat() if due else None,
                               follow_up_due=bool(due and due <= at), excluded_from_follow_up=bool(excluded)))
        groups = {}
        for r in result:
            groups.setdefault(r.get('thread_id') or r['outreach_id'], []).append(r)
        for group in groups.values():
            available = [r for r in group if r['next_follow_up']]
            selected = max(available, key=lambda r: r.get('sent_at') or '', default=None)
            for r in group:
                if any(x['reply_action'] for x in group) or r is not selected:
                    r['next_follow_up'] = None
                    r['follow_up_due'] = False
        return result

    def sync_plan(self, at=None):
        account, provider = mail_account(self.ws)
        state = self.state()
        at = (stamp(at) if at else datetime.now(timezone.utc)).replace(microsecond=0)
        if state['last_complete_sync']:
            start = stamp(state['last_complete_sync']) - timedelta(days=self.settings['overlap_days'])
        else:
            start = at - timedelta(days=FIRST_SYNC_DAYS)
        routes = list(self.routes().values())
        recipients = sorted({address(r['recipient']) for r in routes if r.get('recipient')})
        companies = sorted({str(r['company']).replace('"', '') for r in routes if r.get('company')})
        senders = list(dict.fromkeys(list(market.ats_senders(self.ws)) + ['mailer-daemon', 'postmaster']))
        # Epoch seconds make the window exact in every timezone (date operators use the server's).
        bounds = 'after:%d before:%d' % (int(start.timestamp()), int(at.timestamp()))
        incoming, outgoing = [], []
        for i in range(0, len(recipients), 20):
            incoming.append(bounds + ' -in:sent -in:drafts {' + ' '.join('from:' + e for e in recipients[i:i + 20]) + '}')
            outgoing.append(bounds + ' in:sent {' + ' '.join('to:' + e for e in recipients[i:i + 20]) + '}')
        for i in range(0, len(companies), 20):
            incoming.append(bounds + ' -in:sent -in:drafts {' + ' '.join('"' + c + '"' for c in companies[i:i + 20]) + '}')
        for i in range(0, len(senders), 20):
            incoming.append(bounds + ' -in:sent -in:drafts {' + ' '.join('from:' + s for s in senders[i:i + 20]) + '}')
        committed = {s['message']['message_id'] for s in state['signals'].values() if s['phase'] == 'committed'}
        return {'account': account, 'provider': provider, 'capture_from': start.isoformat(),
                'capture_until': at.isoformat(), 'queries': incoming + outgoing,
                'message_limit': self.settings['sync_limit'], 'last_complete_sync': state['last_complete_sync'],
                'already_recorded_message_ids': sorted(committed | {r['message_id'] for r in routes if r.get('message_id')}
                                                       | set(state['ignored_messages'])),
                'pending_message_ids': list(state['pending']), 'archived_included': True,
                'advance_cursor_only_when_all_queries_and_pages_complete': True}

    def _tie_breaks(self):
        keys, unknown = [], []
        for key in market.scoring(self.ws)['tie_breaks']:
            (keys if key in TIE_BREAKS else unknown).append(key)
        if 'job_id' not in keys:
            keys.append('job_id')
        return keys, unknown

    def brief(self, at=None):
        state, basis = self.state(), self.basis()
        today = (stamp(at) if at else datetime.now(timezone.utc)).astimezone(self.zone).date()
        contacted, cache = self._contacted(), {}
        records = self.outreach.load()['outreach']
        market_value = market.load(self.ws) or {}
        referral_jobs = {j for r in records if r['outreach_type'] == 'referral'
                         and r['status'] not in ('draft-deleted', 'cancelled') for j in r['job_ids']}
        referral_companies = {norm(r['company']) for r in records if r['outreach_type'] == 'referral'
                              and r['status'] not in ('draft-deleted', 'cancelled')}
        priorities = []
        for j in self.jobs():
            p = state['priorities'].get(j['job_id'])
            if not p or p['job_sha256'] != self.fingerprint(j) or p['context_hashes'] != basis:
                continue
            if self._blocked(j, contacted, False, cache):
                continue
            deadline = date_only(j.get('deadline'))
            if p['priority'] == 'Skip' or (deadline and deadline < today):
                continue
            posted = date_only(j.get('posted_date'))
            contact = bool(j.get('email') or any(c.get('email') for c in j.get('additional_contacts') or []))
            priorities.append(dict(p, company=j['company'], role=j['role'], location=j.get('location'),
                                   application_url=j.get('application_url'), posted_date=j.get('posted_date'),
                                   deadline=j.get('deadline'), posting_age_days=(today - posted).days if posted else None,
                                   location_rank=market.location_rank(self.ws, j.get('location'), market_value),
                                   network=j['job_id'] in referral_jobs or norm(j['company']) in referral_companies,
                                   published_contact=contact))
        keys, unknown = self._tie_breaks()

        def order(p):
            closing = date_only(p.get('deadline'))
            values = {'priority': PRIORITIES.index(p['priority']),
                      'closing_soon': not bool(closing and 0 <= (closing - today).days <= 7),
                      'location_rank': p['location_rank'], 'network': not p['network'],
                      'published_contact': not p['published_contact'], 'job_id': p['job_id']}
            return tuple(values[k] for k in keys)
        priorities.sort(key=order)
        routes = self.outreach_summary(state, at)

        def compact(r):
            keep = ('outreach_id', 'company', 'recipient', 'roles', 'application_id', 'thread_id',
                    'reply_action', 'follow_ups_sent', 'last_activity', 'next_follow_up', 'sent_url')
            value = {k: r.get(k) for k in keep}
            m = r.get('latest_reply')
            if m:
                value['latest_reply'] = {k: m.get(k) for k in ('message_id', 'signal', 'quote', 'url', 'occurred_at')}
            return value
        result = {'reply_actions': [compact(r) for r in routes if r['reply_action']],
                  'follow_ups_due': [compact(r) for r in routes if r['follow_up_due']],
                  'needs_clarification': [{'message_id': mid, 'subject': p['message']['subject'], 'reason': p['reason'],
                                           'url': p['message']['url']} for mid, p in state['pending'].items()],
                  'top_jobs': priorities[:self.settings['top']], 'valid_priorities': len(priorities),
                  'tie_breaks': keys, 'last_complete_sync': state['last_complete_sync'], 'local_snapshot_only': True}
        if unknown:
            result['warnings'] = ['Ignored unknown market.json scoring.tie_breaks: ' + ', '.join(unknown)]
        return result

    def projection(self, at=None):
        state = self.state()
        return {'schema_version': 1, 'checked_at': at or now(),
                'outreach': self.outreach_summary(state, at),
                'signals': [s for s in state['signals'].values() if s['phase'] == 'committed'],
                'pending': len(state['pending']), 'last_complete_sync': state['last_complete_sync']}

    def export(self, host='unknown'):
        import tracker
        return tracker.export(self.ws, host=host, test=self.test)


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    parser.add_argument('--test', action='store_true', help='use fictional sandbox storage')
    sub = parser.add_subparsers(dest='command', required=True)
    helps = {'candidates': 'new or changed jobs to triage', 'job': 'one stored job with its full JD',
             'save-priority': 'save a reviewed triage', 'save-research': 'save source-backed company research',
             'research': 'cached company research and whether it is fresh', 'sync-plan': 'bounded mail queries',
             'reconcile': 'record classified mail messages', 'resolve-action': 'mark a reply action handled',
             'brief': 'the action brief', 'projection': 'outreach summary and committed signals',
             'export': 'refresh jobs/outputs/Tracker.xlsx'}
    for name, text in helps.items():
        c = sub.add_parser(name, help=text)
        c.add_argument('--workspace', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        c.add_argument('--test', action='store_true', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        if name in ('save-priority', 'save-research', 'reconcile'):
            c.add_argument('--file', type=Path, required=True)
        if name in ('save-priority', 'save-research', 'reconcile', 'resolve-action'):
            c.add_argument('--platform', choices=PLATFORMS, required=True)
        if name == 'export':
            c.add_argument('--platform', choices=PLATFORMS, default='unknown')
        if name == 'candidates':
            c.add_argument('--limit', type=int)
            c.add_argument('--focus')
            c.add_argument('--include-sent', action='store_true')
        if name == 'job':
            c.add_argument('--id', required=True)
        if name == 'research':
            c.add_argument('--company', required=True)
            c.add_argument('--website', required=True)
        if name in ('brief', 'projection', 'sync-plan', 'research'):
            c.add_argument('--at', help='an ISO timestamp with a timezone (default: now)')
        if name == 'projection':
            c.add_argument('--output', type=Path)
        if name == 'reconcile':
            c.add_argument('--completed-through')
            c.add_argument('--window-complete', action='store_true')
        if name == 'resolve-action':
            c.add_argument('--message-id', required=True)
    a = parser.parse_args()
    pipeline = Pipeline(a.workspace, test=a.test)
    load = lambda path: json.loads(read_text(path))
    if a.command == 'candidates':
        result = pipeline.candidates(a.limit, a.focus, a.include_sent)
    elif a.command == 'job':
        result = pipeline.job(a.id)
    elif a.command == 'save-priority':
        result = pipeline.save_priority(load(a.file), a.platform)
    elif a.command == 'save-research':
        result = pipeline.save_research(load(a.file), a.platform)
    elif a.command == 'research':
        result = pipeline.research(a.company, a.website, a.at)
    elif a.command == 'reconcile':
        data = load(a.file)
        if not isinstance(data, dict) or set(data) != {'messages'}:
            raise ValueError('Reconcile input must be {"messages": [...]}')
        result = pipeline.reconcile(data['messages'], a.platform, a.completed_through, a.window_complete)
    elif a.command == 'sync-plan':
        result = pipeline.sync_plan(a.at)
    elif a.command == 'brief':
        result = pipeline.brief(a.at)
    elif a.command == 'resolve-action':
        result = pipeline.resolve_action(a.message_id, a.platform)
    elif a.command == 'projection':
        result = pipeline.projection(a.at)
        if a.output:
            atomic_json(a.output, result)
            result = {'projection': str(a.output)}
    else:
        result = pipeline.export(a.platform)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        raise SystemExit('Pipeline operation failed: ' + str(error))
