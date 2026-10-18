"""Naukri parsers, URL building, job references and checkpoint rules, offline on sanitized fixtures."""
import json
import os
import re
import unittest
from datetime import date
from unittest import mock

from naukri_support import FIXTURES, fixture, fixture_job_id

from jobfather_mcp.errors import JFError
from jobfather_mcp.naukri import TOOL_NAMES, parse
from jobfather_mcp.naukri import site as site_mod

TODAY = date(2026, 10, 19)


def site():
    with mock.patch.dict(os.environ, {'JOBFATHER_TEST_MODE': ''}):
        site_mod.reset()
        return site_mod.Site()


class FieldParserTests(unittest.TestCase):
    def test_experience(self):
        self.assertEqual(parse.experience('6-11 Yrs'), {'min_years': 6, 'max_years': 11, 'text': '6-11 Yrs'})
        self.assertEqual(parse.experience('6 - 11 years')['max_years'], 11)
        self.assertEqual(parse.experience('10+ Yrs')['min_years'], 10)
        self.assertEqual(parse.experience('Fresher')['max_years'], 0)
        self.assertEqual(parse.experience(None, '3', '8'), {'min_years': 3, 'max_years': 8, 'text': None})

    def test_salary(self):
        self.assertEqual(parse.salary('Not disclosed'), {'text': 'Not disclosed', 'min_inr': None, 'max_inr': None,
                                                         'disclosed': False})
        self.assertEqual(parse.salary('15-30 Lacs PA')[ 'min_inr'], 1500000)
        self.assertEqual(parse.salary('15-30 Lacs PA')['max_inr'], 3000000)
        varied = parse.salary('10-15 Lacs PA (Including Variable: 5.0%)')
        self.assertEqual((varied['min_inr'], varied['max_inr'], varied['disclosed']), (1000000, 1500000, True))
        self.assertEqual(parse.salary('1-1.5 Cr PA')['max_inr'], 15000000)
        self.assertEqual(parse.salary('50,000-1.5 Lacs PA')['min_inr'], 50000)
        self.assertEqual(parse.salary('25,000-35,000 per month')['max_inr'], 420000)
        hidden = parse.salary('Not Disclosed', {'minimumSalary': 0, 'maximumSalary': 0, 'hideSalary': True})
        self.assertFalse(hidden['disclosed'])
        exact = parse.salary(None, {'minimumSalary': 1300000, 'maximumSalary': 2300000, 'hideSalary': False,
                                    'salaryText': '13-23 Lacs P.A.'})
        self.assertEqual((exact['min_inr'], exact['max_inr'], exact['text']), (1300000, 2300000, '13-23 Lacs P.A.'))

    def test_posted(self):
        self.assertEqual(parse.posted('5 Days Ago', today=TODAY)['date'], '2026-10-14')
        self.assertEqual(parse.posted('Few Hours Ago', today=TODAY)['date'], '2026-10-19')
        self.assertIsNone(parse.posted('30+ Days Ago', today=TODAY)['date'])
        self.assertEqual(parse.posted('3 weeks ago', today=TODAY)['date'], '2026-09-28')
        self.assertEqual(parse.posted(None, '2026-10-13 11:16:47')['date'], '2026-10-13')
        stamp = parse.posted('5 Days Ago', 1791870407969)
        self.assertEqual(stamp, {'text': '5 Days Ago', 'date': '2026-10-13'})

    def test_locations_and_modes(self):
        self.assertEqual(parse.split_locations('Hybrid - Alpha, Beta (All Areas)'), (['Alpha', 'Beta (All Areas)'], 'hybrid'))
        self.assertEqual(parse.split_locations('Remote'), ([], 'remote'))
        self.assertEqual(parse.split_locations('Gamma / Region, Delta'), (['Gamma / Region', 'Delta'], None))
        self.assertEqual(parse.work_mode_from('3', {'3': 'hybrid'}), 'hybrid')
        self.assertEqual(parse.work_mode_from('Work from office', {}), 'office')

    def test_html_to_text_and_limits(self):
        text = parse.html_to_text('<p>One<br>Two</p><ul><li>a</li><li>b</li></ul><script>x()</script>')
        self.assertEqual(text, 'One\nTwo\n\n• a\n• b')
        self.assertEqual(parse.limited('x' * 30, 20), ('x' * 20, True))
        self.assertTrue(parse.snippet('<b>' + 'word ' * 200 + '</b>').endswith('…'))


class SelectorTests(unittest.TestCase):
    MARKUP = ('<div id="a" class="one two" data-k="v"><span class="x">Hi</span><p><a href="/job-listings-x-0123456789">'
              'T</a></p></div><div class="one"><label for="q1">Role: </label><span>R &amp; D</span></div>')

    def test_selectors(self):
        root = parse.parse_html(self.MARKUP)
        self.assertEqual(len(root.select('div.one')), 2)
        self.assertEqual(root.select_one('#a > span.x').text(), 'Hi')
        self.assertEqual(len(root.select('#a > a')), 0)
        self.assertEqual(len(root.select('#a a[href*="job-listings"]')), 1)
        self.assertEqual(len(root.select('[data-k=v], .missing')), 1)
        self.assertEqual(len(root.select("[CLASS*='ONE' i]")), 2)  # attribute names are case-insensitive
        self.assertEqual(len(root.select("[class*='ONE' i]")), 2)
        self.assertEqual(parse.labelled_values(root), {'role': 'R & D'})
        with self.assertRaises(ValueError):
            root.select('div:has(span)')


class FixtureParserTests(unittest.TestCase):
    def setUp(self):
        self.site = site()

    def tearDown(self):
        site_mod.reset()

    def test_manifests_match_files(self):
        import hashlib
        for folder in sorted(p for p in FIXTURES.iterdir() if p.is_dir()):
            manifest = json.loads((folder / 'MANIFEST.json').read_text())
            self.assertEqual(manifest['sanitizer_version'], '1')
            self.assertRegex(manifest['site_profile'], r'^\d{4}-\d{2}-\d{2}$')
            for name, digest in manifest['files'].items():
                self.assertEqual(hashlib.sha256((folder / name).read_bytes()).hexdigest(), digest, folder.name + name)

    def test_search_json_and_dom_agree(self):
        responses, html, _ = fixture('search')
        body = next(r['body'] for r in responses if re.search(self.site.section('search')['response']['url_regex'],
                                                                r['url']))
        parsed = parse.search_from_json(body, self.site, today=TODAY)
        self.assertGreater(len(parsed['jobs']), 3)
        self.assertEqual(parsed['failed'], 0)
        self.assertGreater(parsed['total'], 1000)
        first = parsed['jobs'][0]
        self.assertRegex(first['job_id'], r'^\d{12}$')
        self.assertTrue(first['url'].startswith('https://www.naukri.com/job-listings-'))
        self.assertTrue(first['url'].endswith(first['job_id']))
        self.assertIn(first['apply_type'], ('naukri', 'company_site'))
        self.assertTrue(all(j['title'] and j['company'] for j in parsed['jobs']))
        self.assertTrue(any(j['salary']['disclosed'] for j in parsed['jobs']))
        self.assertTrue(any(j['work_mode'] == 'hybrid' for j in parsed['jobs']))
        self.assertTrue(any(j['apply_type'] == 'company_site' for j in parsed['jobs']))
        dom = parse.jobs_from_dom(html, self.site, today=TODAY)
        self.assertEqual(dom['failed'], 0)
        by_id = {j['job_id']: j for j in dom['jobs']}
        for job in parsed['jobs']:
            self.assertIn(job['job_id'], by_id)  # the sanitizer kept ids consistent across JSON and HTML
            other = by_id[job['job_id']]
            self.assertEqual(other['title'], job['title'])
            self.assertEqual(other['experience']['min_years'], job['experience']['min_years'])
            self.assertEqual(other['salary']['min_inr'], job['salary']['min_inr'])
            self.assertEqual(other['locations'], job['locations'])

    def test_empty_search(self):
        responses, html, _ = fixture('search-empty')
        body = responses[0]['body']
        parsed = parse.search_from_json(body, self.site)
        self.assertEqual((parsed['jobs'], parsed['total'], parsed['empty']), ([], 0, True))
        text = parse.parse_html(html).text()
        self.assertRegex(text, self.site.section('search')['empty_text_regex'])

    def test_job_detail_json_and_dom(self):
        responses, html, _ = fixture('job')
        job_id = fixture_job_id()
        body = next(r['body'] for r in responses if '/jobapi/v4/job/' in r['url'])
        detail = parse.detail_from_json(body, self.site, today=TODAY)
        self.assertEqual(detail['job_id'], job_id)
        self.assertGreater(len(detail['description_text']), 100)
        self.assertFalse(detail['truncated'])
        self.assertEqual(detail['apply_type'], 'naukri')
        self.assertEqual(detail['work_mode'], 'hybrid')
        self.assertTrue(detail['education'][0].startswith('UG: '))
        self.assertIsInstance(detail['applicants'], int)
        self.assertIsInstance(detail['openings'], int)
        self.assertEqual(detail['posted']['date'], '2026-10-13')
        merged = parse.merge_dom_into_detail(detail, html)
        self.assertRegex(merged['posted']['text'], r'ago')
        dom = parse.detail_from_dom(html, self.site, job_id=job_id)
        self.assertEqual(dom['job_id'], job_id)
        self.assertEqual(dom['title'], detail['title'])
        self.assertEqual(dom['company'], detail['company'])
        self.assertEqual(dom['experience']['min_years'], detail['experience']['min_years'])
        self.assertEqual(dom['skills'][:3], detail['skills'][:3])
        self.assertTrue(dom['description_text'])

    def test_company_site_job_and_similar(self):
        responses, _, _ = fixture('job-company-site')
        body = next(r['body'] for r in responses if '/jobapi/v4/job/' in r['url'])
        detail = parse.detail_from_json(body, self.site)
        self.assertEqual(detail['apply_type'], 'company_site')
        self.assertTrue(detail['external_apply_url'].startswith('https://'))
        similar = next(r['body'] for r in responses if '/simjobs/' in r['url'])
        result = parse.similar_from_json(similar, self.site)
        self.assertGreater(len(result['jobs']), 3)
        self.assertEqual(len({j['job_id'] for j in result['jobs']}), len(result['jobs']))
        self.assertTrue(all(j['skills'] for j in result['jobs']))

    def test_find_job_lists_for_unverified_pages(self):
        responses, _, _ = fixture('search')
        wrapped = {'data': {'recommended': {'items': responses[0]['body']['jobDetails']}}, 'meta': [1, 2]}
        lists = parse.find_job_lists(wrapped)
        self.assertEqual(len(lists), 1)
        jobs, failed = parse.jobs_from_json_list(lists[0], self.site)
        self.assertEqual((len(jobs) > 0, failed), (True, 0))

    def test_expired_fixture_url(self):
        _, _, meta = fixture('job-expired')
        self.assertRegex(meta['url'], self.site.section('job')['expired_url_regex'])


class LoggedInFixtureTests(unittest.TestCase):
    """Fixtures sanitized from a logged-in recording (2026-10-19)."""

    def setUp(self):
        self.site = site()

    def tearDown(self):
        site_mod.reset()

    def count(self, markup, selectors):
        root = parse.parse_html(markup)
        return sum(len(root.select(selector)) for selector in selectors)

    def test_login_markers(self):
        login = self.site.section('login')
        _, home, meta = fixture('logged-in-home')
        self.assertIn('/mnjuser/homepage', meta['url'])
        self.assertGreater(self.count(home, login['logged_in_selectors']), 0)
        self.assertEqual(self.count(home, login['logged_out_selectors']), 0)
        self.assertEqual(self.count(home, login['login_form_selectors']), 0)
        _, public, _ = fixture('search')  # recorded logged out
        self.assertGreater(self.count(public, login['logged_out_selectors']), 0)
        self.assertEqual(self.count(public, login['logged_in_selectors']), 0)

    def test_recommended(self):
        responses, html, _ = fixture('recommended')
        pattern = self.site.section('recommended')['response']['url_regex']
        body = next(r['body'] for r in responses if re.search(pattern, r['url']))
        self.assertTrue(body['isLoggedIn'])
        lists = parse.find_job_lists(body)
        jobs, failed = parse.jobs_from_json_list(lists[0], self.site)
        self.assertEqual((len(jobs), failed), (body['noOfJobs'], 0))
        self.assertIn('company_site', {j['apply_type'] for j in jobs})
        dom = parse.jobs_from_dom(html, self.site, 'recommended')
        self.assertEqual({j['job_id'] for j in dom['jobs']}, {j['job_id'] for j in jobs})

    def test_saved_empty(self):
        responses, html, _ = fixture('saved-empty')
        spec = self.site.section('saved')
        body = next(r['body'] for r in responses if re.search(spec['response']['url_regex'], r['url']))
        self.assertEqual((body[spec['response']['total_key']], body['list']), (0, []))
        self.assertRegex(parse.parse_html(html).text(), re.compile(spec['empty_text_regex'], re.I))

    def test_profile_paths_and_no_contact_fields(self):
        responses, _, _ = fixture('profile')
        pattern = self.site.section('profile')['response']['url_regex']
        bodies = [json.loads(json.dumps(r['body'])) for r in responses if re.search(pattern, r['url'])]
        self.assertEqual(len(bodies), 2)
        flat = json.dumps(bodies)
        self.assertNotRegex(flat.replace('REDACTED_', ''), r'@|\d{7,}')  # the fixture itself is fully redacted
        paths = self.site.section('profile')['json_paths']
        for field, options in paths.items():
            self.assertTrue(any(parse._get(b, path) is not None for b in bodies for path in options), field)
        # Fill the observed paths (and some contact keys) with fictional values, as the live page would.
        user = next(b for b in bodies if 'profile' in b)
        dash = next(b for b in bodies if 'dashBoard' in b)
        info = user['profile'][0]['cvInfo']
        info.update({'fileName': 'Demo_Person__' + '0' * 3 + '5550142' + '__file.pdf', 'uploadDate': '2026-10-02 10:00:00',
                     'isAvailable': True})
        user['profile'][0]['lastModTime'] = '2026-10-18 09:30:00.0'
        user['profile'][0]['noticePeriod']['value'] = 'Serving Notice Period'
        dash['dashBoard'].update({'pc': 100, 'mvn': '+1555' + '0100123', 'username': 'demo.person@example.com',
                                  'name': 'Demo Person'})
        markup = '<main><p>Preferred work location</p><p>Northvale, Eastport</p></main>'
        out = parse.profile_summary(bodies, markup, self.site, today=TODAY)
        self.assertTrue(out.pop('found'))
        self.assertEqual(out['resume']['uploaded_on'], '2026-10-02')
        self.assertTrue(out['resume']['present'])
        self.assertEqual(out['resume']['file_name'], 'Demo_Person__[removed]__file.pdf')
        self.assertEqual((out['profile_updated_on'], out['completeness_pct']), ('2026-10-18', 100))
        self.assertEqual(out['notice_period_text'], 'Serving Notice Period')
        self.assertEqual(out['preferred_locations'], ['Northvale', 'Eastport'])
        text = json.dumps(out)
        for secret in ('example.com', '5550142', '0100123', '"Demo Person"'):
            self.assertNotIn(secret, text)

    def test_visible_date_shape(self):
        self.assertEqual(parse._date_text('18Oct , 2026', TODAY), '2026-10-18')
        self.assertEqual(parse._date_text('Oct 12, 2026', TODAY), '2026-10-12')

    def test_logged_in_job_page(self):
        responses, html, _ = fixture('job-logged-in')
        pattern = self.site.section('job')['response']['url_regex']
        matches = [r for r in responses if re.search(pattern, r['url'])]
        self.assertEqual(len(matches), 1)  # the matchscore call is not mistaken for the job
        self.assertTrue(matches[0]['body']['loggedIn'])
        detail = parse.detail_from_json(matches[0]['body'], self.site)
        self.assertEqual(detail['apply_type'], 'naukri')
        root = parse.parse_html(html)
        self.assertEqual(len(root.select(self.site.section('apply')['apply_button'])), 2)
        self.assertEqual(sum(len(root.select(s)) for s in self.site.section('apply')['already_applied_selectors']), 0)


class QuestionnaireTests(unittest.TestCase):
    MARKUP = '''
      <div role="dialog"><p class="question-text">Kindly answer the recruiter's questions</p>
        <fieldset><legend>Are you willing to relocate?</legend>
          <input type="radio" name="reloc" id="r1" value="y"><label for="r1">Yes</label>
          <input type="radio" name="reloc" id="r2" value="n"><label for="r2">No</label></fieldset>
        <label for="np">Notice period in days</label><input type="number" id="np" name="np" min="0" max="180" required>
        <label for="tool">Main tool</label>
        <select id="tool" name="tool"><option value="">Select</option><option value="a">Alpha</option>
          <option value="b">Beta</option></select>
        <textarea id="why" aria-label="Why this role?" maxlength="200"></textarea>
        <input type="checkbox" name="agree" id="agree"><label for="agree">I confirm</label>
        <input type="hidden" name="token" value="x"><button>Save</button></div>'''

    def test_questions_and_answer_checks(self):
        questions = parse.questions_from_dom(self.MARKUP, site())
        site_mod.reset()
        by_label = {q['label']: q for q in questions}
        self.assertEqual(by_label['Are you willing to relocate?']['type'], 'single_select')
        self.assertEqual([o['label'] for o in by_label['Are you willing to relocate?']['options']], ['Yes', 'No'])
        self.assertEqual(by_label['Notice period in days']['type'], 'number')
        self.assertTrue(by_label['Notice period in days']['required'])
        self.assertEqual(by_label['Notice period in days']['max'], 180)
        self.assertEqual([o['label'] for o in by_label['Main tool']['options']], ['Alpha', 'Beta'])
        self.assertEqual(by_label['Why this role?']['max_length'], 200)
        self.assertEqual(by_label['I confirm']['type'], 'boolean')
        self.assertEqual(len(questions), 5)
        self.assertEqual(len({q['qid'] for q in questions}), 5)
        self.assertEqual(questions, parse.questions_from_dom(self.MARKUP, site()))  # stable ids
        site_mod.reset()
        public = parse.public_questions(questions)
        self.assertFalse(any(k.startswith('_') for q in public for k in q))
        qid = {q['label']: q['qid'] for q in questions}
        good = [{'qid': qid['Are you willing to relocate?'], 'value': 'Yes'},
                {'qid': qid['Notice period in days'], 'value': 30},
                {'qid': qid['Main tool'], 'value': 'Beta'}, {'qid': qid['I confirm'], 'value': True}]
        self.assertEqual(parse.check_answers(questions, good), [])
        bad = parse.check_answers(questions, [{'qid': qid['Are you willing to relocate?'], 'value': 'yes'},
                                              {'qid': 'q_unknown', 'value': 'x'},
                                              {'qid': qid['Main tool'], 'value': 'b'}])
        self.assertEqual(len(bad), 4)  # wrong case, unknown id, value instead of label, missing required number
        self.assertTrue(any('option labels' in p for p in bad))


class ProfileParserTests(unittest.TestCase):
    def test_contact_fields_never_come_back(self):
        phone = '+91 9' + '8765 4' + '3210'  # built at runtime so the repository text holds no phone number
        markup = ('<div><h2>Your profile</h2><p>Demo Person</p><p>demo.person@example.com</p><p>' + phone + '</p>'
                  '<p>555-0100</p><div>Resume</div><div>fictional_resume_v2.pdf</div><div>Uploaded on 12 Mar, 2026</div>'
                  '<div>Profile last updated - Today</div><div>Profile completeness 85%</div>'
                  '<div>Notice period: 30 days, call 555-0123 or mail demo.person@example.com</div>'
                  '<div>Preferred locations: Northvale, Remote</div></div>')
        out = parse.profile_summary([{'email': 'demo.person@example.com', 'mobile': '555-0100'}], markup, site(),
                                    today=TODAY)
        site_mod.reset()
        self.assertTrue(out.pop('found'))
        self.assertEqual(out['resume'], {'present': True, 'file_name': 'fictional_resume_v2.pdf',
                                         'uploaded_on': '2026-03-12'})
        self.assertEqual(out['profile_updated_on'], '2026-10-19')
        self.assertEqual(out['completeness_pct'], 85)
        self.assertEqual(out['preferred_locations'], ['Northvale', 'Remote'])
        dumped = json.dumps(out)
        self.assertNotIn('example.com', dumped)
        self.assertNotIn('555', dumped)
        self.assertNotIn('8765', dumped)
        self.assertNotIn('Demo Person', dumped)
        self.assertEqual(set(out), {'resume', 'profile_updated_on', 'completeness_pct', 'notice_period_text',
                                    'preferred_locations'})


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.site = site()

    def tearDown(self):
        site_mod.reset()

    def test_tool_names(self):
        self.assertEqual(len(TOOL_NAMES), 11)
        self.assertEqual(len(set(TOOL_NAMES)), 11)

    def test_profile_version_and_flags(self):
        self.assertRegex(self.site.version, r'^\d{4}-\d{2}-\d{2}$')
        self.assertTrue(self.site.verified('search'))
        self.assertTrue(self.site.verified('job'))
        for feature in ('recommended', 'profile', 'login'):
            self.assertTrue(self.site.verified(feature), feature)
        for feature in ('saved', 'apply'):  # saved items and the apply outcome were not observable
            self.assertFalse(self.site.verified(feature), feature)

    def test_job_refs(self):
        ref = self.site.job_ref('012345678901')
        self.assertEqual(ref.url, 'https://www.naukri.com/job-listings-012345678901')
        ref = self.site.job_ref('https://www.naukri.com/job-listings-data-analyst-alpha-2-to-5-years-012345678901?src=x#y')
        self.assertEqual((ref.job_id, ref.url), ('012345678901',
                                                 'https://www.naukri.com/job-listings-data-analyst-alpha-2-to-5-years-012345678901'))
        self.assertEqual(self.site.job_ref('https://naukri.com/job-listings-x-012345678901').job_id, '012345678901')
        for bad in ('https://www.naukri.com.evil.example/job-listings-x-012345678901', 'http://www.naukri.com/job-listings-x-1234567',
                    'https://' + 'user' + '@' + 'www.naukri.com/job-listings-x-012345678901',  # userinfo, built at runtime
                    'https://www.naukri.com:8443/job-listings-x-012345678901',
                    'https://www.naukri.com/data-analyst-jobs', 'javascript:alert(1)', 'ftp://www.naukri.com/x', '12', '',
                    'https://example.com/job-listings-x-012345678901', None):
            with self.subTest(bad=bad):
                with self.assertRaises(JFError) as caught:
                    self.site.job_ref(bad)
                self.assertEqual(caught.exception.code, 'invalid_argument')

    def test_allowed_urls(self):
        self.assertTrue(self.site.allowed('https://www.naukri.com/x'))
        self.assertTrue(self.site.allowed('https://login.naukri.com/nLogin/Login.php'))
        self.assertFalse(self.site.allowed('http://www.naukri.com/x'))
        self.assertFalse(self.site.allowed('https://accounts.example.com/'))
        self.assertFalse(self.site.allowed('https://www.naukri.com.example.org/'))

    def test_search_urls(self):
        url, warnings = self.site.search_url('Data Analyst', 'Northvale', 3, 7, ['hybrid', 'remote'], 12, 'date', 2)
        self.assertTrue(url.startswith('https://www.naukri.com/data-analyst-jobs-in-northvale-2?'))
        query = url.split('?', 1)[1]
        for part in ('k=Data%20Analyst', 'l=Northvale', 'experience=3', 'jobAge=7', 'wfhType=3', 'wfhType=2',
                     'ctcFilter=10to15', 'ctcFilter=15to25', 'sort=f'):
            self.assertIn(part, query)
        self.assertNotIn('ctcFilter=6to10', query)
        self.assertTrue(any('10-15 LPA range' in w for w in warnings))
        plain, notes = self.site.search_url('c++ developer')
        self.assertEqual(plain, 'https://www.naukri.com/c-plus-plus-developer-jobs?k=c%2B%2B%20developer')
        self.assertEqual(notes, [])

    def test_overrides_only_in_test_mode_and_loopback(self):
        with mock.patch.dict(os.environ, {'JOBFATHER_NAUKRI_BASE': 'http://127.0.0.1:9', 'JOBFATHER_TEST_MODE': ''}):
            site_mod.reset()
            self.assertEqual(site_mod.Site().base_url, 'https://www.naukri.com')
            self.assertEqual(site_mod.browser_channel(), 'chrome')
            self.assertFalse(site_mod.headless())
        with mock.patch.dict(os.environ, {'JOBFATHER_NAUKRI_BASE': 'https://evil.example', 'JOBFATHER_TEST_MODE': '1'}):
            with self.assertRaises(JFError):
                site_mod.Site()
        with mock.patch.dict(os.environ, {'JOBFATHER_NAUKRI_BASE': 'http://127.0.0.1:9', 'JOBFATHER_TEST_MODE': '1',
                                          'JOBFATHER_NAUKRI_CHANNEL': 'chromium', 'JOBFATHER_NAUKRI_HEADLESS': '1'}):
            local = site_mod.Site()
            self.assertEqual(local.base_url, 'http://127.0.0.1:9')
            self.assertTrue(local.allowed('http://127.0.0.1:9/job-listings-x-012345678901'))
            self.assertFalse(local.allowed('https://www.naukri.com/'))
            self.assertEqual(local.url('https://login.naukri.com/nLogin/Login.php'), 'http://127.0.0.1:9/nLogin/Login.php')
            self.assertEqual(local.job_ref('http://127.0.0.1:9/job-listings-x-012345678901').job_id, '012345678901')
            self.assertEqual(site_mod.browser_channel(), 'chromium')
            self.assertTrue(site_mod.headless())


class CheckpointRuleTests(unittest.TestCase):
    def setUp(self):
        self.rules = site().checkpoint_rules
        site_mod.reset()

    def test_rules(self):
        detect = self.rules.detect
        self.assertIsNone(detect(url='https://www.naukri.com/data-analyst-jobs', status=200, title='Data Analyst Jobs',
                                 text='Background verification will be done. Apply now.'))
        self.assertEqual(detect(status=403).kind, 'http_403')
        self.assertEqual(detect(status=403).code, 'blocked')
        self.assertEqual(detect(status=429).code, 'blocked')
        self.assertEqual(detect(title='Access Denied').kind, 'block_page')
        self.assertEqual(detect(text="You don't have permission to access this server").kind, 'block_page')
        self.assertEqual(detect(url='https://www.naukri.com/captcha?x=1').code, 'checkpoint')
        self.assertEqual(detect(text='Please verify you are a human').kind, 'verification')
        self.assertEqual(detect(visible=['el_recaptcha']).kind, 'captcha')
        self.assertEqual(detect(visible=['el_otp_input']).kind, 'otp')
        self.assertIsNone(detect(visible=['el_otp_input'], in_login_flow=True))  # the user's own login
        self.assertIsNone(detect(visible=['el_recaptcha'], in_login_flow=True))
        self.assertEqual(detect(status=403, in_login_flow=True).code, 'blocked')
        self.assertEqual(detect(text='Your account has been temporarily blocked').kind, 'restricted')


if __name__ == '__main__':
    unittest.main()
