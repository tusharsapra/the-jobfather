"""Change-journal tests on the fictional demo workspace; platform strings are simulated writers."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

from support import SCRIPTS, DemoWorkspace, proposal

import layout
import managed
from applications import Store
from local import atomic_json, digest, read_json, read_text, shared_lock
from memory import apply, current, events, initialize, read_context, rollback, status


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.demo = DemoWorkspace()
        self.ws = self.demo.root
        self.seed = current(self.ws)

    def tearDown(self):
        self.demo.close()

    def proposal(self, suffix='first', host='claude-code', paths=('profile/VOICE.md',)):
        return proposal(self.ws, suffix, host, paths)

    def child(self, value, crash=None):
        payload = self.demo.base / (value['transaction_id'] + '.json')
        atomic_json(payload, value)
        code = ('import sys; sys.path.insert(0, sys.argv[1]); from memory import apply; '
                'from local import read_json; apply(read_json(sys.argv[3]), sys.argv[2])')
        env = dict(os.environ)
        if crash:
            env['JOBFATHER_TEST_CRASH'] = crash
        return subprocess.Popen([sys.executable, '-c', code, str(SCRIPTS), str(self.ws), str(payload)],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_initialization_registers_reviewed_files_not_invented_history(self):
        event = events(self.ws)[0]
        self.assertEqual(event['change_type'], 'initialization')
        self.assertEqual(event['before_revision'], self.seed)
        self.assertEqual(len(events(self.ws)), 1)
        initialize(self.ws, 'codex')
        self.assertEqual(len(events(self.ws)), 1)
        self.assertEqual(read_json(self.ws / layout.ONBOARDING)['phase'], 'complete')

    def test_initialize_requires_reviewed_receipt(self):
        other = DemoWorkspace(init=False)
        try:
            with self.assertRaises(ValueError):
                initialize(other.root, 'codex')
            import onboard
            atomic_json(other.root / layout.ONBOARDING, {'schema_version': 1, 'phase': 'collecting', 'steps': {}})
            onboard.mark_ready(other.root, 'codex')
            with open(other.root / layout.VOICE, 'a', encoding='utf-8') as fh:
                fh.write('\nChanged after review.\n')
            with self.assertRaises(ValueError):
                initialize(other.root, 'codex')
        finally:
            other.close()

    def test_acceptance_alone_rejected_even_with_an_invented_intent_flag(self):
        p = self.proposal()
        p['user_request'] = 'Looks good'
        with self.assertRaises(ValueError):
            apply(p, self.ws)
        p = self.proposal()
        p['intent'] = 'accepted'
        with self.assertRaises(ValueError):
            apply(p, self.ws)
        self.assertEqual(current(self.ws), self.seed)

    def test_targeted_update_one_event_and_complete_provenance(self):
        e = apply(self.proposal(host='unknown'), self.ws)
        self.assertEqual(e['platform'], 'unknown')
        self.assertEqual(e['affected_paths'], ['profile/VOICE.md'])
        offset = datetime.now(timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('America/Toronto')).isoformat()[-6:]
        self.assertTrue(e['timestamp_local'].endswith(offset))
        self.assertTrue(e['timezone_exact'])
        self.assertTrue(e['timestamp_utc'].endswith('+00:00'))
        self.assertEqual(e['hashes']['profile/VOICE.md']['after'], digest(self.ws / layout.VOICE))
        for path, value in self.seed['files'].items():
            if path != layout.VOICE:
                self.assertEqual(managed.raw_revision(self.ws)['files'][path], value)
        self.assertEqual(len(events(self.ws)), 2)
        self.assertEqual(read_json(self.ws / layout.INDEX)['source_revision'], current(self.ws))
        self.assertIn('The Jobfather change history', read_text(self.ws / layout.CHANGELOG))

    def test_retry_is_idempotent_and_changed_payload_rejected(self):
        p = self.proposal()
        e = apply(p, self.ws)
        self.assertEqual(apply(p, self.ws), e)
        self.assertEqual(len(events(self.ws)), 2)
        p['reason'] = 'Different reason'
        with self.assertRaises(ValueError):
            apply(p, self.ws)

    def test_stale_revision_and_target_hash_rejected(self):
        old = self.proposal('old')
        apply(self.proposal('new'), self.ws)
        with self.assertRaises(ValueError):
            apply(old, self.ws)
        p = self.proposal('hash')
        p['changes'][0]['before_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            apply(p, self.ws)

    def test_actual_process_crash_at_every_transaction_boundary_recovers(self):
        for point in ('prepared', 'after-file-1', 'after-file-2', 'before-log', 'after-log'):
            with self.subTest(point=point):
                p = self.proposal(point, paths=('profile/VOICE.md', 'profile/STORY-BANK.md'))
                child = self.child(p, point)
                _, stderr = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 87, stderr)
                result = status(self.ws)
                self.assertEqual(len(result['recovered_transactions']), 1)
                e = apply(p, self.ws)
                self.assertIn(e['status'], ('committed', 'recovered'))
                for op in p['changes']:
                    self.assertTrue(read_text(self.ws / op['path']).endswith('Short direct opening.\n'))
                self.assertEqual(sum(x['transaction_id'] == p['transaction_id'] for x in events(self.ws)), 1)
                self.assertEqual(read_json(self.ws / layout.INDEX)['source_revision'], current(self.ws))

    def test_two_simulated_platform_writers_never_silently_overwrite(self):
        a = self.proposal('concurrent-a', host='codex')
        b = self.proposal('concurrent-b', host='claude-code')
        left, right = self.child(a), self.child(b)
        outputs = [left.communicate(timeout=20), right.communicate(timeout=20)]
        self.assertEqual(sorted([left.returncode, right.returncode]), [0, 1], str(outputs))
        self.assertEqual(len(events(self.ws)), 2)
        text = read_text(self.ws / layout.VOICE)
        self.assertNotEqual('Fixture concurrent-a' in text, 'Fixture concurrent-b' in text)

    def test_live_kernel_lock_is_not_deleted_on_timeout(self):
        ready = self.demo.base / 'ready'
        lock = self.ws / layout.ROOT_LOCK
        code = ('import sys,time; sys.path.insert(0,sys.argv[1]); from local import shared_lock; '
                'from pathlib import Path\nwith shared_lock(sys.argv[2]):\n Path(sys.argv[3]).write_text("ready"); time.sleep(1)')
        child = subprocess.Popen([sys.executable, '-c', code, str(SCRIPTS), str(lock), str(ready)])
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        try:
            with self.assertRaises(RuntimeError):
                with shared_lock(lock, timeout=.05):
                    pass
            self.assertTrue(lock.exists())
            self.assertTrue(Path(str(lock) + '.json').exists())
        finally:
            child.wait(timeout=5)
        self.assertEqual(current(self.ws), self.seed)

    def test_rollback_restores_previous_content_and_adds_new_event(self):
        original = apply(self.proposal(paths=('profile/VOICE.md', 'profile/STORY-BANK.md')), self.ws)
        e = rollback(original['event_id'], 'Roll back this fixture update.', 'claude-code', 'TX-fixture-rollback', self.ws)
        self.assertEqual(e['status'], 'rolled-back')
        self.assertEqual(current(self.ws), self.seed)
        self.assertEqual(len(events(self.ws)), 3)
        self.assertEqual(rollback(original['event_id'], 'Roll back this fixture update.', 'claude-code',
                                  'TX-fixture-rollback', self.ws), e)

    def test_rollback_refuses_overwriting_later_changes(self):
        original = apply(self.proposal(), self.ws)
        apply(self.proposal('later'), self.ws)
        with self.assertRaises(ValueError):
            rollback(original['event_id'], 'Rollback', 'codex', 'TX-fixture-conflict', self.ws)

    def test_partial_log_tail_is_preserved_and_repaired_without_duplicate(self):
        p = self.proposal('tail')
        self.child(p, 'before-log').communicate(timeout=20)
        with (self.ws / layout.CHANGES).open('ab') as f:
            f.write(b'{"event_id":"partial')
        status(self.ws)
        self.assertEqual(len(events(self.ws)), 2)
        self.assertEqual(len(list((self.ws / layout.LOG_FRAGMENTS).glob('*.bin'))), 1)
        apply(p, self.ws)
        self.assertEqual(len(events(self.ws)), 2)

    def test_tampered_recovery_file_refused_without_success_event(self):
        p = self.proposal('tamper')
        self.child(p, 'prepared').communicate(timeout=20)
        (self.ws / layout.VOICE).write_bytes(b'# External conflicting change\n')
        with self.assertRaises(RuntimeError):
            current(self.ws)
        self.assertEqual(len(events(self.ws)), 1)

    def test_paths_and_symlinks_blocked(self):
        p = self.proposal()
        p['changes'][0]['path'] = '../escape'
        with self.assertRaises(ValueError):
            apply(p, self.ws)
        p = self.proposal()
        target = self.demo.base / 'elsewhere.md'
        target.write_bytes((self.ws / layout.VOICE).read_bytes())
        (self.ws / layout.VOICE).unlink()
        (self.ws / layout.VOICE).symlink_to(target)
        with self.assertRaises(ValueError):
            apply(p, self.ws)

    def test_hr_update_dependency_review_masking_and_schema(self):
        p = self.proposal('hr', paths=('profile/HR-DETAILS.json',))
        op = p['changes'][0]
        del op['append']
        op['json_set'] = [{'keys': ['notice_period', 'value'], 'value': 'Fictional fixture: 30 days'}]
        with self.assertRaises(ValueError):
            apply(p, self.ws)  # dependency review missing
        p['dependency_review'] = {'profile/PROFILE.md': 'unchanged', 'profile/FORM-ANSWERS.json': 'unchanged'}
        e = apply(p, self.ws)
        self.assertEqual(e['affected_paths'], ['profile/HR-DETAILS.json'])
        self.assertTrue(e['user_feedback'].startswith('[withheld'))
        hr = read_json(self.ws / layout.HR)
        self.assertIsNone(hr['current_compensation']['value'])
        bad = self.proposal('bad-hr', paths=('profile/HR-DETAILS.json',))
        op = bad['changes'][0]
        del op['append']
        op['json_set'] = [{'keys': ['owner'], 'value': 'Someone else'}]
        bad['dependency_review'] = p['dependency_review']
        with self.assertRaises(ValueError):
            apply(bad, self.ws)
        leak = self.proposal('leak-hr', paths=('profile/HR-DETAILS.json',))
        op = leak['changes'][0]
        del op['append']
        op['json_set'] = [{'keys': ['expected_compensation', 'value'], 'value': 123456}]
        leak['dependency_review'] = p['dependency_review']
        leak['summary'] = {'before': 'Older pay.', 'after': 'Expected pay is now 123456.'}
        with self.assertRaises(ValueError):
            apply(leak, self.ws)

    def test_new_fact_types_go_under_custom_with_json_add(self):
        p = self.proposal('custom', paths=('profile/HR-DETAILS.json',))
        op = p['changes'][0]
        del op['append']
        op['json_set'] = [{'keys': ['night_shifts'], 'value': {'value': True, 'confirmed_at': '2026-10-01'}}]
        p['dependency_review'] = {'profile/PROFILE.md': 'unchanged', 'profile/FORM-ANSWERS.json': 'unchanged'}
        with self.assertRaises(ValueError):
            apply(p, self.ws)
        del op['json_set']
        op['json_add'] = [{'keys': ['custom', 'night_shifts'],
                           'value': {'value': True, 'confirmed_at': '2026-10-01', 'description': 'Accepts night shifts'}}]
        apply(p, self.ws)
        self.assertTrue(read_json(self.ws / layout.HR)['custom']['night_shifts']['value'])

    def test_policy_changes_only_through_policy_type(self):
        p = self.proposal('policy', paths=('config/policy.json',))
        op = p['changes'][0]
        del op['append']
        op['json_set'] = [{'keys': ['mode'], 'value': 'send-with-approval'}]
        p['user_request'] = 'Enable sending emails after I approve each one.'
        with self.assertRaises(ValueError):
            apply(dict(p, change_type='policy', changes=[dict(op, path='profile/VOICE.md')]), self.ws)
        e = apply(dict(p, change_type='policy'), self.ws)
        self.assertEqual(read_json(self.ws / layout.POLICY)['mode'], 'send-with-approval')
        self.assertEqual(e['change_type'], 'policy')
        bad = self.proposal('policy-bad', paths=('config/policy.json',))
        op = bad['changes'][0]
        del op['append']
        op['json_set'] = [{'keys': ['mode'], 'value': 'autopilot'}]
        bad['user_request'] = 'Set mode to autopilot.'
        with self.assertRaises(ValueError):
            apply(dict(bad, change_type='policy'), self.ws)

    def test_external_edits_are_logged_and_block_conflicting_rollback(self):
        original = apply(self.proposal('ext'), self.ws)
        with open(self.ws / layout.VOICE, 'a', encoding='utf-8') as fh:
            fh.write('\n- Edited in a text editor.\n')
        result = status(self.ws)
        self.assertTrue(result['external_edits_logged'])
        latest = events(self.ws)[-1]
        self.assertEqual((latest['change_type'], latest['status']), ('external-edit', 'observed'))
        with self.assertRaises(ValueError):
            rollback(original['event_id'], 'Undo the fixture rule', 'codex', 'TX-fixture-ext-undo', self.ws)
        status(self.ws)
        self.assertEqual(len(events(self.ws)), 3)  # detection is not repeated

    def make_application(self):
        store = Store(self.ws, test=True)
        app = store.create('Example Corp', 'Senior Data Analyst',
                           'Example Corp is hiring a data analyst for its in-house team. You will build dashboards.',
                           'fixture', 'claude-code')
        analysis = {'employer_type': 'in_house', 'role_track': 'analytics',
                    'supporting_excerpts': ['in-house team', 'build dashboards'],
                    'requirements': [{'requirement': 'Dashboards', 'evidence_ids': ['LKM-02'], 'assessment': 'supported'}],
                    'fit_summary': 'Relevant dashboard experience.', 'selected_evidence_ids': ['LKM-02'],
                    'gaps': [], 'emphasis': 'Dashboards and SQL.'}
        store.save_analysis(app['application_id'], analysis, 'claude-code')
        output = store.save_output(app['application_id'], 'linkedin', 'I build dashboards people use.', 'claude-code',
                                   review={'facts_preserved': True, 'voice_checked': True})
        return store, app['application_id'], output

    def test_company_preference_is_local_with_full_audit_and_retry(self):
        store, app, _ = self.make_application()
        other = store.create('Other Co', 'Role', 'Other JD', 'fixture', 'claude-code')
        p = self.proposal('local')
        p.update(scope='application', change_type='application-specific', application_id=app, test_application=True)
        p['user_request'] = 'For this application, focus on dashboard work.'
        path = store.directory(app) / 'record.json'
        p['changes'] = [{'path': path.relative_to(self.ws).as_posix(), 'before_sha256': digest(path),
                         'json_set': [{'keys': ['local_preferences', 'emphasis'], 'value': 'Dashboards only here'}]}]
        e = apply(p, self.ws)
        self.assertEqual(apply(p, self.ws), e)
        self.assertEqual(current(self.ws)['files'], self.seed['files'])
        self.assertEqual(store.load(other['application_id'])['local_preferences'], {})
        self.assertEqual(store.load(app)['status'], 'draft')
        self.assertEqual(store.load(app)['local_preferences']['emphasis'], 'Dashboards only here')
        new = store.save_output(app, 'linkedin', 'Later unrelated draft.', 'claude-code',
                                review={'facts_preserved': True, 'voice_checked': True})
        rollback(e['event_id'], 'Undo this company preference', 'unknown', 'TX-local-rollback', self.ws)
        self.assertEqual(store.load(app)['local_preferences'], {})
        self.assertTrue(Path(store.output_path(app, new['output_id'])['path']).exists())

    def test_voice_keeps_fit_analysis_career_change_invalidates_it(self):
        store, app, output = self.make_application()
        apply(self.proposal('voice'), self.ws)
        self.assertTrue(store.analysis(app)['valid'])
        p = self.proposal('career', paths=('profile/EXPERIENCE.md',))
        p['change_type'] = 'factual-correction'
        p['dependency_review'] = {'profile/PROFILE.md': 'unchanged', 'profile/STORY-BANK.md': 'unchanged'}
        apply(p, self.ws)
        self.assertFalse(store.analysis(app)['valid'])
        self.assertTrue(Path(store.output_path(app, output['output_id'])['path']).is_file())

    def test_stale_draft_review_rejected_only_when_its_group_changed(self):
        store, app, _ = self.make_application()
        old = read_context(['VOICE.md'], self.ws)
        apply(self.proposal('read'), self.ws)
        new = read_context(['VOICE.md'], self.ws)
        self.assertNotEqual(old['context_revision'], new['context_revision'])
        self.assertIn('Fixture read', new['content'][layout.VOICE])
        with self.assertRaises(ValueError):
            store.save_output(app, 'linkedin', 'New draft', 'claude-code', review={
                'facts_preserved': True, 'voice_checked': True, 'context_revision': old['context_revision']['revision']})
        # A voice change does not make CV work stale.
        self.assertEqual(managed.group_changes(self.ws, old['context_revision']['revision'], 'cv'), [])

    def test_learning_ref_requires_actual_accepted_output(self):
        store, app, output = self.make_application()
        p = self.proposal('accepted-ref')
        p['accepted_output_ref'] = {'application_id': app, 'output_id': output['output_id'], 'test': True}
        with self.assertRaises(ValueError):
            apply(p, self.ws)
        store.record_event(app, {'type': 'accepted', 'text': 'Looks good', 'basis': 'user-reported',
                                 'output_id': output['output_id']}, 'claude-code')
        apply(p, self.ws)
        self.assertEqual(events(self.ws)[-1]['accepted_output_ref'], p['accepted_output_ref'])

    def test_cli_apply_does_not_self_lock(self):
        store, app, output = self.make_application()
        store.record_event(app, {'type': 'accepted', 'text': 'Fixture approved', 'basis': 'user-reported',
                                 'output_id': output['output_id']}, 'claude-code')
        p = self.proposal('cli-ref')
        p['accepted_output_ref'] = {'application_id': app, 'output_id': output['output_id'], 'test': True}
        path = self.demo.base / 'proposal.json'
        atomic_json(path, p)
        result = subprocess.run([sys.executable, str(SCRIPTS / 'memory.py'), 'apply', '--file', str(path)],
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['platform'], 'claude-code')
        self.assertEqual(len(events(self.ws)), 2)

    def test_fact_correction_with_dependent_summary_and_rollback_of_new_id(self):
        from facts import field
        p = self.proposal('career-summary', paths=('profile/EXPERIENCE.md', 'profile/PROFILE.md'))
        p['change_type'] = 'factual-correction'
        p['user_request'] = 'Correct my stored title to Fictional Fixture Manager.'
        for op in p['changes']:
            del op['append']
            op['replace'] = {'old': 'Senior Data Analyst', 'new': 'Fictional Fixture Manager'}
        p['dependency_review'] = {'profile/PROFILE.md': 'updated', 'profile/STORY-BANK.md': 'unchanged'}
        e = apply(p, self.ws)
        self.assertEqual(field('current_job_title', self.ws)['value'], 'Fictional Fixture Manager')
        rollback(e['event_id'], 'Roll back fictional title correction', 'unknown', 'TX-summary-rollback', self.ws)
        self.assertEqual(current(self.ws)['files'], self.seed['files'])
        p = self.proposal('new-id', paths=('profile/EXPERIENCE.md',))
        p['dependency_review'] = {'profile/PROFILE.md': 'unchanged', 'profile/STORY-BANK.md': 'unchanged'}
        row = '| LKM-05 | Added data-quality checks in Airflow. | Broken-report incidents from about 9 to 2 per quarter. |'
        del p['changes'][0]['append']
        p['changes'][0]['replace'] = {'old': row, 'new': row + '\n| LKM-06 | Fictional new evidence. | Fixture only. |'}
        e = apply(p, self.ws)
        rollback(e['event_id'], 'Roll back fictional example', 'unknown', 'TX-id-rollback', self.ws)
        self.assertEqual(current(self.ws)['files'], self.seed['files'])

    def test_evidence_ids_cannot_be_deleted_or_dangle(self):
        p = self.proposal('delete-id', paths=('profile/EXPERIENCE.md',))
        p['dependency_review'] = {'profile/PROFILE.md': 'unchanged', 'profile/STORY-BANK.md': 'unchanged'}
        del p['changes'][0]['append']
        p['changes'][0]['replace'] = {'old': '| LKM-04 | Mentored two junior analysts. | Informal mentoring; no direct reports. |\n', 'new': ''}
        with self.assertRaises(ValueError):
            apply(p, self.ws)
        p = self.proposal('dangle', paths=('profile/STORY-BANK.md',))
        p['changes'][0]['append'] = '- Evidence: LKM-99'
        with self.assertRaises(ValueError):
            apply(p, self.ws)

    def test_corrupt_snapshot_blocks_recovery(self):
        p = self.proposal('snapshot')
        self.child(p, 'prepared').communicate(timeout=20)
        journal = self.ws / layout.TRANSACTIONS / p['transaction_id'] / 'journal.json'
        snapshot = self.ws / read_json(journal)['targets'][0]['after_snapshot']
        snapshot.write_bytes(b'# Tampered snapshot\n')
        with self.assertRaises(RuntimeError):
            status(self.ws)
        self.assertEqual(len(events(self.ws)), 1)

    def test_application_preference_crash_recovers_before_next_read(self):
        store, app, _ = self.make_application()
        p = self.proposal('local-crash')
        p.update(scope='application', change_type='application-specific', application_id=app, test_application=True)
        p['user_request'] = 'For this application, focus on forecasting.'
        path = store.directory(app) / 'record.json'
        p['changes'] = [{'path': path.relative_to(self.ws).as_posix(), 'before_sha256': digest(path),
                         'json_set': [{'keys': ['local_preferences', 'emphasis'], 'value': 'Fictional local emphasis'}]}]
        child = self.child(p, 'after-file-1')
        child.communicate(timeout=20)
        self.assertEqual(child.returncode, 87)
        self.assertEqual(store.load(app)['local_preferences']['emphasis'], 'Fictional local emphasis')
        self.assertEqual(apply(p, self.ws)['status'], 'recovered')

    def test_pack_index_requires_requested_modules_and_same_jd(self):
        store, app, output = self.make_application()
        with self.assertRaises(ValueError):
            store.pack_index(app, ['linkedin', 'cv-pdf'], 'claude-code')
        index = store.pack_index(app, ['linkedin'], 'claude-code')
        self.assertEqual(index['kind'], 'application-pack')
        self.assertEqual(store.load(app)['status'], 'draft')
        Path(store.output_path(app, output['output_id'])['path']).write_bytes(b'Tampered draft')
        with self.assertRaises(ValueError):
            store.pack_index(app, ['linkedin'], 'claude-code')


if __name__ == '__main__':
    unittest.main()
