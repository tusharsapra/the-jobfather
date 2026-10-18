"""Learning loop: pending queue, resolve against logged events, sweep, outcome stats, LEARNED.md compaction."""
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
import unittest

from support import SCRIPTS, DemoWorkspace, proposal

import jf
import layout
import learn
import memory
from applications import Store
from local import atomic_json, read_json, read_text


class LearnTests(unittest.TestCase):
    def setUp(self):
        self.demo = DemoWorkspace()
        self.ws = self.demo.root

    def tearDown(self):
        self.demo.close()

    def apply(self, suffix, paths=('profile/VOICE.md',), change_type='preference', learning_ids=None):
        p = proposal(self.ws, suffix, paths=paths, change_type=change_type)
        if learning_ids:
            p['learning_ids'] = learning_ids
        return memory.apply(p, self.ws)

    def lines(self):
        return [json.loads(line) for line in read_text(self.ws / layout.INBOX).splitlines()]

    def test_queue_validates_defaults_target_and_dedupes(self):
        item = learn.queue(self.ws, 'form-answer', 'Answers "willing to travel" with: occasionally.', 'chat')
        self.assertRegex(item['id'], r'^LRN-[0-9a-f]{12}$')
        self.assertEqual((item['status'], item['scope'], item['target_hint']),
                         ('pending', 'global', 'profile/FORM-ANSWERS.json'))
        again = learn.queue(self.ws, 'form-answer', '  Answers "willing to travel"   with: occasionally. ', 'recruiter')
        self.assertEqual((again['id'], again['duplicate']), (item['id'], True))
        self.assertEqual(len(self.lines()), 1)
        fact = learn.queue(self.ws, 'fact', 'Has never used Spark.', 'chat', target='EXPERIENCE.md', sensitive=False)
        self.assertEqual(fact['target_hint'], 'profile/EXPERIENCE.md')
        for bad in (dict(kind='rumour'), dict(source='guess'), dict(text=' '), dict(scope='application'),
                    dict(application_id='APP-000000000000'), dict(target='notes.txt'), dict(text='x' * 2001)):
            args = dict(kind='fact', text='Fictional fact.', source='chat')
            args.update(bad)
            with self.subTest(bad=list(bad)), self.assertRaises(ValueError):
                learn.queue(self.ws, **args)

    def test_application_scope_needs_a_real_application(self):
        store = Store(self.ws, test=True)
        app = store.create('Example Corp', 'Data Analyst', 'Fictional JD text.', 'fixture', 'claude-code')
        item = learn.queue(self.ws, 'preference', 'For this company, lead with forecasting work.', 'chat',
                           scope='application', application_id=app['application_id'], test=True)
        self.assertEqual(item['application_id'], app['application_id'])
        self.assertNotIn('target_hint', item)
        with self.assertRaises(ValueError):
            learn.queue(self.ws, 'preference', 'Unknown application.', 'chat', scope='application',
                        application_id='APP-0123456789ab', test=True)

    def test_queue_apply_with_learning_ids_then_resolve(self):
        voice = learn.queue(self.ws, 'voice', 'Sign off with "Thanks," above the signature.', 'draft-edit')
        other = learn.queue(self.ws, 'exclusion', 'Never show data-entry roles.', 'chat')
        self.assertEqual(jf.status(str(self.ws))['learning_pending'], 2)
        event = self.apply('voice', learning_ids=[voice['id']])
        self.assertEqual(event['learning_ids'], [voice['id']])
        with self.assertRaises(ValueError):
            learn.resolve(self.ws, event['event_id'], [other['id']])  # not listed by that event
        with self.assertRaises(ValueError):
            learn.resolve(self.ws, 'EV-' + '0' * 32, [voice['id']])  # no such event
        with self.assertRaises(ValueError):
            learn.resolve(self.ws, self.demo.init_event['event_id'])  # lists no learning ids
        result = learn.resolve(self.ws, event['event_id'], [voice['id']])
        self.assertEqual(result['saved'], [voice['id']])
        saved = next(i for i in learn.items(self.ws) if i['id'] == voice['id'])
        self.assertEqual((saved['status'], saved['event_id']), ('saved', event['event_id']))
        self.assertIn('resolved_at', saved)
        self.assertEqual(learn.resolve(self.ws, event['event_id'])['already_saved'], [voice['id']])  # idempotent
        self.assertEqual(len(self.lines()), 2)  # one line per item, updated in place
        self.assertEqual(jf.status(str(self.ws))['learning_pending'], 1)
        self.assertEqual(learn.pending_count(self.ws), 1)
        self.assertTrue(learn.listing(self.ws)['items'][0]['event_active'])

    def test_dismiss_and_forget(self):
        item = learn.queue(self.ws, 'fact', 'Fictional expected pay for a second city.', 'chat', sensitive=True)
        kept = learn.queue(self.ws, 'market', 'Recruiters reply faster to short subject lines.', 'outcome')
        with self.assertRaises(ValueError):
            learn.dismiss(self.ws, [item['id']], ' ')
        with self.assertRaises(ValueError):
            learn.dismiss(self.ws, ['LRN-0123456789ab'], 'Unknown')
        learn.dismiss(self.ws, [item['id']], 'User does not want it stored.', forget=True)
        stored = next(i for i in self.lines() if i['id'] == item['id'])
        self.assertEqual((stored['status'], stored['reason']), ('dismissed', 'User does not want it stored.'))
        self.assertNotIn('second city', read_text(self.ws / layout.INBOX))
        self.assertEqual([i['id'] for i in learn.items(self.ws, pending_only=True)], [kept['id']])
        event = self.apply('market-note', paths=('profile/MARKET.md',), change_type='market', learning_ids=[kept['id']])
        learn.resolve(self.ws, event['event_id'])
        with self.assertRaises(ValueError):
            learn.dismiss(self.ws, [kept['id']], 'Too late')  # saved items are undone with memory.py rollback

    def test_sweep_reconciles_lists_pending_and_prints_a_checklist(self):
        forgotten = learn.queue(self.ws, 'voice', 'Shorter openings.', 'draft-edit')
        pending = learn.queue(self.ws, 'fact', 'Degree title is BSc Statistics.', 'chat')
        old = self.lines()
        for line in old:
            if line['id'] == pending['id']:
                line['at'] = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        (self.ws / layout.INBOX).write_bytes(''.join(json.dumps(l) + '\n' for l in old).encode('utf-8'))
        event = self.apply('openings', learning_ids=[forgotten['id']])  # the host forgot to resolve
        result = learn.sweep(self.ws)
        self.assertEqual(result['reconciled'], [{'id': forgotten['id'], 'event_id': event['event_id']}])
        self.assertEqual([p['id'] for p in result['pending']], [pending['id']])
        self.assertTrue(result['pending'][0]['stale'])
        self.assertEqual({c['kind'] for c in result['checklist']}, set(learn.KINDS))
        text = learn.sweep_text(learn.sweep(self.ws))
        self.assertIn('Degree title is BSc Statistics.', text)
        self.assertIn('Save this for next time?', text)
        learn.dismiss(self.ws, [pending['id']], 'Not needed')
        self.assertIn('Pending: none.', learn.sweep_text(learn.sweep(self.ws)))

    def test_compact_preview_then_proposal_applies_cleanly(self):
        self.assertEqual(learn.compact(self.ws)['status'], 'up-to-date')  # fresh workspace: template only
        voice = self.apply('voice-rule')
        market = self.apply('market-note', paths=('profile/MARKET.md',), change_type='market')
        story = self.apply('story', paths=('profile/STORY-BANK.md',), change_type='example')
        workflow = self.apply('workflow-rule', paths=('learning/LEARNED.md',), change_type='workflow')
        memory.rollback(market['event_id'], 'Undo that market note.', 'claude-code', 'TX-undo-market-note', self.ws)
        preview = learn.compact(self.ws)
        self.assertEqual((preview['status'], preview['rules']), ('preview', 2))
        text = preview['preview']
        self.assertTrue(text.startswith('# Learned rules\n'))
        self.assertIn('## Writing and voice\n\n- Fictional isolated test preference: voice-rule (%s, ' % voice['event_id'], text)
        self.assertIn('## Workflow\n\n- Fictional isolated test preference: workflow-rule (%s, ' % workflow['event_id'], text)
        for absent in (market['event_id'], story['event_id']):
            self.assertNotIn(absent, text)  # rolled back; a story is not a rule
        self.assertEqual(preview['unlinked_lines'], ['Short direct opening.'])  # appended text without an event id
        self.assertNotIn('proposal', preview)
        with self.assertRaises(ValueError):
            learn.compact(self.ws, exclude=['EV-' + '0' * 32])
        staged = learn.compact(self.ws, request='Yes, update my learned rules.', host='claude-code')
        self.assertEqual(staged['status'], 'proposal-ready')
        value = read_json(self.ws / staged['proposal'])
        self.assertEqual(value['changes'][0]['write']['text'], text)
        event = memory.apply(value, self.ws)
        self.assertEqual((event['status'], event['change_type'], event['affected_paths']),
                         ('committed', 'workflow', ['learning/LEARNED.md']))
        self.assertEqual(read_text(self.ws / layout.LEARNED), text)
        self.assertEqual(learn.compact(self.ws)['status'], 'up-to-date')  # the compaction itself is not a rule
        narrowed = learn.compact(self.ws, exclude=[voice['event_id']])
        self.assertEqual((narrowed['rules'], narrowed['dropped']), (1, [voice['event_id']]))

    def test_compact_without_the_users_words_is_not_applicable(self):
        self.apply('voice-rule')
        with self.assertRaises(ValueError):
            learn.compact(self.ws, request='Yes', host='made-up-host')
        staged = learn.compact(self.ws, request='Looks fine', host='codex')
        with self.assertRaises(ValueError):
            memory.apply(read_json(self.ws / staged['proposal']), self.ws)  # not a request to save anything

    def test_stats_on_missing_and_empty_stores(self):
        result = learn.stats(self.ws)
        for key in ('applications', 'outreach', 'skips'):
            self.assertFalse(result[key]['available'])
        self.assertEqual(result['learning'], {'pending': 0, 'saved': 0, 'dismissed': 0})
        (self.ws / layout.APPLICATIONS).mkdir(parents=True, exist_ok=True)
        atomic_json(self.ws / layout.OUTREACH, [])
        atomic_json(self.ws / layout.REGISTRY, {})
        result = learn.stats(self.ws, days=0)
        self.assertEqual((result['applications']['applications'], result['applications']['reply_rate']), (0, None))
        self.assertEqual((result['outreach']['records'], result['skips']['skipped']), (0, 0))
        (self.ws / layout.OUTREACH).write_bytes(b'{not json')
        self.assertFalse(learn.stats(self.ws)['outreach']['available'])
        with self.assertRaises(ValueError):
            learn.stats(self.ws, days=-1)

    def test_stats_counts_outcomes_by_family_company_type_and_window(self):
        store = Store(self.ws, test=True)

        def application(company, role, *statuses):
            record = store.create(company, role, 'Fictional JD for %s.' % role, 'fixture', 'claude-code')
            for status in statuses:
                store.record_event(record['application_id'], {'type': 'status', 'text': 'Fixture ' + status,
                                                              'basis': 'user-reported', 'status': status}, 'claude-code')
            return record['application_id']

        application('Example Corp', 'Senior Data Analyst', 'submitted', 'interview')
        application('Acme Analytics', 'Product Analyst', 'submitted', 'rejected')
        application('Other Co', 'Data Entry Clerk')
        old = application('Older Co', 'Data Analyst', 'submitted')
        path = store.directory(old) / 'record.json'
        record = read_json(path)
        record['created_at'] = record['status_history'][-1]['at'] = '2020-01-01T00:00:00+00:00'
        atomic_json(path, record)
        now = datetime.now(timezone.utc).isoformat()
        atomic_json(self.ws / layout.OUTREACH, {'schema_version': 1, 'items': [
            {'status': 'sent', 'sent_at': now}, {'status': 'replied', 'sent_at': now, 'replied_at': now},
            {'status': 'draft'}]})
        atomic_json(self.ws / layout.REGISTRY, {'jobs': [
            {'job_id': 'JOB-1', 'stage': 'Skipped', 'skip_reason': 'Needs relocation'},
            {'job_id': 'JOB-2', 'priority': {'label': 'Skip', 'reason': 'Needs relocation'}},
            {'job_id': 'JOB-3', 'stage': 'New'}]})
        result = learn.stats(self.ws, days=30, test=True)
        apps = result['applications']
        self.assertEqual((apps['applications'], apps['sent'], apps['replied'], apps['interviews'], apps['rejections']),
                         (3, 2, 2, 1, 1))
        self.assertEqual(apps['by_status'], {'interview': 1, 'rejected': 1, 'draft': 1})
        families = apps['by_role_family']
        self.assertEqual((families['data-analytics']['sent'], families['data-analytics']['reply_rate']), (1, 1.0))
        self.assertEqual(families['product-analytics']['rejections'], 1)
        self.assertEqual((families['clerical']['applications'], families['clerical']['sent']), (1, 0))
        self.assertEqual(set(apps['by_company_type']), {'in_house', 'consultancy', 'unknown'})
        self.assertEqual(apps['by_location'], {'unknown': dict(apps['by_location']['unknown'])})
        self.assertTrue(families['data-analytics']['low_sample'])
        self.assertEqual((result['outreach']['sent'], result['outreach']['replied']), (2, 1))
        self.assertEqual(result['skips']['top_reasons'], [{'reason': 'Needs relocation', 'count': 2}])
        self.assertEqual(learn.stats(self.ws, days=0, test=True)['applications']['applications'], 4)

    def test_cli_queue_list_resolve_sweep(self):
        script = str(SCRIPTS / 'learn.py')
        run = lambda *args: subprocess.run([sys.executable, script] + list(args), capture_output=True, text=True,
                                           timeout=60)
        queued = run('queue', '--kind', 'voice', '--text', 'Keep emails under 150 words.', '--source', 'draft-edit')
        self.assertEqual(queued.returncode, 0, queued.stderr)
        item = json.loads(queued.stdout)
        self.assertEqual(json.loads(run('list', '--pending').stdout)['items'][0]['id'], item['id'])
        self.assertIn('Keep emails under 150 words.', run('sweep').stdout)
        event = self.apply('cli', learning_ids=[item['id']])
        resolved = run('resolve', '--ids', item['id'], '--event', event['event_id'])
        self.assertEqual(json.loads(resolved.stdout)['saved'], [item['id']], resolved.stderr)
        self.assertEqual(json.loads(run('sweep', '--json').stdout)['pending'], [])
        self.assertNotEqual(run('queue', '--kind', 'voice', '--text', 'x', '--source', 'gossip').returncode, 0)
        self.assertIn('applications', json.loads(run('stats', '--days', '7').stdout))
        self.assertEqual(json.loads(run('compact').stdout)['status'], 'preview')


if __name__ == '__main__':
    unittest.main()
