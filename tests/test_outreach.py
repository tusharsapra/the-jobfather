"""Outreach records: drafts from receipts, sends proven by the mailbox, deletions, user fields."""
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
from local import atomic_json
from outreach import OutreachStore, mail_link

ACCOUNT = 'alex.rivera@mail.example'


def evidence(message_id='m-1', thread_id='t-1', recipients=('hiring@example.com',), **changes):
    value = {'provider': 'gmail', 'message_id': message_id, 'thread_id': thread_id,
             'sent_at': '2026-10-05T09:30:00-04:00', 'url': mail_link(ACCOUNT, message_id), 'sender': ACCOUNT,
             'recipients': list(recipients), 'labels': ['SENT', 'IMPORTANT'],
             'capture_sha256': hashlib.sha256(message_id.encode()).hexdigest()}
    value.update(changes)
    return value


class OutreachTests(unittest.TestCase):
    def setUp(self):
        self.demo = DemoWorkspace()
        self.ws = self.demo.root
        self.apps = Store(self.ws, test=True)
        self.app = self.apps.create('Example Corp', 'Senior Data Analyst',
                                    'Example Corp needs SQL and dashboards.', 'fixture', 'codex')['application_id']
        self.output = self.apps.save_output(self.app, 'email', 'Subject: Data role\n\nI build dashboards.', 'codex',
                                            review={'facts_preserved': True, 'voice_checked': True})['output_id']
        self.receipt = {'provider': 'gmail', 'account': ACCOUNT, 'draft_id': 'd-1', 'message_id': 'm-1',
                        'thread_id': 't-1', 'url': 'https://mail.google.com/mail/#drafts/m-1',
                        'to': ['hiring@example.com'], 'subject': 'Data role', 'attachments': [],
                        'signature': {'name': 'Alex', 'source': 'sent-message', 'verified': True},
                        'checks': {'recipient': True, 'subject': True, 'body_matches_output': True,
                                   'signature_once': True, 'unsent': True},
                        'verified_at': '2026-10-04T16:00:00-04:00'}
        self.apps.record_draft(self.app, self.output, self.receipt, 'codex')
        self.jobs = JobStore(self.ws, test=True)
        self.jobs.ingest({'jobs': [{'company': 'Example Corp', 'role': 'Senior Data Analyst',
                                    'application_id': self.app, 'full_jd': 'Example Corp needs SQL and dashboards.',
                                    'sources': [{'type': 'text', 'label': 'Fixture', 'access': 'readable',
                                                 'text': 'Example Corp needs SQL and dashboards.'}]}]}, 'codex')
        self.job_id = self.jobs.show()['jobs'][0]['job_id']
        self.store = OutreachStore(self.ws, test=True)

    def tearDown(self):
        self.demo.close()

    def drafted(self):
        return self.store.record_drafted({'application_draft': {'application_id': self.app, 'output_id': self.output}},
                                         'codex')['outreach']

    def test_draft_from_an_application_receipt_is_idempotent_and_cross_checked(self):
        record = self.drafted()
        self.assertEqual((record['status'], record['recipient'], record['subject'], record['outreach_type']),
                         ('drafted', 'hiring@example.com', 'Data role', 'application'))
        self.assertEqual((record['application_id'], record['job_ids'], record['roles']),
                         (self.app, [self.job_id], ['Senior Data Analyst']))
        self.assertEqual(record['draft']['draft_id'], 'd-1')
        self.assertTrue(record['draft']['receipt'].startswith(layout.sandboxed(layout.APPLICATIONS, True) + '/' + self.app))
        self.assertEqual(hashlib.sha256((self.ws / record['draft']['receipt']).read_bytes()).hexdigest(),
                         record['draft']['receipt_sha256'])
        self.assertIsNone(record['sent'])
        self.assertEqual(record['history'][0]['basis'], 'draft-receipt')
        again = self.store.record_drafted({'application_draft': {'application_id': self.app, 'output_id': self.output}},
                                          'claude-code')
        self.assertEqual((again['created'], again['changed']), (False, False))
        self.assertEqual(len(self.store.load()['outreach']), 1)
        for bad in ({'recipient': 'someone@example.com'}, {'subject': 'Other subject'},
                    {'draft': {'provider': 'gmail', 'draft_id': 'd-unknown'}}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.store.record_drafted(dict(bad, application_draft={'application_id': self.app,
                                                                           'output_id': self.output}), 'codex')
        self.assertFalse((self.ws / layout.OUTREACH).exists())

    def test_host_verified_draft_and_updates_keep_one_record(self):
        data = {'company': 'Acme Analytics', 'recipient': 'talent@example.org', 'roles': ['Analyst'],
                'outreach_type': 'enquiry', 'subject': 'Analytics roles',
                'draft': {'provider': 'gmail', 'draft_id': 'd-9', 'message_id': 'm-9', 'thread_id': 't-9'}}
        record = self.store.record_drafted(data, 'codex')['outreach']
        self.assertEqual((record['history'][0]['basis'], record['application_id'], record['job_ids']),
                         ('host-verified', None, []))
        updated = self.store.record_drafted(dict(data, subject='Analytics roles in Toronto',
                                                 draft=dict(data['draft'], message_id='m-10')), 'codex')
        self.assertTrue(updated['changed'])
        self.assertEqual(updated['outreach']['draft']['message_id'], 'm-10')
        self.assertEqual(updated['outreach']['history'][-1]['action'], 'draft-updated')
        with self.assertRaises(ValueError):
            self.store.record_drafted(dict(data, recipient='other@example.org'), 'codex')
        with self.assertRaises(ValueError):
            self.store.record_drafted(dict(data, draft=dict(data['draft'], draft_id='d-8'), job_ids=['JOB-000000000000']),
                                      'codex')
        with self.assertRaises(ValueError):
            self.store.record_drafted(dict(data, outreach_type='cold'), 'codex')

    def test_a_send_needs_the_mailbox_record(self):
        outreach_id = self.drafted()['outreach_id']
        bad = [evidence(labels=['DRAFT']), evidence(labels=['SENT', 'DRAFT']), evidence(url='https://example.com/m-1'),
               evidence(sender='someone@example.com'), evidence(recipients=['other@example.com']),
               evidence(sent_at='2026-10-05T09:30:00'), evidence(sent_at='2099-01-01T00:00:00Z'),
               evidence(capture_sha256='abc'), dict(evidence(), token='secret')]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.store.record_sent({'outreach_id': outreach_id, 'evidence': value}, 'codex')
        with patch('outreach.identity', return_value={'full_name': 'Alex Rivera'}):
            with self.assertRaisesRegex(ValueError, 'mail.account'):
                self.store.record_sent({'outreach_id': outreach_id, 'evidence': evidence()}, 'codex')
        result = self.store.record_sent({'outreach_id': outreach_id, 'evidence': evidence()}, 'codex')
        record = result['outreach']
        self.assertEqual(record['status'], 'sent')
        self.assertEqual(set(record['sent']), {'message_id', 'thread_id', 'sent_at', 'url', 'capture_sha256'})
        self.assertIn('authuser=alex.rivera@mail.example', record['sent']['url'])
        self.assertEqual(record['history'][-1]['basis'], 'connector-verified')
        repeat = self.store.record_sent({'outreach_id': outreach_id, 'evidence': evidence()}, 'codex')
        self.assertFalse(repeat['changed'])
        with self.assertRaises(ValueError):
            self.store.record_sent({'outreach_id': outreach_id, 'evidence': evidence('m-2')}, 'codex')
        with self.assertRaises(ValueError):
            self.store.record_deleted(outreach_id, 'codex')

    def test_a_deleted_or_cancelled_draft_is_never_a_send(self):
        outreach_id = self.drafted()['outreach_id']
        record = self.store.record_deleted(outreach_id, 'codex', note='User removed it')['outreach']
        self.assertEqual(record['status'], 'draft-deleted')
        self.assertFalse(self.store.record_deleted(outreach_id, 'codex')['changed'])
        with self.assertRaisesRegex(ValueError, 'not a send'):
            self.store.record_sent({'outreach_id': outreach_id, 'evidence': evidence()}, 'codex')
        other = self.store.record_drafted({'company': 'Acme Analytics', 'recipient': 'talent@example.org',
                                           'outreach_type': 'referral', 'subject': 'Referral',
                                           'draft': {'provider': 'gmail', 'draft_id': 'd-5', 'message_id': 'm-5',
                                                     'thread_id': 't-5'}}, 'codex')['outreach']
        self.assertEqual(self.store.record_deleted(other['outreach_id'], 'codex', reason='cancelled')['outreach']['status'],
                         'cancelled')
        with self.assertRaisesRegex(ValueError, 'already recorded as draft-deleted'):
            self.store.record_drafted({'application_draft': {'application_id': self.app, 'output_id': self.output}},
                                      'codex')
        fresh = self.store.record_drafted({'company': 'Example Corp', 'recipient': 'hiring@example.com',
                                           'outreach_type': 'application', 'subject': 'Data role',
                                           'draft': {'provider': 'gmail', 'draft_id': 'd-12', 'message_id': 'm-12',
                                                     'thread_id': 't-12'}}, 'codex')
        self.assertIn('deleted an earlier draft', fresh['warnings'][0])

    def test_a_send_without_a_recorded_draft_is_idempotent_on_the_message(self):
        data = {'company': 'Example Corp', 'recipient': 'hiring@example.com', 'roles': ['Senior Data Analyst'],
                'job_ids': [self.job_id], 'application_id': self.app, 'outreach_type': 'application',
                'subject': 'Data role', 'evidence': evidence('m-7', 't-7')}
        first = self.store.record_sent(data, 'claude-code')
        self.assertTrue(first['created'])
        self.assertIsNone(first['outreach']['draft'])
        second = self.store.record_sent(data, 'codex')
        self.assertEqual((second['created'], second['outreach']['outreach_id']),
                         (False, first['outreach']['outreach_id']))
        follow = self.store.record_drafted({'company': 'Example Corp', 'recipient': 'hiring@example.com',
                                            'job_ids': [self.job_id], 'outreach_type': 'application',
                                            'subject': 'Data role',
                                            'draft': {'provider': 'gmail', 'draft_id': 'd-11', 'message_id': 'm-11',
                                                      'thread_id': 't-7'}}, 'codex')
        self.assertIn('Already sent to this recipient for the same job', follow['warnings'][0])

    def test_user_fields_and_listing(self):
        outreach_id = self.drafted()['outreach_id']
        result = self.store.set(outreach_id, 'codex', next_action='Call on Monday', follow_up_override='2026-10-20',
                                notes='Met at a meetup')
        self.assertEqual(result['changed'], ['follow_up_override', 'next_action', 'notes'])
        self.assertEqual(self.store.set(outreach_id, 'codex', next_action='Call on Monday')['changed'], [])
        self.assertEqual(self.store.set(outreach_id, 'codex', follow_up_override='2026-10-21T09:00:00Z')['outreach']
                         ['follow_up_override'], '2026-10-21T09:00:00+00:00')
        for bad in ({'follow_up_override': '2026-02-30'}, {'follow_up_override': 'next week'}, {'colour': 'red'},
                    {'notes': 'x' * 10001}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.store.set(outreach_id, 'codex', **bad)
        self.assertIsNone(self.store.set(outreach_id, 'codex', notes='')['outreach']['notes'])
        self.assertEqual(len(self.store.list(company='example', status='drafted', job_id=self.job_id)), 1)
        self.assertEqual(self.store.list(recipient='HIRING@example.com')[0]['outreach_id'], outreach_id)
        self.assertEqual(self.store.list(status='sent'), [])

    def test_cli(self):
        draft = self.demo.base / 'draft.json'
        atomic_json(draft, {'application_draft': {'application_id': self.app, 'output_id': self.output}})
        command = [sys.executable, str(SCRIPTS / 'outreach.py'), '--test']
        run = lambda *args: subprocess.run(command + list(args), capture_output=True, text=True)
        result = run('record-drafted', '--file', str(draft), '--platform', 'codex')
        self.assertEqual(result.returncode, 0, result.stderr)
        outreach_id = json.loads(result.stdout)['outreach']['outreach_id']
        result = run('set', '--outreach-id', outreach_id, '--follow-up-date', '2026-10-25', '--platform', 'codex')
        self.assertEqual(json.loads(result.stdout)['changed'], ['follow_up_override'])
        sent = self.demo.base / 'sent.json'
        atomic_json(sent, {'outreach_id': outreach_id, 'evidence': evidence()})
        self.assertEqual(run('record-sent', '--file', str(sent), '--platform', 'codex').returncode, 0)
        listed = json.loads(run('list', '--status', 'sent').stdout)
        self.assertEqual([r['outreach_id'] for r in listed], [outreach_id])
        result = run('record-deleted', '--outreach-id', outreach_id)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('only an unsent draft', result.stderr)


if __name__ == '__main__':
    unittest.main()
