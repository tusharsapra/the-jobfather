"""EXPERIENCE.md convention parser and evidence-ID helpers."""
import unittest

from support import DEMO

import evidence


DEMO_TEXT = (DEMO / 'profile' / 'EXPERIENCE.md').read_bytes().decode('utf-8')


class EvidenceTests(unittest.TestCase):
    def test_demo_parses(self):
        cat = evidence.parse(DEMO_TEXT)
        self.assertEqual(cat.prefixes, ['LKM', 'HBH', 'MFC', 'COOP', 'NBU'])
        self.assertIn('HBH-H01', cat.defined)
        self.assertEqual(cat.current_role()['org'], 'Lakeshore Metrics')
        self.assertEqual(len(cat.sections['LKM']['bullets']), 4)
        self.assertEqual(cat.sections['LKM']['bullets'][0]['refs'], ['LKM-01'])
        self.assertIn('**churn-risk model**', cat.sections['LKM']['bullets'][0]['text'])
        self.assertEqual(cat.cv_summary_refs, ['LKM-01', 'LKM-02', 'HBH-01'])
        self.assertIn('sql', evidence.vocabulary(cat))
        self.assertEqual(cat.cv_lists['tools'][:2], ['SQL', 'Python'])
        self.assertEqual([e['kind'] for e in cat.entries], ['role', 'role', 'role', 'group', 'education'])

    def test_ids_only_from_declared_prefixes(self):
        cat = evidence.parse(DEMO_TEXT)
        self.assertEqual(evidence.leaks('Cut churn 18% (LKM-01) during COVID-19.', cat), ['LKM-01'])
        self.assertEqual(evidence.leaks('COVID-19 and ISO-9001 are not IDs.', cat), [])

    def test_retired_ids_are_defined_but_inactive(self):
        text = DEMO_TEXT.replace('| LKM-04 | Mentored two junior analysts.', '| LKM-04 | (retired 2026-10-01) Mentored two junior analysts.')
        cat = evidence.parse(text)
        self.assertIn('LKM-04', cat.defined)
        self.assertNotIn('LKM-04', cat.active)
        with self.assertRaises(ValueError):
            evidence.require_known(['LKM-04'], cat)
        evidence.require_known(['LKM-04'], cat, active_only=False)

    def test_malformed_inputs_fail_with_line_numbers(self):
        cases = {
            'reserved prefix': DEMO_TEXT.replace('## [LKM]', '## [APP]').replace('LKM-', 'APP-'),
            'duplicate prefix': DEMO_TEXT.replace('## [HBH] Harbourview Health', '## [LKM] Harbourview Health'),
            'unknown key': DEMO_TEXT.replace('cv_weight: primary', 'cv_weight: primary\nmood: happy'),
            'bad date': DEMO_TEXT.replace('start: 2023-04', 'start: April 2023'),
            'foreign definition': DEMO_TEXT.replace('| HBH-03 |', '| LKM-07 |'),
            'dangling reference': DEMO_TEXT + '\nSee LKM-77.\n',
            'unclosed fence': DEMO_TEXT + '\n```jobfather-entry\nkind: role\n',
            'missing entry': DEMO_TEXT + '\n## [ZZZ] Nothing here\n',
        }
        for name, text in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    evidence.parse(text)

    def test_guidance_comments_are_ignored(self):
        text = DEMO_TEXT.replace('## CV skills', '<!-- e.g. [LKM-99] is only an example -->\n\n## CV skills')
        self.assertNotIn('LKM-99', evidence.parse(text).defined)


if __name__ == '__main__':
    unittest.main()
