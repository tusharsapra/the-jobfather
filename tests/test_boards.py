"""Public job-board listings (Greenhouse, Lever, Ashby) normalised to job-intake format, offline."""
import io
import json
import unittest
import urllib.error

from support import DemoWorkspace

import boards

GREENHOUSE = {'jobs': [
    {'id': 101, 'title': ' Senior Data Analyst ', 'location': {'name': 'Toronto, ON'},
     'absolute_url': 'https://boards.greenhouse.io/examplecorp/jobs/101', 'updated_at': '2026-10-10T12:00:00-04:00',
     'requisition_id': 'R-7', 'content': '&lt;p&gt;Own &lt;strong&gt;forecasting&lt;/strong&gt; &amp;amp; dashboards.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;SQL&lt;/li&gt;&lt;/ul&gt;'},
    {'id': 102, 'title': 'Office Coordinator', 'location': {'name': 'Toronto, ON'},
     'absolute_url': 'https://boards.greenhouse.io/examplecorp/jobs/102', 'updated_at': '2026-06-01T00:00:00Z', 'content': ''}]}
LEVER = [{'id': 'a1', 'text': 'Data Analyst', 'categories': {'location': 'Remote - Canada', 'commitment': 'Full-time'},
          'hostedUrl': 'https://jobs.lever.co/acme/a1', 'createdAt': 1790000000000, 'workplaceType': 'remote',
          'descriptionPlain': 'Analyse product usage.', 'lists': [{'text': 'You have', 'content': '<li>SQL</li><li>Python</li>'}],
          'additionalPlain': 'Benefits included.'}]
ASHBY = {'jobs': [{'id': 'z9', 'title': 'Analytics Engineer', 'location': 'Vancouver, BC', 'jobUrl': 'https://jobs.ashbyhq.com/acme/z9',
                   'publishedAt': '2026-10-01T09:00:00.000Z', 'descriptionPlain': 'Model data in dbt.', 'isRemote': False,
                   'employmentType': 'FullTime', 'compensation': {'compensationTierSummary': 'CA$100K - CA$120K'}}]}


def opener_for(payload, status=200):
    def opener(request, timeout=20):
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, 'error', {}, io.BytesIO(b''))
        body = io.BytesIO(json.dumps(payload).encode())
        body.__enter__ = lambda *a: body
        body.__exit__ = lambda *a: False
        return body
    return opener


class BoardTests(unittest.TestCase):
    def test_greenhouse_double_escaped_html_and_fields(self):
        jobs = boards.normalize('greenhouse', 'examplecorp', GREENHOUSE, 'Example Corp')
        job = jobs[0]
        self.assertEqual(job['role'], 'Senior Data Analyst')
        self.assertIn('forecasting & dashboards', job['full_jd'])
        self.assertIn('- SQL', job['full_jd'])
        self.assertEqual((job['requisition_id'], job['posted_date']), ('R-7', '2026-10-10'))
        self.assertEqual(job['sources'][0]['access'], 'readable')
        self.assertEqual(jobs[1]['sources'][0]['access'], 'partial')  # no description published

    def test_lever_and_ashby(self):
        lever = boards.normalize('lever', 'acme', LEVER, 'Acme Analytics')[0]
        self.assertIn('Python', lever['full_jd'])
        self.assertEqual((lever['employment_type'], lever['work_mode']), ('Full-time', 'remote'))
        ashby = boards.normalize('ashby', 'acme', ASHBY, 'Acme Analytics')[0]
        self.assertEqual(ashby['salary'], 'CA$100K - CA$120K')
        self.assertEqual(ashby['posted_date'], '2026-10-01')

    def test_filtering(self):
        jobs = boards.normalize('greenhouse', 'examplecorp', GREENHOUSE)
        self.assertEqual([j['role'] for j in boards.filter_jobs(jobs, ['data analyst'])], ['Senior Data Analyst'])
        import datetime
        self.assertEqual(len(boards.filter_jobs(jobs, None, ['toronto'], 30, today=datetime.date(2026, 10, 18))), 1)

    def test_fetch_errors_are_clear(self):
        with self.assertRaises(ValueError):
            boards.fetch('greenhouse', '../etc', opener_for({}))
        with self.assertRaises(ValueError) as ctx:
            boards.fetch('lever', 'gone', opener_for({}, status=404))
        self.assertIn('not found', str(ctx.exception))
        self.assertEqual(boards.fetch('ashby', 'acme', opener_for(ASHBY))['jobs'][0]['id'], 'z9')

    def test_targets_from_market_map(self):
        demo = DemoWorkspace(init=False)
        try:
            payloads = {'examplecorp': GREENHOUSE, 'acme': LEVER}

            def opener(request, timeout=20):
                board = 'examplecorp' if 'examplecorp' in request.full_url else 'acme'
                return opener_for(payloads[board])(request, timeout)
            batch, report = boards.targets(demo.root, pause=0, opener=opener)
            self.assertEqual([j['role'] for j in batch['jobs']], ['Senior Data Analyst', 'Data Analyst'])
            self.assertTrue(all(j['sources'][0]['url'].startswith('https://') for j in batch['jobs']))
            self.assertEqual(len(report), 2)
        finally:
            demo.close()


if __name__ == '__main__':
    unittest.main()
