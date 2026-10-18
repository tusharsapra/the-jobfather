"""Pipeline behaviour on the fictional demo workspace: matching, dates, evidence, follow-ups, interrupted writes."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from support import SCRIPTS, DemoWorkspace

import layout
from applications import Store
from job_intake import JobStore
from job_pipeline import Pipeline, stamp
from local import atomic_json, local_now
from outreach import OutreachStore, mail_link

ACCOUNT = 'alex.rivera@mail.example'
JD = 'Build SQL models and Tableau dashboards. Partner with product teams on pricing experiments.'


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.demo = DemoWorkspace()
        self.ws = self.demo.root
        self.jobs = JobStore(self.ws, test=True)
        self.jobs.ingest({'jobs': [self.job('Senior Data Analyst', 'REQ-A'), self.job('Product Analyst', 'REQ-B'),
                                   self.job('BI Analyst', 'REQ-T1'), self.job('Analytics Analyst', 'REQ-T2')]}, 'codex')
        self.rows = self.jobs.show()['jobs']
        self.applications = Store(self.ws, test=True)
        self.app = self.applications.create('Example Corp', 'Senior Data Analyst', JD, 'fixture', 'codex')
        self.other = self.applications.create('Example Corp', 'Product Analyst', JD, 'fixture', 'codex')
        self.outreach = OutreachStore(self.ws, test=True)
        self.route_a = self.send('thread-a', self.app, self.rows[0])
        self.route_b = self.send('thread-b', self.other, self.rows[1])
        self.pipeline = Pipeline(self.ws, test=True)
        self.pipeline.reconcile([], 'codex')  # creates the state file

    def tearDown(self):
        self.demo.close()

    def job(self, role, requisition, **extra):
        value = {'company': 'Example Corp', 'role': role, 'location': 'Toronto, ON', 'requisition_id': requisition,
                 'full_jd': JD, 'sources': [{'type': 'text', 'label': 'Fictional JD', 'text': JD, 'access': 'readable'}]}
        value.update(extra)
        return value

    def send(self, thread, app, job, sent_at='2026-09-01T10:00:00-04:00', message_id=None, roles=None,
             recipient='hr@example.test', outreach_type='application'):
        message_id = message_id or 'sent-' + thread
        data = {'company': 'Example Corp', 'recipient': recipient, 'roles': roles or [job['role']],
                'job_ids': [job['job_id']], 'application_id': app['application_id'] if app else None,
                'outreach_type': outreach_type, 'subject': job['role'] + ' application',
                'evidence': {'provider': 'gmail', 'message_id': message_id, 'thread_id': thread, 'sent_at': sent_at,
                             'url': mail_link(ACCOUNT, message_id), 'sender': ACCOUNT, 'recipients': [recipient],
                             'labels': ['SENT'], 'capture_sha256': hashlib.sha256(message_id.encode()).hexdigest()}}
        return self.outreach.record_sent(data, 'codex')['outreach']['outreach_id']

    def message(self, signal='hr_question', identity='message-1', **changes):
        m = {'message_id': identity, 'thread_id': 'thread-a', 'occurred_at': '2026-09-05T10:00:00-04:00',
             'sender': 'hr@example.test', 'recipients': [ACCOUNT], 'subject': 'Re: Senior Data Analyst application',
             'body': 'Please share your notice period.', 'direction': 'incoming', 'signal': signal,
             'quote': 'Please share your notice period.'}
        m.update(changes)
        return m

    def commit(self, *messages):
        return self.pipeline.reconcile(list(messages), 'codex')

    def summary(self, at=None):
        return {r['outreach_id']: r for r in self.pipeline.outreach_summary(at=at)}

    def triage(self, job_id=None, priority='Strong'):
        job_id = job_id or self.rows[2]['job_id']
        candidate = next(x for x in self.pipeline.candidates()['candidates'] if x['job_id'] == job_id)
        data = {k: candidate[k] for k in ('job_id', 'job_sha256', 'jd_sha256', 'context_hashes')}
        data.update(priority=priority, strengths=['SQL models and dashboards'], gaps=[],
                    recommendation='Prepare a tailored application',
                    requirements=[{'quote': 'Build SQL models and Tableau dashboards.', 'assessment': 'supported',
                                   'critical': True, 'evidence_ids': ['LKM-02']}])
        return data

    # ---------------------------------------------------------- mail signals
    def test_same_message_does_not_duplicate_app_history(self):
        self.commit(self.message())
        self.commit(self.message())
        record = self.applications.load(self.app['application_id'])
        self.assertEqual(len(record['interactions']), 1)
        self.assertEqual(len(self.pipeline.state()['signals']), 1)
        self.assertEqual(record['status'], 'draft')
        self.assertEqual(record['interactions'][0]['basis'], 'connector-verified')

    def test_changed_capture_with_same_identity_fails(self):
        self.commit(self.message())
        with self.assertRaises(ValueError):
            self.commit(self.message(action='Different classification'))
        self.assertEqual(len(self.pipeline.state()['signals']), 1)

    def test_recorded_message_cannot_migrate_after_route_changes(self):
        self.commit(self.message())
        self.send('thread-a', self.app, self.rows[0], sent_at='2026-09-02T10:00:00-04:00', message_id='sent-thread-a-2')
        self.assertEqual(self.commit(self.message())['duplicate'], 1)
        signals = self.pipeline.state()['signals']
        self.assertEqual(len(signals), 1)
        self.assertEqual(next(iter(signals.values()))['outreach_id'], self.route_a)
        with self.assertRaises(ValueError):
            self.commit(self.message('noise'))

    def test_shared_email_separate_threads_only_change_right_application(self):
        self.commit(self.message('interview', body='Please attend an interview.', quote='Please attend an interview.'))
        self.assertEqual(self.applications.load(self.app['application_id'])['status'], 'interview')
        self.assertEqual(self.applications.load(self.other['application_id'])['status'], 'draft')

    def test_shared_email_new_thread_needs_role_identity(self):
        m = self.message(thread_id='new-thread', subject='Hello')
        self.assertEqual(self.commit(m)['pending'], 1)
        self.assertFalse(self.pipeline.state()['signals'])
        m['body'] += ' This concerns Product Analyst.'
        self.commit(m)
        self.assertFalse(self.pipeline.state()['pending'])
        event = next(iter(self.pipeline.state()['signals'].values()))
        self.assertEqual(event['application_id'], self.other['application_id'])

    def test_grouped_anonymous_decision_is_pending(self):
        self.send('thread-g', self.app, self.rows[0], roles=['Senior Data Analyst', 'Another role'], message_id='sent-g')
        m = self.message('rejection', thread_id='thread-g', subject='Update', body='We cannot proceed.',
                         quote='We cannot proceed.')
        self.assertEqual(self.commit(m)['pending'], 1)
        self.assertEqual(self.applications.load(self.app['application_id'])['status'], 'draft')

    def test_ats_new_thread_exact_company_and_role(self):
        m = self.message('acknowledgement', sender='notifications@ats.example.test', thread_id='ats-new',
                         subject='Example Corp Senior Data Analyst', body='We received your application.',
                         quote='We received your application.')
        self.commit(m)
        record = self.applications.load(self.app['application_id'])
        self.assertEqual(record['status'], 'submitted')
        self.assertNotIn('submitted_at', record)

    def test_assessment_is_not_interview(self):
        self.commit(self.message('assessment', body='Complete the assessment.', quote='Complete the assessment.'))
        self.assertEqual(self.applications.load(self.app['application_id'])['status'], 'draft')
        self.assertEqual(self.pipeline.brief()['reply_actions'][0]['reply_action'], 'Complete assessment')

    def test_offer_is_not_hired_and_rejection_conflict_stays_pending(self):
        self.commit(self.message('offer', body='We extend an offer.', quote='We extend an offer.'))
        self.assertEqual(self.applications.load(self.app['application_id'])['status'], 'offer')
        rejected = self.message('rejection', identity='message-2', occurred_at='2026-09-06T10:00:00-04:00',
                                body='Not selected.', quote='Not selected.')
        self.assertEqual(self.commit(rejected)['pending'], 1)
        self.assertEqual(self.applications.load(self.app['application_id'])['status'], 'offer')

    def test_resolved_application_cannot_silently_reopen(self):
        self.applications.record_event(self.app['application_id'], {'type': 'status', 'basis': 'user-reported',
                                                                    'text': 'I withdrew', 'status': 'withdrawn'}, 'codex')
        self.assertEqual(self.commit(self.message('interview'))['pending'], 1)
        self.assertEqual(self.applications.load(self.app['application_id'])['status'], 'withdrawn')

    def test_interview_after_offer_requires_review(self):
        self.commit(self.message('offer', body='We extend an offer.', quote='We extend an offer.'))
        m = self.message('interview', identity='later-interview', occurred_at='2026-09-06T10:00:00-04:00')
        self.assertEqual(self.commit(m)['pending'], 1)
        self.assertEqual(self.applications.load(self.app['application_id'])['status'], 'offer')

    def test_cold_enquiry_rejection_cannot_silently_reopen(self):
        self.send('thread-c', None, self.rows[0], recipient='talent@example.test', outreach_type='enquiry',
                  message_id='sent-c')
        cold = {'thread_id': 'thread-c', 'sender': 'talent@example.test'}
        self.commit(self.message('rejection', body='Not selected.', quote='Not selected.', **cold))
        m = self.message('interview', identity='later-interview', occurred_at='2026-09-06T10:00:00-04:00', **cold)
        self.assertEqual(self.commit(m)['pending'], 1)
        self.assertEqual(len(self.pipeline.state()['signals']), 1)

    def test_same_title_separate_requisitions_need_exact_identity(self):
        self.jobs.ingest({'jobs': [self.job('Senior Data Analyst', 'REQ-C')]}, 'codex')
        job = next(j for j in self.jobs.show()['jobs'] if j['requisition_id'] == 'REQ-C')
        app = self.applications.create('Example Corp', 'Senior Data Analyst', JD, 'fixture', 'codex')
        self.send('thread-c', app, job, message_id='sent-c')
        m = self.message('acknowledgement', thread_id='new-thread')
        self.assertEqual(self.commit(m)['pending'], 1)
        m.update(requisition_id='REQ-C', body=m['body'] + ' REQ-C')
        self.assertEqual(self.commit(m)['committed'], 1)
        self.assertEqual(self.applications.load(app['application_id'])['status'], 'submitted')
        self.assertEqual(self.applications.load(self.app['application_id'])['status'], 'draft')

    def test_bounce_marks_the_outreach_without_closing_the_vacancy(self):
        m = self.message('bounce', sender='mailer-daemon@example.test', thread_id='bounce-thread',
                         original_message_id='sent-thread-a', body='Address not found.', quote='Address not found.')
        self.commit(m)
        self.assertEqual(self.applications.load(self.app['application_id'])['status'], 'draft')
        self.assertFalse(self.pipeline.brief(at='2026-09-30T12:00:00-04:00')['follow_ups_due'])
        record = self.outreach.get(self.route_a)
        self.assertEqual(record['status'], 'bounced')
        self.assertEqual(record['history'][-1]['quote'], 'Address not found.')
        self.assertEqual(self.jobs.show(self.rows[0]['job_id'])['workflow_status'], 'New')
        self.commit(m)  # replaying the same bounce changes nothing
        self.assertEqual(len([h for h in self.outreach.get(self.route_a)['history'] if h['action'] == 'bounced']), 1)

    def test_deleted_draft_is_not_followed_up(self):
        draft = self.outreach.record_drafted({
            'company': 'Example Corp', 'recipient': 'jobs@example.test', 'roles': ['BI Analyst'],
            'job_ids': [self.rows[2]['job_id']], 'outreach_type': 'application', 'subject': 'BI Analyst application',
            'draft': {'provider': 'gmail', 'draft_id': 'd-del', 'message_id': 'm-del', 'thread_id': 'thread-d'}},
            'codex')['outreach']['outreach_id']
        self.outreach.record_deleted(draft, 'codex')
        ids = [r['outreach_id'] for r in self.pipeline.brief(at='2026-09-30T12:00:00-04:00')['follow_ups_due']]
        self.assertNotIn(draft, ids)
        self.assertIn(self.route_a, ids)

    def test_due_dates_promises_and_manual_override(self):
        self.assertFalse(self.summary(at='2026-09-10T10:00:00-04:00')[self.route_a]['follow_up_due'])
        self.assertTrue(self.summary(at='2026-09-11T10:00:00-04:00')[self.route_a]['follow_up_due'])
        self.assertTrue(self.summary(at='2026-09-11T14:00:00Z')[self.route_a]['follow_up_due'])  # same instant
        m = self.message('acknowledgement', body='We will reply on September 25.', quote='We will reply on September 25.',
                         promised_at='2026-09-25T17:00:00-04:00', promise_quote='reply on September 25')
        self.commit(m)
        self.assertFalse(self.summary(at='2026-09-20T12:00:00-04:00')[self.route_a]['follow_up_due'])
        self.outreach.set(self.route_a, 'codex', follow_up_override='2026-09-26T12:00:00-04:00')
        self.assertEqual(stamp(self.summary()[self.route_a]['next_follow_up']), stamp('2026-09-26T12:00:00-04:00'))
        self.outreach.set(self.route_a, 'codex', follow_up_override='2026-09-27')  # a date: local midnight
        self.assertEqual(stamp(self.summary()[self.route_a]['next_follow_up']), stamp('2026-09-27T00:00:00-04:00'))

    def test_ambiguous_promised_date_needs_clarification(self):
        self.commit(self.message('acknowledgement', body='We will reply next week.', quote='We will reply next week.',
                                 promise_note='next week'))
        r = self.summary(at='2026-09-30T12:00:00-04:00')[self.route_a]
        self.assertIsNone(r['next_follow_up'])
        self.assertEqual(r['reply_action'], 'Clarify promised reply timeline')

    def test_verified_followups_only_and_max_two_per_thread(self):
        m = self.message('follow_up', sender=ACCOUNT, recipients=['hr@example.test'], direction='outgoing',
                         labels=['DRAFT'])
        with self.assertRaises(ValueError):
            self.commit(m)
        m['labels'] = ['SENT']
        self.commit(m)
        second = copy.deepcopy(m)
        second.update(message_id='follow-2', occurred_at='2026-09-20T14:00:00Z')
        self.commit(second)
        sibling = self.send('thread-a', self.app, self.rows[0], sent_at='2026-09-02T10:00:00-04:00', message_id='sent-3')
        summary = self.summary(at='2026-10-01T12:00:00-04:00')
        self.assertEqual(summary[self.route_a]['follow_ups_sent'], 2)
        self.assertEqual(summary[sibling]['follow_ups_sent'], 2)
        self.assertFalse(summary[self.route_a]['follow_up_due'])
        self.assertFalse(summary[sibling]['follow_up_due'])

    def test_one_due_action_for_repeated_thread(self):
        self.send('thread-a', self.app, self.rows[0], sent_at='2026-09-02T10:00:00-04:00', message_id='sent-3')
        due = self.pipeline.brief(at='2026-10-01T12:00:00-04:00')['follow_ups_due']
        self.assertEqual(sum(r['thread_id'] == 'thread-a' for r in due), 1)

    def test_new_draft_does_not_reset_the_follow_up_clock(self):
        self.outreach.record_drafted({
            'company': 'Example Corp', 'recipient': 'hr@example.test', 'roles': ['Senior Data Analyst'],
            'job_ids': [self.rows[0]['job_id']], 'application_id': self.app['application_id'],
            'outreach_type': 'follow_up', 'subject': 'Re: Senior Data Analyst application',
            'draft': {'provider': 'gmail', 'draft_id': 'd-f', 'message_id': 'm-f', 'thread_id': 'thread-a'}}, 'codex')
        summary = self.summary(at='2026-09-11T10:00:00-04:00')
        self.assertTrue(summary[self.route_a]['follow_up_due'])
        self.assertEqual(summary[self.route_a]['follow_ups_sent'], 0)

    def test_manual_reply_resolves_hr_action(self):
        self.commit(self.message())
        outgoing = self.message('sent_reply', identity='reply-2', occurred_at='2026-09-06T10:00:00-04:00',
                                sender=ACCOUNT, recipients=['hr@example.test'], direction='outgoing', labels=['SENT'])
        self.commit(outgoing)
        self.assertFalse(self.pipeline.brief()['reply_actions'])
        self.assertEqual(self.summary()[self.route_a]['follow_ups_sent'], 0)

    def test_explicit_handled_action_does_not_invent_send(self):
        self.commit(self.message())
        self.pipeline.resolve_action('message-1', 'codex')
        self.assertFalse(self.pipeline.brief()['reply_actions'])
        self.assertEqual(self.summary()[self.route_a]['follow_ups_sent'], 0)

    def test_partial_application_write_recovery_and_no_duplicate_event(self):
        original, calls = self.pipeline.save, []

        def fail_after_event(state):
            calls.append(1)
            if len(calls) == 2:
                raise OSError('Injected interruption after application commit')
            return original(state)

        with patch.object(self.pipeline, 'save', side_effect=fail_after_event):
            with self.assertRaises(OSError):
                self.commit(self.message())
        self.assertEqual(len(self.applications.load(self.app['application_id'])['interactions']), 1)
        self.commit(self.message())
        self.assertEqual(len(self.applications.load(self.app['application_id'])['interactions']), 1)
        self.assertTrue(all(s['phase'] == 'committed' for s in self.pipeline.state()['signals'].values()))

    def test_atomic_failure_leaves_previous_state(self):
        before = self.pipeline.path.read_bytes()
        with patch('local.os.replace', side_effect=OSError('Injected atomic failure')):
            with self.assertRaises(OSError):
                self.commit(self.message())
        self.assertEqual(self.pipeline.path.read_bytes(), before)
        self.assertFalse(self.applications.load(self.app['application_id'])['interactions'])

    def test_recovery_respects_manual_resolution_after_prepare(self):
        m = self.message('interview', body='Attend an interview.', quote='Attend an interview.')
        with patch.object(self.pipeline.store, 'record_event', side_effect=OSError('Interrupted before application commit')):
            with self.assertRaises(OSError):
                self.commit(m)
        self.applications.record_event(self.app['application_id'], {'type': 'status', 'basis': 'user-reported',
                                                                    'text': 'I withdrew', 'status': 'withdrawn'}, 'codex')
        self.commit()
        self.assertEqual(self.applications.load(self.app['application_id'])['status'], 'withdrawn')
        self.assertIn(m['message_id'], self.pipeline.state()['pending'])
        self.assertFalse(self.pipeline.state()['signals'])

    def test_incomplete_sync_does_not_advance_cursor_pending_stays_retryable(self):
        self.commit(self.message(thread_id='unknown', sender='unknown@example.test', subject='Hello'))
        self.assertIsNone(self.pipeline.state()['last_complete_sync'])
        with self.assertRaises(ValueError):
            self.pipeline.reconcile([], 'codex', None, True)
        self.pipeline.reconcile([], 'codex', '2026-09-10T12:00:00-04:00', True)
        self.assertIn('message-1', self.pipeline.state()['pending'])
        plan = self.pipeline.sync_plan('2026-09-11T12:00:00-04:00')
        self.assertTrue(plan['archived_included'])
        self.assertIn('message-1', plan['pending_message_ids'])
        self.assertTrue(all('in:inbox' not in q for q in plan['queries']))
        start = int(datetime(2026, 9, 8, 16, 0, tzinfo=timezone.utc).timestamp())  # cursor minus the 2-day overlap
        end = int(datetime(2026, 9, 11, 16, 0, tzinfo=timezone.utc).timestamp())
        self.assertTrue(all(q.startswith('after:%d before:%d ' % (start, end)) for q in plan['queries']))
        self.assertEqual(plan['capture_until'], '2026-09-11T16:00:00+00:00')
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):
            self.pipeline.reconcile([], 'codex', future, True)

    def test_noise_does_not_create_application_and_is_not_reread(self):
        self.commit(self.message('noise'))
        self.assertFalse(self.pipeline.state()['signals'])
        self.assertIn('message-1', self.pipeline.sync_plan()['already_recorded_message_ids'])

    def test_fake_quotes_wrong_account_and_auth_data_rejected(self):
        for changes in [{'quote': 'Not in body'}, {'recipients': ['someone-else@example.test']},
                        {'access_token': 'not-a-real-value'}, {'url': 'https://example.com/mail/message-1'}]:
            with self.assertRaises(ValueError):
                self.commit(self.message(**changes))

    def test_reconcile_and_sync_plan_refuse_without_a_mail_account(self):
        with patch('outreach.identity', return_value={'full_name': 'Alex Rivera'}):
            with self.assertRaisesRegex(ValueError, 'mail.account'):
                self.commit(self.message())
            with self.assertRaisesRegex(ValueError, 'mail.account'):
                self.pipeline.sync_plan()
        self.assertFalse(self.pipeline.state()['signals'])

    def test_sync_plan_uses_ats_senders_and_links_use_the_account(self):
        plan = self.pipeline.sync_plan('2026-09-11T12:00:00-04:00')
        self.assertEqual((plan['account'], plan['provider']), (ACCOUNT, 'gmail'))
        ats = [q for q in plan['queries'] if 'from:greenhouse.io' in q]
        self.assertEqual(len(ats), 1)
        self.assertIn('from:mailer-daemon', ats[0])
        self.assertIn('in:sent {to:hr@example.test}', ' | '.join(plan['queries']))
        self.assertIn('"Example Corp"', ' | '.join(plan['queries']))
        path = self.ws / layout.MARKET_JSON
        value = json.loads(path.read_bytes().decode('utf-8'))
        value['ats_senders'] = ['ats.example.test']
        path.write_bytes(json.dumps(value).encode('utf-8'))
        queries = ' | '.join(self.pipeline.sync_plan()['queries'])
        self.assertIn('from:ats.example.test', queries)
        self.assertNotIn('from:greenhouse.io', queries)
        self.commit(self.message())
        url = next(iter(self.pipeline.state()['signals'].values()))['message']['url']
        self.assertEqual(url, 'https://mail.google.com/mail/?authuser=alex.rivera@mail.example#all/message-1')

    def test_limits_come_from_config(self):
        path = self.ws / 'config.json'
        cfg = json.loads(path.read_bytes().decode('utf-8'))
        cfg['pipeline'].update(sync_limit=1, follow_up_days=5)
        path.write_bytes(json.dumps(cfg).encode('utf-8'))
        pipeline = Pipeline(self.ws, test=True)
        with self.assertRaisesRegex(ValueError, 'at most 1 messages'):
            pipeline.reconcile([self.message(), self.message(identity='message-2')], 'codex')
        due = {r['outreach_id']: r for r in pipeline.outreach_summary(at='2026-09-06T10:00:00-04:00')}
        self.assertTrue(due[self.route_a]['follow_up_due'])
        cfg['pipeline']['rank_limit'] = 'ten'
        path.write_bytes(json.dumps(cfg).encode('utf-8'))
        with self.assertRaisesRegex(ValueError, 'rank_limit'):
            Pipeline(self.ws, test=True)

    # ---------------------------------------------------------- triage
    def test_contacted_and_applied_jobs_leave_the_ranking(self):
        ids = {c['job_id'] for c in self.pipeline.candidates()['candidates']}
        self.assertEqual(ids, {self.rows[2]['job_id'], self.rows[3]['job_id']})
        everything = {c['job_id'] for c in self.pipeline.candidates(include_sent=True)['candidates']}
        self.assertEqual(everything, {row['job_id'] for row in self.rows})
        self.jobs.set_stage(self.rows[2]['job_id'], 'Applied', 'codex')
        self.jobs.set_stage(self.rows[3]['job_id'], 'Drafted', 'codex')
        self.assertEqual(self.pipeline.candidates()['candidates'], [])
        again = {c['job_id'] for c in self.pipeline.candidates(include_sent=True)['candidates']}
        self.assertNotIn(self.rows[2]['job_id'], again)
        self.assertIn(self.rows[3]['job_id'], again)

    def test_priority_invalidation_and_voice_change_is_cheap(self):
        data = self.triage()
        self.pipeline.save_priority(data, 'codex')
        self.assertEqual(len(self.pipeline.candidates()['candidates']), 1)
        voice = self.ws / layout.VOICE
        voice.write_bytes(voice.read_bytes() + b'\nFixture voice edit\n')
        self.assertEqual(len(self.pipeline.candidates()['candidates']), 1)
        experience = self.ws / layout.EXPERIENCE
        experience.write_bytes(experience.read_bytes() + b'\nFixture change\n')
        self.assertEqual(len(self.pipeline.candidates()['candidates']), 2)
        with self.assertRaises(ValueError):
            self.pipeline.save_priority(data, 'codex')

    def test_changed_jd_invalidates_cache(self):
        self.pipeline.save_priority(self.triage(), 'codex')
        payload = self.jobs.show()
        row = next(j for j in payload['jobs'] if j['job_id'] == self.rows[2]['job_id'])
        row['full_jd'] += ' Changed requirement.'
        atomic_json(self.jobs.path, payload)
        self.assertEqual(len(self.pipeline.candidates()['candidates']), 2)
        self.assertFalse(self.pipeline.brief()['top_jobs'])

    def test_critical_gap_cannot_be_strong_or_use_fabricated_evidence(self):
        data = self.triage()
        data['requirements'][0].update(assessment='gap', evidence_ids=[])
        with self.assertRaises(ValueError):
            self.pipeline.save_priority(data, 'codex')
        data['priority'] = 'Stretch'
        self.pipeline.save_priority(data, 'codex')
        data['requirements'][0]['evidence_ids'] = ['FAKE-01']
        with self.assertRaises(ValueError):
            self.pipeline.save_priority(data, 'codex')
        data['requirements'][0].update(evidence_ids=['LKM-02'], quote='Lead a team of twenty engineers.')
        with self.assertRaises(ValueError):
            self.pipeline.save_priority(data, 'codex')

    def test_stages_notes_and_registry_remain_unchanged(self):
        before, outreach_before = self.jobs.path.read_bytes(), self.outreach.path.read_bytes()
        self.pipeline.save_priority(self.triage(), 'codex')
        self.commit(self.message())
        self.assertEqual(self.jobs.path.read_bytes(), before)
        self.assertEqual(self.outreach.path.read_bytes(), outreach_before)
        for real in (layout.REGISTRY, layout.OUTREACH, layout.PIPELINE_STATE):
            self.assertFalse((self.ws / real).exists())

    def test_default_batch_limit_and_deferred_jobs(self):
        self.jobs.ingest({'jobs': [self.job('Role %d' % i, 'EXTRA-%d' % i) for i in range(12)]}, 'codex')
        result = self.pipeline.candidates()
        self.assertEqual(len(result['candidates']), 10)
        self.assertEqual(result['deferred'], 4)

    def test_ten_cached_priorities_show_five_and_keep_remaining_pending(self):
        template = self.triage()
        self.jobs.ingest({'jobs': [self.job('Role %d' % i, 'EXTRA-%d' % i) for i in range(12)]}, 'codex')
        for candidate in self.pipeline.candidates()['candidates']:
            data = copy.deepcopy(template)
            data.update({k: candidate[k] for k in ('job_id', 'job_sha256', 'jd_sha256', 'context_hashes')})
            self.pipeline.save_priority(data, 'codex')
        brief = self.pipeline.brief()
        self.assertEqual(len(brief['top_jobs']), 5)
        self.assertEqual(brief['valid_priorities'], 10)
        self.assertEqual(len(self.pipeline.candidates()['candidates']), 4)
        self.assertEqual(len(self.pipeline.state()['priorities']), 10)

    def test_brief_breaks_ties_with_the_market_order(self):
        soon = (local_now(self.ws).date() + timedelta(days=3)).isoformat()
        self.jobs.ingest({'jobs': [
            self.job('Data Analyst', 'REQ-L1', location='Remote (Canada)', deadline=soon),
            self.job('Analytics Lead', 'REQ-L2'),
            self.job('Insights Analyst', 'REQ-L3', location='Remote (Canada)'),
            self.job('Reporting Analyst', 'REQ-L4', email='jobs@example.test')]}, 'codex')
        for candidate in self.pipeline.candidates()['candidates']:
            self.pipeline.save_priority(self.triage(candidate['job_id']), 'codex')
        brief = self.pipeline.brief()
        roles = [p['role'] for p in brief['top_jobs']]
        self.assertEqual(brief['valid_priorities'], 6)
        self.assertEqual(roles[:2], ['Data Analyst', 'Reporting Analyst'])  # closing soon, then published contact
        self.assertNotIn('Insights Analyst', roles)  # remote ranks after the user's city
        self.assertEqual(brief['tie_breaks'], ['priority', 'closing_soon', 'location_rank', 'network',
                                               'published_contact', 'job_id'])
        path = self.ws / layout.MARKET_JSON
        value = json.loads(path.read_bytes().decode('utf-8'))
        value['scoring']['tie_breaks'] = ['location_rank', 'colour']
        path.write_bytes(json.dumps(value).encode('utf-8'))
        self.assertFalse(self.pipeline.brief()['top_jobs'])  # market.json is triage context: reassess
        for candidate in self.pipeline.candidates()['candidates']:
            self.pipeline.save_priority(self.triage(candidate['job_id']), 'codex')
        brief = self.pipeline.brief()
        self.assertEqual(brief['tie_breaks'], ['location_rank', 'job_id'])
        self.assertEqual(brief['top_jobs'][0]['location'], 'Toronto, ON')
        self.assertIn('colour', brief['warnings'][0])

    def test_blocked_is_unverified_not_expired(self):
        blocked = self.job('Blocked role', 'BLOCKED-1')
        blocked['sources'][0]['access'] = 'blocked'
        self.jobs.ingest({'jobs': [blocked]}, 'codex')
        record = next(j for j in self.jobs.show()['jobs'] if j['role'] == 'Blocked role')
        self.assertIn(record['job_id'], self.pipeline.candidates()['needs_readable_jd'])
        self.assertEqual(record['workflow_status'], 'New')

    def test_company_cache_ttl_distinct_brands_and_source_proof(self):
        research = {'company': 'Example Group A', 'website': 'https://www.example.test', 'findings': [
            {'kind': 'contact', 'summary': 'Published careers contact', 'url': 'https://example.test/careers',
             'text': 'Careers: hr@example.test', 'email': 'hr@example.test'}]}
        self.pipeline.save_research(research, 'codex')
        result = self.pipeline.research(research['company'], 'https://example.test')
        self.assertTrue(result['fresh'])
        future = (stamp(result['research']['captured_at']) + timedelta(days=31)).isoformat()
        self.assertFalse(self.pipeline.research(research['company'], research['website'], future)['fresh'])
        self.assertFalse(self.pipeline.research('Example Group B', research['website'])['fresh'])
        research['findings'][0]['email'] = 'guessed@example.test'
        with self.assertRaises(ValueError):
            self.pipeline.save_research(research, 'codex')

    def test_cli(self):
        command = [sys.executable, str(SCRIPTS / 'job_pipeline.py'), '--test']
        result = subprocess.run(command + ['candidates', '--limit', '1'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['deferred'], 1)
        batch = self.demo.base / 'messages.json'
        atomic_json(batch, {'messages': [self.message()]})
        result = subprocess.run(command + ['reconcile', '--file', str(batch), '--platform', 'codex'],
                                capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)['committed'], 1, result.stderr)
        result = subprocess.run(command + ['brief'], capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)['reply_actions'][0]['reply_action'], 'Reply to HR')
        result = subprocess.run(command + ['job', '--id', 'JOB-000000000000'], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unknown job_id', result.stderr)


if __name__ == '__main__':
    unittest.main()
