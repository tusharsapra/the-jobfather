"""Outgoing-text checks: limits, placeholders, IDs, the user's avoid list, stock phrases."""
import unittest

from support import DEMO

import evidence
from check_text import avoid_list, check

CAT = evidence.parse((DEMO / 'profile' / 'EXPERIENCE.md').read_bytes().decode('utf-8'))
VOICE = (DEMO / 'profile' / 'VOICE.md').read_bytes().decode('utf-8')


class CheckTextTests(unittest.TestCase):
    def test_limits_include_spaces_by_default(self):
        self.assertFalse(check('a b', max_chars=2)['ok'])
        self.assertTrue(check('a b', max_chars=2, include_spaces=False)['ok'])
        self.assertFalse(check('one two three', max_words=2)['ok'])
        self.assertFalse(check('line one\nline two', one_line=True)['ok'])

    def test_placeholders_and_ids_block(self):
        self.assertFalse(check('Use this TODO')['ok'])
        self.assertFalse(check('Dear {{name}}')['ok'])
        result = check('I cut churn 18% (LKM-01).', catalog=CAT)
        self.assertFalse(result['ok'])
        self.assertEqual(result['id_check'], 'done')
        self.assertEqual(check('I cut churn 18%.')['id_check'], 'skipped')

    def test_users_avoid_list_blocks_and_stock_phrases_warn(self):
        avoid = avoid_list(VOICE)
        self.assertIn('passionate', avoid)
        blocked = check('I am passionate about data.', avoid=avoid)
        self.assertFalse(blocked['ok'])
        warned = check('I have a proven track record with dashboards.', avoid=avoid)
        self.assertTrue(warned['ok'])
        self.assertTrue(any('proven track record' in w for w in warned['warnings']))

    def test_heavy_dashes_warn(self):
        result = check('One — two — three — four.')
        self.assertTrue(result['ok'])
        self.assertTrue(any('dashes' in w for w in result['warnings']))


if __name__ == '__main__':
    unittest.main()
