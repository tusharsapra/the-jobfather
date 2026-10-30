"""Market model: validation, taxonomy fallback, ranking and query building."""
import copy
import json
import unittest

from support import DEMO, DemoWorkspace

import layout
import market


DEMO_MARKET = json.loads((DEMO / 'profile' / 'market.json').read_bytes().decode('utf-8'))


class MarketTests(unittest.TestCase):
    def test_demo_validates_and_supplies_taxonomy(self):
        market.validate(copy.deepcopy(DEMO_MARKET))
        demo = DemoWorkspace(init=False)
        try:
            tax = market.taxonomy(demo.root)
            self.assertEqual(tax['source'], 'market')
            self.assertIn('in_house', tax['dimensions']['employer_type'])
            (demo.root / layout.MARKET_JSON).write_bytes(b'{"schema_version": 1}')
            self.assertEqual(market.taxonomy(demo.root)['source'], 'generic')
            (demo.root / layout.MARKET_JSON).write_bytes(b'{"schema_version": 1, "taxonomy": {"role_track": {"values": {}}}}')
            with self.assertRaises(ValueError):
                market.taxonomy(demo.root)
        finally:
            demo.close()

    def test_bad_shapes_rejected(self):
        for mutate in (lambda m: m['role_families'][0].update(status='maybe'),
                       lambda m: m['exclusions'].append({'kind': 'vibes', 'value': 'x'}),
                       lambda m: m['target_companies'].append({'name': 'X', 'careers_url': 'ftp://x'}),
                       lambda m: m['skills']['must'].append({'name': 'R', 'source': 'guess'})):
            value = copy.deepcopy(DEMO_MARKET)
            mutate(value)
            with self.assertRaises(ValueError):
                market.validate(value)

    def test_location_rank_and_queries(self):
        self.assertEqual(market.location_rank(None, 'Toronto, ON (Hybrid)', DEMO_MARKET), 1)
        self.assertEqual(market.location_rank(None, 'Remote - Canada', DEMO_MARKET), 2)
        self.assertGreater(market.location_rank(None, 'Vancouver, BC', DEMO_MARKET), 2)
        query = market.build_queries(DEMO_MARKET, 'linkedin', 'Toronto')[0]['query']
        self.assertIn('"Data Analyst"', query)
        self.assertIn('NOT ("Data Entry Clerk"', query)
        self.assertNotIn('Analytics Engineer', query)  # stretch titles stay out of first-page searches
        indeed = market.build_queries(DEMO_MARKET, 'indeed', 'Toronto')[0]
        self.assertEqual(indeed['platform'], 'indeed')
        self.assertIn('"Data Analyst"', indeed['query'])
        self.assertIn(' -"Data Entry Clerk"', indeed['query'])
        self.assertNotIn('NOT', indeed['query'])


if __name__ == '__main__':
    unittest.main()
