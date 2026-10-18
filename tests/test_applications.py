"""Application records, on the fictional demo workspace."""
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from support import DemoWorkspace

import layout
from applications import Store, connector_event_id
from facts import field
from local import atomic_json, contained, context_revision
from memory import current


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.demo = DemoWorkspace()
        self.ws = self.demo.root
        self.store = Store(self.ws, test=True)
        self.record = self.store.create('Example Corp', 'Senior Data Analyst',
                                        'Example Corp builds subscription software. Our in-house team needs SQL and dashboards.',
                                        'fictional fixture', 'claude-code')
        self.id = self.record['application_id']

    def tearDown(self):
        self.demo.close()

    def analysis(self):
        return {'employer_type': 'in_house', 'role_track': 'analytics',
                'supporting_excerpts': ['Our in-house team', 'needs SQL and dashboards'],
                'requirements': [{'requirement': 'SQL', 'evidence_ids': ['LKM-02'], 'assessment': 'supported'}],
                'fit_summary': 'Strong SQL and dashboard evidence.', 'selected_evidence_ids': ['LKM-02', 'HBH-02'],
                'gaps': [], 'emphasis': 'Dashboards.'}

    def test_unique_repeat_applications_and_sandbox(self):
        other = self.store.create('Example Corp', 'Senior Data Analyst', 'Another JD', 'fixture', 'codex')
        self.assertNotEqual(self.id, other['application_id'])
        self.assertFalse((self.ws / layout.APPLICATIONS).exists())
        self.assertTrue((self.ws / layout.SANDBOX).exists())
        self.assertTrue(self.store.load(self.id)['fictional'])

    def test_known_and_unknown_fields_without_jd(self):
        self.assertEqual(field('current_job_title', self.ws)['value'], 'Senior Data Analyst')
        self.assertEqual(field('current_employer', self.ws)['value'], 'Lakeshore Metrics')
        self.assertEqual(field('notice_period', self.ws)['value'], '2 weeks')
        result = field('current_compensation', self.ws)
        self.assertFalse(result['known'])
        self.assertEqual(result['needs_confirmation'], 'current_compensation')
        self.assertEqual(field('answer:How many years of SQL experience do you have?', self.ws)['value'], None)
        self.assertEqual(field('answer:years of sql experience', self.ws)['value'], '6')
        with self.assertRaises(ValueError):
            field('favourite_colour', self.ws)

    def test_atomic_failure_preserves_previous_file(self):
        path = self.store.directory(self.id) / 'record.json'
        before = path.read_bytes()
        with patch('local._replace', side_effect=OSError('injected failure')):
            with self.assertRaises(OSError):
                atomic_json(path, {'replacement': True})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob('.record.json-*')), [])

    def test_analysis_taxonomy_and_invalidation(self):
        self.store.save_analysis(self.id, self.analysis(), 'claude-code')
        self.assertTrue(self.store.analysis(self.id)['valid'])
        bad = self.analysis()
        bad['role_track'] = 'account_management'  # another person's taxonomy
        with self.assertRaises(ValueError):
            self.store.save_analysis(self.id, bad, 'claude-code')
        with open(self.ws / layout.EXPERIENCE, 'a', encoding='utf-8') as fh:
            fh.write('\nA hand edit.\n')
        self.assertFalse(self.store.analysis(self.id)['valid'])
        with self.assertRaises(ValueError):
            self.store.save_output(self.id, 'email', 'A new draft', 'claude-code',
                                   review={'facts_preserved': True, 'voice_checked': True})
        self.store.revise_jd(self.id, 'Updated JD', 'new fixture', 'antigravity')
        record = self.store.load(self.id)
        self.assertEqual(len(record['jd_versions']), 2)
        self.assertIsNone(record['analysis'])

    def test_analysis_rejects_wrong_job_or_unsupported_evidence(self):
        for key, value in (('supporting_excerpts', ['From another company']), ('selected_evidence_ids', ['FAKE-01'])):
            data = self.analysis()
            data[key] = value
            with self.assertRaises(ValueError):
                self.store.save_analysis(self.id, data, 'claude-code')

    def test_generated_and_accepted_leave_profile_and_status_unchanged(self):
        revision = context_revision(self.ws)
        review = {'facts_preserved': True, 'voice_checked': True}
        first = self.store.save_output(self.id, 'email', 'Subject: Data role\n\nI build dashboards.', 'codex', review=review)
        second = self.store.save_output(self.id, 'email', 'Subject: Data role\n\nI build SQL models.', 'claude-code', review=review)
        self.assertEqual(second['version'], 2)
        record = self.store.record_event(self.id, {'type': 'accepted', 'text': 'Looks good', 'basis': 'user-reported',
                                                   'output_id': second['output_id']}, 'antigravity')
        self.assertEqual(record['status'], 'draft')
        self.assertEqual(record['outputs'][1]['state'], 'user-approved')
        self.assertEqual(context_revision(self.ws), revision)

    def test_outgoing_text_cannot_leak_ids_or_avoided_phrases(self):
        review = {'facts_preserved': True, 'voice_checked': True}
        for text in ('I cut churn 18% (LKM-01).', 'I am passionate about dashboards.'):
            with self.assertRaises(ValueError):
                self.store.save_output(self.id, 'email', text, 'claude-code', review=review)

    def test_status_needs_real_evidence(self):
        with self.assertRaises(ValueError):
            self.store.record_event(self.id, {'type': 'status', 'text': 'Generated', 'basis': 'generated',
                                              'status': 'submitted'}, 'codex')
        with self.assertRaises(ValueError):
            self.store.record_event(self.id, {'type': 'accepted', 'text': 'Good', 'basis': 'user-reported',
                                              'output_id': 'foreign'}, 'codex')

    def test_connector_and_submission_evidence(self):
        quote = 'Thanks for applying'
        evidence = {'provider': 'gmail', 'message_id': 'm1', 'thread_id': 't1',
                    'url': 'https://mail.google.com/mail/?authuser=alex.rivera@mail.example#all/m1',
                    'occurred_at': '2026-10-02T10:00:00-04:00', 'signal': 'acknowledgement', 'quote': quote,
                    'capture_sha256': hashlib.sha256(quote.encode()).hexdigest()}
        event = {'type': 'status', 'text': 'Acknowledged', 'basis': 'connector-verified', 'status': 'submitted',
                 'event_id': connector_event_id(self.id, 'm1'), 'evidence': evidence}
        self.assertEqual(self.store.record_event(self.id, event, 'codex')['status'], 'submitted')
        self.assertEqual(len(self.store.record_event(self.id, event, 'codex')['interactions']), 1)
        bad = dict(event, evidence=dict(evidence, url='https://example.com/mail'))
        with self.assertRaises(ValueError):
            self.store.record_event(self.id, bad, 'codex')
        other = self.store.create('Acme Analytics', 'Analyst', 'JD', 'fixture', 'codex')['application_id']
        submission = {'type': 'status', 'text': 'Submitted on the careers page', 'basis': 'authorized-submission',
                      'status': 'submitted', 'evidence': {'kind': 'confirmation-page', 'channel': 'greenhouse',
                                                          'submitted_at': '2026-10-03T09:00:00-04:00',
                                                          'quote': 'Application submitted'}}
        self.assertEqual(self.store.record_event(other, submission, 'claude-code')['status'], 'submitted')
        with self.assertRaises(ValueError):
            self.store.record_event(other, dict(submission, evidence={'kind': 'guess', 'channel': 'x',
                                                                      'submitted_at': '2026-10-03T09:00:00-04:00'}), 'codex')

    def test_draft_receipt_requires_unsent_verified_draft_and_matching_cv(self):
        output = self.store.save_output(self.id, 'email', 'Subject: Data role\n\nI build dashboards.', 'codex',
                                        review={'facts_preserved': True, 'voice_checked': True})
        receipt = {'provider': 'gmail', 'account': 'alex.rivera@mail.example', 'draft_id': 'd1', 'message_id': 'm1',
                   'thread_id': 't1', 'url': 'https://mail.google.com/mail/#drafts/m1', 'to': ['hiring@example.com'],
                   'subject': 'Data role', 'attachments': [], 'signature': {'name': 'Alex', 'source': 'sent-message',
                                                                             'verified': True},
                   'checks': {'recipient': True, 'subject': True, 'body_matches_output': True, 'signature_once': True,
                              'unsent': True}, 'verified_at': '2026-10-02T10:00:00-04:00'}
        item = self.store.record_draft(self.id, output['output_id'], receipt, 'codex')
        self.assertEqual(self.store.record_draft(self.id, output['output_id'], receipt, 'codex'), item)
        with self.assertRaises(ValueError):
            self.store.record_draft(self.id, output['output_id'], dict(receipt, draft_id='d2',
                                    checks=dict(receipt['checks'], unsent=False)), 'codex')
        with self.assertRaises(ValueError):
            self.store.record_draft(self.id, output['output_id'], dict(receipt, draft_id='d3',
                                    attachments=[{'filename': 'cv.pdf', 'sha256': '0' * 64}]), 'codex')

    def test_path_traversal_and_symlink_rejected(self):
        with self.assertRaises(ValueError):
            contained(self.ws, '../escape')
        with self.assertRaises(ValueError):
            self.store.directory('../../profile')
        (self.ws / 'link').symlink_to(self.ws / 'profile')
        with self.assertRaises(ValueError):
            contained(self.ws, 'link/VOICE.md')

    def test_explicit_platform_and_reviews_required(self):
        with self.assertRaises(ValueError):
            self.store.save_output(self.id, 'forms', 'Lakeshore Metrics', '.agents')
        with self.assertRaises(ValueError):
            self.store.save_output(self.id, 'forms', 'Lakeshore Metrics', 'codex')
        with self.assertRaises(ValueError):
            self.store.save_output(self.id, 'cv-pdf', 'not a PDF', 'codex')

    def test_output_failure_never_leaves_a_referenced_orphan(self):
        before = self.store.load(self.id)
        with patch('applications.atomic_json', side_effect=OSError('manifest failed')):
            with self.assertRaises(OSError):
                self.store.save_output(self.id, 'forms', 'Lakeshore Metrics', 'codex',
                                       review={'facts_preserved': True, 'voice_checked': True})
        self.assertEqual(self.store.load(self.id), before)


if __name__ == '__main__':
    unittest.main()
