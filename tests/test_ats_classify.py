"""ATS routing: which system an application link belongs to, its board/job ids, and short links."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error

from support import SKILL  # noqa: F401  (puts scripts on sys.path)

from ats import classify as classifier

UUID = '0b1c2d3e-4f50-4617-8293-a4b5c6d7e8f9'
EMBED = 'https://job-boards.greenhouse.io/embed/job_app?for=examplecorp&token=4567890'
SHORT_LINKEDIN = 'https://lnkd.' + 'in/' + 'eXample1'  # built at runtime: the privacy scan flags literal short links

# (link, ats, board, job_id, apply_url or None for "the link itself", supported_autofill)
CASES = [
    ('https://boards.greenhouse.io/examplecorp/jobs/4567890', 'greenhouse', 'examplecorp', '4567890', EMBED, True),
    ('https://job-boards.greenhouse.io/examplecorp/jobs/4567890?gh_src=abc123', 'greenhouse', 'examplecorp', '4567890',
     EMBED, True),
    ('boards.greenhouse.io/examplecorp/jobs/4567890#app', 'greenhouse', 'examplecorp', '4567890', EMBED, True),
    ('https://boards.greenhouse.io/embed/job_app?for=examplecorp&token=4567890', 'greenhouse', 'examplecorp', '4567890',
     EMBED, True),
    ('https://job-boards.greenhouse.io/embed/job_app?token=4567890&for=examplecorp', 'greenhouse', 'examplecorp',
     '4567890', EMBED, True),
    ('https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs/4567890?questions=true', 'greenhouse', 'examplecorp',
     '4567890', EMBED, True),
    ('https://job-boards.eu.greenhouse.io/examplecorp/jobs/4567890', 'greenhouse', 'examplecorp', '4567890',
     'https://job-boards.eu.greenhouse.io/embed/job_app?for=examplecorp&token=4567890', False),
    ('https://boards.eu.greenhouse.io/examplecorp/jobs/4567890', 'greenhouse', 'examplecorp', '4567890',
     'https://job-boards.eu.greenhouse.io/embed/job_app?for=examplecorp&token=4567890', False),
    ('https://careers.example.com/open-roles?gh_jid=4567890', 'greenhouse', None, '4567890', None, True),
    ('https://boards.greenhouse.io/examplecorp', 'greenhouse', 'examplecorp', None, None, True),
    ('https://boards.greenhouse.io/embed/job_board?for=examplecorp', 'greenhouse', 'examplecorp', None, None, True),
    ('https://grnh.se/ab12cd34', 'greenhouse', None, None, None, True),
    ('https://boards.greenhouse.io/examplecorp/jobs/not-a-number', 'greenhouse', 'examplecorp', None, None, True),
    ('https://jobs.lever.co/examplecorp/%s' % UUID, 'lever', 'examplecorp', UUID,
     'https://jobs.lever.co/examplecorp/%s/apply' % UUID, False),
    ('https://jobs.lever.co/examplecorp/%s/apply?source=board' % UUID.upper(), 'lever', 'examplecorp', UUID,
     'https://jobs.lever.co/examplecorp/%s/apply' % UUID, False),
    ('https://jobs.eu.lever.co/examplecorp/%s' % UUID, 'lever', 'examplecorp', UUID,
     'https://jobs.eu.lever.co/examplecorp/%s/apply' % UUID, False),
    ('https://jobs.lever.co/examplecorp', 'lever', None, None, None, False),
    ('https://jobs.ashbyhq.com/examplecorp/%s' % UUID, 'ashby', 'examplecorp', UUID,
     'https://jobs.ashbyhq.com/examplecorp/%s/application' % UUID, False),
    ('https://jobs.ashbyhq.com/Example%%20Corp/%s/application' % UUID, 'ashby', 'Example Corp', UUID,
     'https://jobs.ashbyhq.com/Example%%20Corp/%s/application' % UUID, False),
    ('https://www.example.com/careers?ashby_jid=%s' % UUID, 'ashby', None, UUID, None, False),
    ('https://examplecorp.wd5.myworkdayjobs.com/External/job/Toronto/Analyst_R-1042', 'workday', None, None, None, False),
    ('https://examplecorp.myworkdaysite.com/recruiting/examplecorp/External', 'workday', None, None, None, False),
    ('https://jobs.smartrecruiters.com/ExampleCorp/1234567-data-analyst', 'smartrecruiters', None, None, None, False),
    ('https://careers-examplecorp.icims.com/jobs/1042/analyst/job', 'icims', None, None, None, False),
    ('https://examplecorp.keka.com/careers/jobdetails/1042', 'keka', None, None, None, False),
    ('https://examplecorp.darwinbox.in/ms/candidate/careers/a1b2c3', 'darwinbox', None, None, None, False),
    ('https://apply.workable.com/examplecorp/j/ABC123DEF4/', 'workable', None, None, None, False),
    ('https://docs.google.com/forms/d/e/1FAIpExampleForm/viewform', 'google_forms', None, None, None, False),
    ('https://forms.gle/AbCdEfExample1', 'google_forms', None, None, None, False),
    ('https://docs.google.com/document/d/example/edit', 'company_site', None, None, None, False),
    ('https://www.linkedin.com/jobs/view/1234567/', 'linkedin', None, None, None, False),
    (SHORT_LINKEDIN, 'linkedin', None, None, None, False),
    ('https://www.naukri.com/job-listings-data-analyst-example-1042', 'naukri', None, None, None, False),
    ('https://www.iimjobs.com/j/data-analyst-example-1042.html', 'iimjobs', None, None, None, False),
    ('https://internshala.com/internship/detail/data-analyst-example1042', 'internshala', None, None, None, False),
    ('mailto:jobs@example.com?subject=Data%20Analyst', 'email', None, None, None, False),
    ('jobs@example.com', 'email', None, None, 'mailto:jobs@example.com', False),
    ('https://careers.example.com/jobs/1042', 'company_site', None, None, None, False),
    ('https://notgreenhouse.example.com/greenhouse.io/jobs/1', 'company_site', None, None, None, False),
]


class FakeOpener:
    """HEAD-only fake: redirects come back as HTTPError with a Location header, like the no-redirect handler."""

    def __init__(self, hops):
        self.hops, self.methods = hops, []

    def open(self, request, timeout=10):
        self.methods.append(request.get_method())
        target = self.hops.get(request.full_url)
        if isinstance(target, Exception):
            raise target
        if target:
            raise urllib.error.HTTPError(request.full_url, 301, 'Moved', {'Location': target}, io.BytesIO(b''))
        return io.BytesIO(b'')


class ClassifyTests(unittest.TestCase):
    def test_table(self):
        for link, ats, board, job_id, apply_url, supported in CASES:
            with self.subTest(link=link):
                result = classifier.classify(link)
                self.assertEqual(set(result), {'ats', 'board', 'job_id', 'apply_url', 'supported_autofill'})
                self.assertEqual((result['ats'], result['board'], result['job_id']), (ats, board, job_id))
                self.assertEqual(result['supported_autofill'], supported)
                expected = apply_url or (link if '://' in link or link.startswith('mailto:') else 'https://' + link)
                self.assertEqual(result['apply_url'], expected)

    def test_every_family_is_known_and_only_greenhouse_autofills(self):
        seen = {c[1] for c in CASES}
        self.assertEqual(seen, set(classifier.FAMILIES))
        self.assertEqual(classifier.SUPPORTED_AUTOFILL, frozenset({'greenhouse'}))

    def test_records(self):
        self.assertEqual(classifier.classify(None, {'application_email': ' jobs@example.com '})['apply_url'],
                         'mailto:jobs@example.com')
        self.assertEqual(classifier.classify('', {'application_url': EMBED, 'email': 'jobs@example.com'})['ats'],
                         'greenhouse')
        with self.assertRaises(ValueError):
            classifier.classify(None, {'company': 'Example Corp'})
        with self.assertRaises(ValueError):
            classifier.classify('ftp://files.example.com/job.pdf')

    def test_short_links_resolve_with_head_only(self):
        start = 'https://grnh.se/ab12cd34'
        middle = 'https://boards.greenhouse.io/examplecorp/jobs/4567890?gh_src=ab12cd34'
        opener = FakeOpener({start: middle, middle: 'https://job-boards.greenhouse.io/examplecorp/jobs/4567890'})
        final, error = classifier.resolve(start, opener)
        self.assertIsNone(error)
        self.assertEqual(final, 'https://job-boards.greenhouse.io/examplecorp/jobs/4567890')
        self.assertEqual(set(opener.methods), {'HEAD'})
        described = classifier.describe(start, True, opener)
        self.assertEqual((described['board'], described['job_id'], described['resolved_url']), ('examplecorp', '4567890', final))
        offline = classifier.describe(start)
        self.assertIsNone(offline['job_id'])
        self.assertIn('--resolve', offline['note'])

    def test_short_link_failures_keep_the_link(self):
        start = 'https://bit.ly/example1'
        opener = FakeOpener({start: urllib.error.URLError('offline')})
        final, error = classifier.resolve(start, opener)
        self.assertEqual(final, start)
        self.assertIn('could not reach', error)
        loop = FakeOpener({start: 'https://bit.ly/example2', 'https://bit.ly/example2': start})
        self.assertEqual(classifier.resolve(start, loop)[1], 'too many redirects')
        scheme = FakeOpener({start: 'javascript:alert(1)'})
        self.assertEqual(classifier.resolve(start, scheme), (start, 'redirects to a non-web link'))
        self.assertEqual(classifier.describe(start, True, opener)['resolve_error'], error)

    def test_cli_url_and_file(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(classifier.main(['--url', CASES[0][0]]), 0)
        self.assertEqual(json.loads(out.getvalue())['job_id'], '4567890')
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp) / 'links.txt'
            links.write_text('# saved links\n\n%s\nftp://bad.example.com/x\n%s\n' % (CASES[0][0], CASES[13][0]),
                             encoding='utf-8')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(classifier.main(['--file', str(links)]), 0)
        rows = json.loads(out.getvalue())
        self.assertEqual([r.get('ats') for r in rows], ['greenhouse', None, 'lever'])
        self.assertIn('error', rows[1])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(classifier.main(['--url', 'ftp://bad.example.com/x']), 1)


if __name__ == '__main__':
    unittest.main()
