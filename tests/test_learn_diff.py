"""Draft vs sent comparison: cleaning, word diff, recurring edits, targeted checks, private text kept out."""
import json
import subprocess
import sys
import unittest

from support import SCRIPTS, DemoWorkspace

import layout
import learn_diff
from local import atomic_json, atomic_text, read_json

SIGNATURE = ('<div><b>Alex Rivera</b><br>Senior Data Analyst<br>+1 416-555-0142 | '
             '<a href="https://alexrivera.example">alexrivera.example</a></div>')
QUOTE = ('\n\nOn Mon, Oct 5, 2026 at 9:00 AM Sam Lee <\nsam.lee@example.com> wrote:\n'
         '> Quoted recruiter text\n> more quoted text')


def draft(name, line):
    return 'Hi %s,\n\n%s If the role is still open, I would be glad to talk.\n\nBest regards,\nAlex' % (name, line)


def sent(name, line):
    return ('Hi %s,\n\n%s I would be glad to talk.\n\nPortfolio: alexrivera.example <https://alexrivera.example/>\n\n'
            'Thanks,\n-- \nAlex Rivera\nSenior Data Analyst%s' % (name, line, QUOTE))


PAIRS = [
    {'thread_id': 't1', 'subject': 'Fictional subject one', 'recipient': 'sam.lee@example.com',
     'draft': draft('Sam', 'I build dashboards finance uses every week.'),
     'sent': sent('Sam', 'I build dashboards finance uses every week.')},
    {'thread_id': 't2', 'subject': 'Fictional subject two', 'recipient': 'jo@example.com',
     'draft': draft('Jo', 'I cut churn 18% with a churn-risk model.'),
     'sent': sent('Jo', 'I cut churn 18% with a churn-risk model.')},
    {'thread_id': 't3', 'draft': draft('Kim', 'I ran readouts for 14 pricing experiments.'),
     'sent': sent('Kim', 'I ran readouts for 14 pricing experiments.')},
    {'thread_id': 't4',  # only mail-client artefacts differ
     'draft': 'Hi Lee ,\n\nSee alexrivera.example for “work” samples.\nIt’s short.\n\nThanks,\nAlex',
     'sent': ('<div dir="ltr">Hi Lee,<br><br>See <a href="https://alexrivera.example">alexrivera.example</a> for '
              '"work" samples.<br>It&#39;s short.<br><br><div><br></div>Thanks,<br>Alex</div>'
              '<div class="gmail_signature">Alex Rivera<br>Senior Data Analyst</div>'
              '<div class="gmail_quote">On Tue, Oct 6, 2026 Sam wrote: older thread</div>')},
    {'thread_id': 't5', 'draft': 'Hi,\n\nDeleted before sending.', 'sent': None},
]


class LearnDiffTests(unittest.TestCase):
    def setUp(self):
        self.demo = DemoWorkspace(init=False)
        self.ws = self.demo.root
        atomic_text(self.ws / layout.SIGNATURE, SIGNATURE)

    def tearDown(self):
        self.demo.close()

    def run_pairs(self, pairs=PAIRS, **options):
        result = learn_diff.run(self.ws, learn_diff.load_pairs(pairs), **options)
        folder = self.ws / result['directory']
        return result, read_json(folder / 'edit-comparison.json'), read_json(folder / 'edit-counts.json')

    def test_cleaning_strips_signature_quotes_and_artefacts(self):
        signature = learn_diff.signature_tokens(SIGNATURE)
        self.assertEqual(learn_diff.clean(sent('Sam', 'One line.'), signature),
                         ['Hi Sam,', '', 'One line. I would be glad to talk.', '', 'Portfolio: alexrivera.example',
                          '', 'Thanks,'])
        self.assertEqual(learn_diff.clean('Hi,\n\nBody.\n\nThanks,\nAlex', signature), ['Hi,', '', 'Body.', '', 'Thanks,'])
        forwarded = 'Short note.\n\n---------- Forwarded message ---------\nFrom: Someone\nOld text'
        self.assertEqual(learn_diff.clean(forwarded), ['Short note.'])
        outlook = 'Reply text.\n\nFrom: Someone Else\nSent: Monday\nSubject: Old\n\nOld body'
        self.assertEqual(learn_diff.clean(outlook), ['Reply text.'])
        self.assertEqual(learn_diff.clean('On Mondays I review dashboards, as I wrote:\nkept'),
                         ['On Mondays I review dashboards, as I wrote:', 'kept'])  # not a reply header

    def test_recurring_edits_phrases_and_checks(self):
        result, comparison, counts = self.run_pairs()
        self.assertEqual(result['pairs'], {'total': 5, 'compared': 4, 'changed': 3, 'unchanged': 1, 'skipped': 1})
        rows = {row['id']: row for row in comparison['pairs']}
        self.assertEqual(rows['t4']['status'], 'unchanged')  # spacing, quotes, autolink, HTML, signature, quote
        self.assertEqual(rows['t5'], {'id': 't5', 'thread_id': 't5', 'status': 'skipped',
                                      'reason': 'no sent version (not sent, or not matched)'})
        caveat = next(e for e in counts['edits'] if e['op'] == 'delete')
        self.assertEqual((caveat['before'], caveat['messages'], caveat['examples']),
                         ('If the role is still open,', 3, ['t1', 't2', 't3']))
        closing = next(e for e in counts['edits'] if e['op'] == 'replace')
        self.assertEqual((closing['before'], closing['where']), ('Best regards,', 'closing'))
        self.assertIn({'text': 'if the role is still open', 'messages': 3, 'examples': ['t1', 't2', 't3']},
                      counts['removed_phrases'])
        added = {row['text'] for row in counts['added_phrases']}
        self.assertTrue({'thanks', 'portfolio alexrivera.example'} <= added)
        hits = {c['id']: c['hits'] for c in counts['checks']}
        self.assertEqual((hits['caveat-removed'], hits['link-line-added'], hits['thanks-close-added'],
                          hits['regards-close-removed'], hits['greeting-changed']), (3, 3, 3, 3, 0))
        self.assertEqual(result['top_edits'][0]['messages'], 3)
        self.assertEqual(result['voice_conflicts'], [])

    def test_outputs_keep_no_message_text_or_people(self):
        long_line = ' '.join('word%d' % n for n in range(40))
        pairs = PAIRS + [{'thread_id': 't6', 'draft': 'Hi,\n\nShort.', 'sent': 'Hi,\n\n' + long_line}]
        result, comparison, counts = self.run_pairs(pairs)
        stored = json.dumps([comparison, counts])
        for private in ('Quoted recruiter text', 'sam.lee@example.com', 'jo@example.com', 'Fictional subject',
                        'Senior Data Analyst', '555-0142', 'I build dashboards finance uses every week', long_line):
            self.assertNotIn(private, stored)
        clipped = next(e for e in next(r for r in comparison['pairs'] if r['id'] == 't6')['edits'])
        self.assertTrue(clipped['clipped'])
        self.assertTrue(result['directory'].startswith('learning/diffs/'))
        with self.assertRaises(ValueError):
            learn_diff.run(self.ws, learn_diff.load_pairs(PAIRS), run_id=result['run_id'])  # never overwrite a run

    def test_explicit_voice_rules_beat_contrary_examples(self):
        pairs = [{'thread_id': 'v%d' % n, 'draft': 'Hi,\n\nI build dashboards.\n\nThanks,',
                  'sent': 'Hi,\n\nI am passionate about dashboards.\n\nThanks,'} for n in range(2)]
        result, _, _ = self.run_pairs(pairs)
        rules = ' '.join(c['rule'] for c in result['voice_conflicts'])
        self.assertIn('"passionate" under words you avoid', rules)

    def test_custom_checks_and_validation(self):
        checks = [{'id': 'portfolio-line', 'label': 'Portfolio line added', 'where': 'any-line',
                   'phrases': ['portfolio'], 'expect': 'added'}]
        result, _, counts = self.run_pairs(checks=checks)
        self.assertEqual([(c['id'], c['hits']) for c in counts['checks']], [('portfolio-line', 3)])
        for bad in ([], [{'id': 'Bad Id', 'phrases': ['x']}], [{'id': 'x', 'where': 'middle', 'phrases': ['x']}],
                    [{'id': 'x', 'expect': 'added'}], [{'id': 'x', 'pattern': '('}]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                learn_diff.load_checks(bad)
        with self.assertRaises(ValueError):
            learn_diff.load_pairs({'pairs': 'not a list'})

    def test_cli_pairs_file_and_folders(self):
        source = self.demo.base / 'pairs.json'
        atomic_json(source, PAIRS)
        script = str(SCRIPTS / 'learn_diff.py')
        done = subprocess.run([sys.executable, script, '--pairs', str(source), '--run-id', 'cli-run'],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)['pairs']['changed'], 3)
        self.assertTrue((self.ws / 'learning/diffs/cli-run/edit-counts.json').is_file())
        drafts, sents = self.demo.base / 'drafts', self.demo.base / 'sent'
        for folder in (drafts, sents):
            folder.mkdir()
        for pair in PAIRS[:3]:
            (drafts / (pair['thread_id'] + '.txt')).write_bytes(pair['draft'].encode('utf-8'))
            (sents / (pair['thread_id'] + '.txt')).write_bytes(pair['sent'].encode('utf-8'))
        (drafts / 'unsent.txt').write_bytes(b'Hi,\n\nNever sent.')
        done = subprocess.run([sys.executable, script, '--drafts', str(drafts), '--sent', str(sents), '--no-signature'],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        summary = json.loads(done.stdout)
        self.assertEqual((summary['pairs']['changed'], summary['pairs']['skipped']), (3, 1))
        bad = subprocess.run([sys.executable, script, '--drafts', str(drafts)], capture_output=True, text=True,
                             timeout=60)
        self.assertNotEqual(bad.returncode, 0)


if __name__ == '__main__':
    unittest.main()
