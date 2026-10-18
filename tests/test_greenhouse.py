"""Greenhouse answer plans: questions, profile prefill, every safety rule, approval binding, pacing and caps.

Browser tests fill a local fake form (tests/fixtures/greenhouse/form.html), never a real site. They run
only when Playwright is importable and JOBFATHER_E2E=1, for example:
    JOBFATHER_E2E=1 path/to/venv/bin/python -m unittest discover -s tests -p test_greenhouse.py
"""
import contextlib
import copy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.parse

from support import DemoWorkspace, REPO, proposal

import approvals
import layout
from applications import Store
from ats import greenhouse as gh
from job_intake import JobStore
from local import atomic_json, digest, read_json
from memory import apply

FIXTURES = REPO / 'tests' / 'fixtures' / 'greenhouse'
URL = 'https://job-boards.greenhouse.io/examplecorp/jobs/4567890'
EMBED = 'https://job-boards.greenhouse.io/embed/job_app?for=examplecorp&token=4567890'
PDF = b'%PDF-1.4\n% fictional resume for tests\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n'
CONSENT = 'I acknowledge the Example Corp candidate privacy notice'
ANSWERS = {  # what the user would supply in the conversation (fictional)
    'question_30000009': ('Fictional answer: I enjoy turning messy data into clear decisions.', 'user:chat'),
    'location': ('Toronto, Ontario, Canada', 'user:picked-suggestion'),
    'question_30000010[]': (['SQL', 'dbt'], 'user:chat'),
    'question_30000011[]': ([CONSENT], 'user:agreed-in-chat'),
}
PERSONAL = ('Alex', 'Rivera', 'alex.rivera@mail.example', '416-555-0142', '105000', 'Toronto', '2 weeks',
            'Company careers page', 'linkedin.com/in/example', 'Canadian citizen')


def job_data(name='job.json', **changes):
    data = json.loads((FIXTURES / name).read_text(encoding='utf-8'))
    data.update(changes)
    return data


def opener_for(routes):
    """Fake urlopen keyed by path?query; an int value is an HTTP error status."""
    def opener(request, timeout=20):
        parts = urllib.parse.urlsplit(request.full_url)
        key = parts.path + ('?' + parts.query if parts.query else '')
        payload = routes.get(key, 404)
        if isinstance(payload, int):
            raise urllib.error.HTTPError(request.full_url, payload, 'error', {}, io.BytesIO(b'{}'))
        return io.BytesIO(json.dumps(payload).encode('utf-8'))
    return opener


def api_routes(data, board=True, job_id='4567890', name='examplecorp'):
    routes = {'/v1/boards/%s/jobs/%s?questions=true' % (name, job_id): data}
    if board:
        routes['/v1/boards/%s' % name] = {'name': 'Example Corp', 'content': ''}
    return routes


def set_mode(ws, mode):
    """Policy changes only through a logged update (as in test_approvals)."""
    p = proposal(ws, 'mode-' + mode, paths=('config/policy.json',))
    op = p['changes'][0]
    del op['append']
    op['json_set'] = [{'keys': ['mode'], 'value': mode}]
    p['user_request'] = 'Enable %s mode.' % mode
    apply(dict(p, change_type='policy'), ws)


def complete(plan, approve=True):
    """Fill the answers only the user can give, as the host would after asking."""
    for entry in plan['answers']:
        if entry['field'] in ANSWERS:
            entry['value'], entry['source'] = copy.deepcopy(ANSWERS[entry['field']])
        if entry['value'] is not None or entry.get('manual'):
            entry['approved'] = approve
    return plan


def answer(plan, field):
    return next(a for a in plan['answers'] if a['field'] == field)


class WorkspaceCase(unittest.TestCase):
    """A demo workspace with one fictional (sandbox) application and a resume PDF inside the workspace."""

    data_name = 'job.json'

    def setUp(self):
        self.demo = DemoWorkspace()
        self.ws = self.demo.root
        self.data = job_data(self.data_name)
        self.opener = opener_for(api_routes(self.data, job_id=str(self.data['id'])))
        self.url = 'https://job-boards.greenhouse.io/examplecorp/jobs/%s' % self.data['id']
        self.app = Store(self.ws, test=True).create('Example Corp', 'Senior Data Analyst', 'Fictional JD for tests.',
                                                    self.url, 'claude-code')['application_id']
        (self.ws / 'cv').mkdir(exist_ok=True)
        (self.ws / 'cv' / 'Alex_Rivera_CV.pdf').write_bytes(PDF)

    def tearDown(self):
        self.demo.close()

    def template(self, **kwargs):
        kwargs.setdefault('resume', 'cv/Alex_Rivera_CV.pdf')
        return gh.plan_template(self.url, self.app, self.ws, test=True, opener=self.opener, **kwargs)

    def fetched(self):
        return gh.fetch(self.url, opener=self.opener)

    def validate(self, plan, mode='validate'):
        return gh.validate_plan(plan, self.fetched(), self.ws, mode)

    def problems(self, plan, mode='validate'):
        return {(p['field'], p['code']) for p in self.validate(plan, mode)['problems']}


# ------------------------------------------------------------------ questions
class QuestionTests(unittest.TestCase):
    def test_fixture_normalises_every_group(self):
        questions = gh.normalize_questions(job_data())
        by = {q['qkey']: q for q in questions}
        self.assertEqual([q['qkey'] for q in questions][:6],
                         ['first_name', 'last_name', 'preferred_name', 'email', 'country', 'phone'])
        self.assertTrue(all(set(q) >= {'qkey', 'label', 'required', 'type', 'options', 'group', 'kind'} for q in questions))
        self.assertTrue(all(q['type'] in gh.TYPES and q['group'] in gh.GROUPS and q['kind'] in gh.KINDS for q in questions))
        self.assertEqual((by['country']['options_from'], by['country']['match']), ('form', 'dial_code'))
        self.assertEqual(by['resume_text']['alternative_to'], 'resume')
        self.assertFalse(by['resume_text']['required'])
        self.assertEqual((by['location']['group'], by['location']['dom_id']), ('location', 'candidate-location'))
        self.assertEqual(by['latitude']['type'], 'input_hidden')
        self.assertEqual(by['question_30000010[]']['option_ids'], ['9001', '9002', '9003', '9004'])
        kinds = {k: by[k]['kind'] for k in ('question_30000001', 'question_30000002', 'question_30000004',
                                            'question_30000005', 'question_30000006', 'question_30000007',
                                            'question_30000008', 'question_30000011[]', 'question_30000012')}
        self.assertEqual(kinds, {'question_30000001': 'links', 'question_30000002': 'custom',
                                 'question_30000004': 'work_auth', 'question_30000005': 'sponsorship',
                                 'question_30000006': 'relocation', 'question_30000007': 'salary',
                                 'question_30000008': 'notice', 'question_30000011[]': 'consent',
                                 'question_30000012': 'consent'})
        self.assertIn('candidate privacy notice before applying', by['question_30000011[]']['description'])
        compliance = [q['qkey'] for q in questions if q['group'] == 'compliance' and q['kind'] == 'eeo']
        self.assertEqual(compliance, ['veteran_status', 'hispanic_ethnicity', 'race', 'gender'])
        self.assertTrue(by['race']['conditional'])
        self.assertEqual(by['demographic_5000001']['dom_id'], '5000001')
        self.assertEqual(by['demographic_5000001']['decline_options'], ["I don't wish to answer"])
        self.assertEqual(by['demographic_5000002']['free_form_options'], ['Prefer to self-describe'])
        self.assertEqual((by['school--0']['required'], by['degree--0']['options_from']), (False, 'form'))
        self.assertEqual(by['gdpr_retention_consent_given']['kind'], 'consent')
        self.assertNotIn('gdpr_processing_consent_given', by)

    def test_hash_is_stable_and_tracks_every_change(self):
        base = gh.sha_json(gh.normalize_questions(job_data()))
        self.assertEqual(base, gh.sha_json(gh.normalize_questions(job_data())))
        variants = [job_data(), job_data(), job_data(), job_data()]
        variants[0]['questions'][7]['label'] = 'LinkedIn URL'
        variants[1]['questions'][8]['fields'][0]['values'].append({'label': 'Podcast', 'value': 9205})
        variants[2]['questions'][7]['required'] = True
        variants[3]['questions'][17]['description'] = '<p>A different privacy notice.</p>'
        for variant in variants:
            self.assertNotEqual(base, gh.sha_json(gh.normalize_questions(variant)))

    def test_kind_classification(self):
        cases = [
            ('Will you now or in the future require visa sponsorship?', 'sponsorship'),
            ('Are you able to work without sponsorship?', 'sponsorship'),
            ('Do you have a valid work permit for Canada?', 'work_auth'),
            ('Are you legally authorized to work in the country where the job is located?', 'work_auth'),
            ('Are you willing to relocate?', 'relocation'),
            ('Gender', 'eeo'), ('Are you a protected veteran?', 'eeo'), ('Pronouns', 'demographic'),
            ('I consent to Example Corp storing my application', 'consent'),
            ('Candidate AI Usage Attestation:', 'consent'),
            ('Are you subject to any non-compete agreement?', 'custom'),
            ('LinkedIn Profile', 'links'), ('Other Website', 'links'),
            ('Preferred First Name', 'identity'), ("What's the name you'd prefer us to use?", 'identity'),
            ('Current Location', 'contact'), ('What is your current country of residence?', 'contact'),
            ('What are your salary expectations?', 'salary'), ('When can you start?', 'notice'),
            ('How did you hear about this role?', 'custom'), ('Why do you want to work here?', 'custom'),
        ]
        for label, kind in cases:
            with self.subTest(label=label):
                self.assertEqual(gh.classify_kind('question_1', label, 'main'), kind)
        self.assertEqual(gh.classify_kind('question_1', 'Before you apply', 'main', '', ['I acknowledge the notice']),
                         'consent')
        self.assertEqual(gh.classify_kind('anything', 'Anything', 'compliance'), 'eeo')
        self.assertEqual(gh.classify_kind('anything', 'Anything', 'demographic'), 'demographic')

    def test_other_shapes(self):
        by = {q['qkey']: q for q in gh.normalize_questions(job_data('job-intl.json'))}
        self.assertTrue(by['gdpr_processing_consent_given']['required'])
        self.assertFalse(by['gdpr_retention_consent_given']['required'])
        self.assertTrue(by['school--0']['required'] and by['degree--0']['required'])
        self.assertEqual((by['question_40000006']['kind'], by['question_40000007']['kind']), ('eeo', 'demographic'))
        self.assertEqual(by['question_40000008']['kind'], 'consent')
        self.assertFalse(by['country']['required'])  # follows the optional phone question
        self.assertFalse(any(q['group'] == 'compliance' and q['kind'] == 'eeo' for q in by.values()))


class FetchTests(unittest.TestCase):
    def test_fetch_returns_the_embeddable_form_and_hash(self):
        result = gh.fetch(URL, opener=opener_for(api_routes(job_data())))
        self.assertEqual((result['board'], result['job_id'], result['url']), ('examplecorp', '4567890', EMBED))
        self.assertEqual((result['title'], result['company'], result['location']),
                         ('Senior Data Analyst', 'Example Corp', 'Toronto, Ontario, Canada'))
        self.assertEqual(result['questions_sha256'], gh.sha_json(result['questions']))

    def test_closed_jobs_and_unknown_boards(self):
        with self.assertRaises(gh.Stop) as ctx:
            gh.fetch(URL, opener=opener_for({'/v1/boards/examplecorp': {'name': 'Example Corp'}}))
        self.assertEqual((ctx.exception.status, ctx.exception.code), ('closed', gh.EXIT_CLOSED))
        with self.assertRaises(ValueError):
            gh.fetch(URL, opener=opener_for({}))
        past = job_data(application_deadline='2020-01-31')
        with self.assertRaises(gh.Stop) as ctx:
            gh.fetch(URL, opener=opener_for(api_routes(past)))
        self.assertEqual(ctx.exception.status, 'closed')
        with self.assertRaises(ValueError):
            gh.fetch(URL, opener=opener_for(api_routes(500)))

    def test_links_that_cannot_be_filled_by_script(self):
        cases = [('https://jobs.lever.co/examplecorp', 'not_greenhouse'),
                 ('https://job-boards.eu.greenhouse.io/examplecorp/jobs/4567890', 'unsupported_board'),
                 ('https://careers.example.com/jobs?gh_jid=4567890', 'needs_input'),
                 ('https://boards.greenhouse.io/examplecorp', 'needs_input')]
        for link, status in cases:
            with self.subTest(link=link):
                with self.assertRaises(gh.Stop) as ctx:
                    gh.fetch(link, opener=opener_for(api_routes(job_data())))
                self.assertEqual(ctx.exception.status, status)
        result = gh.fetch('https://careers.example.com/jobs?gh_jid=4567890', board='examplecorp',
                          opener=opener_for(api_routes(job_data())))
        self.assertEqual(result['url'], EMBED)


# ------------------------------------------------------------------ plan template
class TemplateTests(WorkspaceCase):
    def test_prefill_comes_only_from_the_workspace(self):
        plan, path = self.template()
        self.assertEqual(plan['schema'], gh.PLAN_SCHEMA)
        self.assertEqual(plan['plan_sha256'], gh.plan_hash(plan))
        self.assertEqual(plan['job'], {'board': 'examplecorp', 'job_id': '4567890', 'url': EMBED,
                                       'title': 'Senior Data Analyst', 'company': 'Example Corp',
                                       'questions_sha256': self.fetched()['questions_sha256']})
        self.assertEqual(path, gh.application_dir(self.ws, self.app, True) / 'ats' / 'plan-greenhouse-examplecorp-4567890.json')
        self.assertTrue(str(path).startswith(str(self.ws / layout.SANDBOX)))
        expected = {
            'first_name': ('Alex', 'profile.identity.first_name'), 'last_name': ('Rivera', 'profile.identity.last_name'),
            'preferred_name': ('Alex', 'profile.identity.preferred_name'),
            'email': ('alex.rivera@mail.example', 'profile.identity.email'),
            'country': ('Canada', 'profile.identity.country'), 'phone': ('+1 416-555-0142', 'profile.identity.phone'),
            'resume': ('files.resume', 'files.resume'), 'location': ('Toronto, ON', 'profile.identity.location'),
            'question_30000001': ('https://www.linkedin.com/in/example', 'profile.identity.links.linkedin'),
            'question_30000002': ('Company careers page', 'profile.form_answers.how-did-you-hear-about-us'),
            'question_30000003': ('6', 'profile.form_answers.years-of-sql-experience'),
            'question_30000004': ('Yes', 'profile.hr.work_authorization'),
            'question_30000005': ('No', 'profile.hr.sponsorship_required'),
            'question_30000006': ('No', 'profile.hr.relocation_willingness'),
            'question_30000007': ('105000 CAD per year', 'profile.hr.expected_compensation'),
            'question_30000008': ('2 weeks', 'profile.hr.notice_period'),
        }
        got = {a['field']: (a['value'], a['source']) for a in plan['answers'] if a['value'] is not None}
        self.assertEqual(got, expected)
        for field in ('question_30000009', 'question_30000010[]', 'question_30000011[]', 'question_30000012',
                      'cover_letter', 'school--0', 'degree--0', 'gdpr_retention_consent_given'):
            self.assertIsNone(answer(plan, field)['value'], field)
        self.assertFalse(any(a['approved'] for a in plan['answers']))
        consent = answer(plan, 'question_30000011[]')
        self.assertIn('candidate privacy notice', consent['consent_text'])
        self.assertEqual(consent['consent_text_sha256'], gh.sha_text(consent['consent_text']))
        self.assertNotIn('consent_text', answer(plan, 'gdpr_retention_consent_given'))
        self.assertEqual(plan['files']['resume'], {'path': 'cv/Alex_Rivera_CV.pdf', 'sha256': digest(self.ws / 'cv' / 'Alex_Rivera_CV.pdf')})
        self.assertIsNone(plan['files']['cover_letter'])
        si = plan['self_identification']
        self.assertEqual((si['mode'], si['answers']), ('decline', []))
        self.assertEqual([q['qkey'] for q in si['questions']], ['veteran_status', 'hispanic_ethnicity', 'race', 'gender',
                                                                 'demographic_5000001', 'demographic_5000002'])
        self.assertFalse({'gender', 'race', 'veteran_status'} & {a['field'] for a in plan['answers']})
        self.assertNotIn('resume_text', {a['field'] for a in plan['answers']})
        self.assertNotIn('latitude', {a['field'] for a in plan['answers']})
        self.assertEqual(set(plan['unanswered_optional']), {'cover_letter', 'question_30000010[]', 'question_30000012',
                                                            'school--0', 'degree--0', 'gdpr_retention_consent_given'})
        self.assertEqual(read_json(path), plan)
        with self.assertRaises(ValueError):
            self.template()
        self.template(force=True)

    def test_resume_defaults_to_the_latest_validated_cv(self):
        store = Store(self.ws, test=True)
        record_path = store.directory(self.app) / 'record.json'
        record = read_json(record_path)
        cv = self.ws / layout.sandboxed(layout.CV_OUTPUTS, True) / 'Alex_Rivera_CV_v2.pdf'
        cv.parent.mkdir(parents=True, exist_ok=True)
        cv.write_bytes(PDF + b'% v2\n')
        record['outputs'] += [
            {'output_id': 'a' * 32, 'kind': 'cv-pdf', 'version': 1, 'path': 'cv/Alex_Rivera_CV.pdf', 'path_root': 'workspace',
             'sha256': digest(self.ws / 'cv' / 'Alex_Rivera_CV.pdf'), 'validation_status': 'passed'},
            {'output_id': 'b' * 32, 'kind': 'cv-pdf', 'version': 2, 'path': cv.relative_to(self.ws).as_posix(),
             'path_root': 'workspace', 'sha256': digest(cv), 'validation_status': 'passed'}]
        atomic_json(record_path, record)
        plan, _ = self.template(resume=None)
        self.assertEqual(plan['files']['resume']['output_id'], 'b' * 32)
        plan, _ = self.template(resume='cv/Alex_Rivera_CV.pdf', force=True)
        self.assertEqual(plan['files']['resume']['output_id'], 'a' * 32)

    def test_uploads_and_plans_stay_inside_the_workspace(self):
        outside = self.demo.base / 'outside.pdf'
        outside.write_bytes(PDF)
        with self.assertRaises(ValueError):
            self.template(resume=str(outside))
        (self.ws / 'cv' / 'notes.pdf').write_bytes(b'plain text pretending to be a PDF')
        with self.assertRaises(ValueError):
            self.template(resume='cv/notes.pdf')
        with self.assertRaises(ValueError):
            self.template(resume='../outside.pdf')
        with self.assertRaises(ValueError):
            self.template(out=str(self.demo.base / 'plan.json'))
        with self.assertRaises((ValueError, OSError)):
            gh.plan_template(self.url, 'APP-000000000000', self.ws, test=True, opener=self.opener)


class DeriveTests(unittest.TestCase):
    """The deterministic mapping from confirmed profile facts to one question's answer."""

    def ctx(self, job_location='Toronto, Ontario, Canada', **hr):
        base = {'work_authorization': {'value': 'Canadian citizen', 'jurisdiction': 'CA', 'confirmed_at': '2026-09-01'},
                'sponsorship_required': {'value': False, 'jurisdiction': 'CA', 'confirmed_at': '2026-09-01'},
                'relocation_willingness': {'value': False, 'confirmed_at': '2026-09-01'},
                'expected_compensation': {'value': 105000, 'currency': 'CAD', 'period': 'year', 'confirmed_at': '2026-09-01'},
                'current_compensation': {'value': None, 'currency': None, 'period': None, 'confirmed_at': None}}
        base.update(hr)
        return {'ws': Path('.'), 'identity': {'full_name': 'Alex Rivera', 'contacts': {}}, 'hr': base,
                'answers': {'note': {'question': 'Anything else?', 'answer': 'No', 'scope': 'APP-0123456789ab',
                                     'confirmed_at': '2026-09-01'}},
                'job_location': job_location}

    def q(self, label, kind, options=('Yes', 'No'), qtype='multi_value_single_select'):
        return {'qkey': 'question_1', 'label': label, 'kind': kind, 'type': qtype, 'options': list(options),
                'group': 'main', 'required': True}

    def test_legal_answers_are_mapped_only_when_they_fit_exactly(self):
        auth, sponsor = 'profile.hr.work_authorization', 'profile.hr.sponsorship_required'
        ctx = self.ctx()
        self.assertEqual(gh.derive(self.q('Are you legally authorized to work in Canada?', 'work_auth'), auth, ctx), 'Yes')
        self.assertIsNone(gh.derive(self.q('Are you legally authorized to work in the United States?', 'work_auth'), auth, ctx))
        self.assertIsNone(gh.derive(self.q('Are you authorized to work in the US and Canada?', 'work_auth'), auth, ctx))
        where = self.q('Are you authorized to work in the country where this job is located?', 'work_auth')
        self.assertEqual(gh.derive(where, auth, ctx), 'Yes')
        self.assertIsNone(gh.derive(where, auth, self.ctx(job_location='Remote')))
        self.assertIsNone(gh.derive(where, auth, self.ctx(job_location='London, United Kingdom')))
        long_form = self.q('Are you authorized to work in Canada?', 'work_auth',
                           ('Yes, I am currently authorized to work in Canada.', 'No, I am not.'))
        self.assertIsNone(gh.derive(long_form, auth, ctx))
        self.assertIsNone(gh.derive(self.q('Do you require a visa to work in Canada?', 'work_auth'), auth, ctx))
        negative = self.ctx(work_authorization={'value': 'Needs a work permit', 'jurisdiction': 'CA', 'confirmed_at': '2026-09-01'})
        self.assertIsNone(gh.derive(self.q('Are you legally authorized to work in Canada?', 'work_auth'), auth, negative))
        text = self.q('What is your work authorization status in Canada?', 'work_auth', (), 'input_text')
        self.assertEqual(gh.derive(text, auth, ctx), 'Canadian citizen')
        self.assertEqual(gh.derive(self.q('Will you now or in the future require sponsorship to work in Canada?',
                                          'sponsorship'), sponsor, ctx), 'No')
        self.assertIsNone(gh.derive(self.q('Are you able to work in Canada without sponsorship?', 'sponsorship'), sponsor, ctx))
        self.assertIsNone(gh.derive(self.q('Will you require sponsorship?', 'sponsorship'), sponsor, self.ctx(job_location='Remote')))
        unconfirmed = self.ctx(sponsorship_required={'value': False, 'jurisdiction': 'CA', 'confirmed_at': None})
        self.assertIsNone(gh.derive(self.q('Will you require sponsorship to work in Canada?', 'sponsorship'), sponsor, unconfirmed))

    def test_relocation_pay_names_and_saved_answers(self):
        ctx = self.ctx()
        relocate = 'profile.hr.relocation_willingness'
        self.assertEqual(gh.derive(self.q('Are you open to relocating?', 'relocation'), relocate, ctx), 'No')
        self.assertIsNone(gh.derive(self.q('Are you based in, or willing to relocate to, Toronto?', 'relocation'), relocate, ctx))
        pay = self.q('What are your salary expectations?', 'salary', (), 'input_text')
        self.assertEqual(gh.derive(pay, 'profile.hr.expected_compensation', ctx), '105000 CAD per year')
        self.assertIsNone(gh.derive(self.q('Expected salary', 'salary', ('Below 100k', '100k+')),
                                    'profile.hr.expected_compensation', ctx))
        self.assertIsNone(gh.derive(pay, 'profile.hr.current_compensation', ctx))
        self.assertIsNone(gh.derive(pay, 'profile.hr.self_identification', ctx))
        name = self.q('First Name', 'identity', (), 'input_text')
        self.assertEqual(gh.derive(name, 'profile.identity.first_name', ctx), 'Alex')
        ctx['identity'] = {'full_name': 'Alex de la Rivera'}
        self.assertIsNone(gh.derive(name, 'profile.identity.first_name', ctx))
        ctx['identity'] = {'full_name': 'Alex de la Rivera', 'given_name': 'Alex'}
        self.assertEqual(gh.derive(name, 'profile.identity.first_name', ctx), 'Alex')
        self.assertIsNone(gh.derive(self.q('Anything else?', 'custom', (), 'input_text'), 'profile.form_answers.note', ctx))


# ------------------------------------------------------------------ validation (safety rules)
class ValidationTests(WorkspaceCase):
    def test_a_complete_plan_passes_and_resolves_exact_actions(self):
        plan = complete(self.template()[0])
        result = self.validate(plan, 'submit')
        self.assertEqual(result['problems'], [])
        actions = {a['field']: a for a in result['actions']}
        self.assertEqual(actions['question_30000004']['value'], 'Yes')
        self.assertEqual(actions['resume']['value'], str(self.ws / 'cv' / 'Alex_Rivera_CV.pdf'))
        self.assertEqual(actions['veteran_status']['value'], "I don't wish to answer")
        self.assertEqual((actions['gender']['value'], actions['race']['value']),
                         ('Decline To Self Identify', 'Decline To Self Identify'))
        self.assertTrue(actions['race']['conditional'])
        self.assertEqual((actions['hispanic_ethnicity']['value'], actions['hispanic_ethnicity']['decline']), (None, True))
        self.assertEqual(actions['demographic_5000001']['value'], "I don't wish to answer")
        self.assertEqual(actions['demographic_5000002']['value'], ["I don't wish to answer"])
        self.assertEqual(actions['demographic_5000002']['control'], 'multi_select')
        boxes = {b['label']: b['checked'] for b in actions['question_30000010[]']['checkboxes']}
        self.assertEqual(boxes, {'SQL': True, 'Python': False, 'dbt': True, 'Tableau': False})
        self.assertEqual(actions['question_30000010[]']['checkboxes'][0]['id'], 'question_30000010[]_9001')
        self.assertEqual(actions['question_30000011[]']['checkboxes'], [{'id': 'question_30000011[]_9101', 'label': CONSENT,
                                                                         'checked': True}])
        self.assertTrue(actions['gdpr_retention_consent_given']['untick'])  # never left ticked by the page
        self.assertNotIn('question_30000012', actions)  # optional consent nobody approved: left alone
        self.assertNotIn('school--0', actions)
        self.assertEqual(list(actions)[:3], ['first_name', 'last_name', 'preferred_name'])

    def test_unknown_required_questions_stop_without_defaults(self):
        plan = complete(self.template()[0])
        for field in ('question_30000009', 'question_30000004', 'question_30000002'):
            answer(plan, field).update(value=None, source=None)
        result = self.validate(plan)
        self.assertEqual({p['field'] for p in result['problems'] if p['code'] == 'required_unanswered'},
                         {'question_30000009', 'question_30000004', 'question_30000002'})
        self.assertFalse({'question_30000009', 'question_30000004', 'question_30000002'} & {a['field'] for a in result['actions']})
        template = self.template(force=True)[0]  # the untouched template: nothing invented for the unknowns
        self.assertIn(('question_30000009', 'required_unanswered'), self.problems(template))
        self.assertIn(('question_30000011[]', 'required_unanswered'), self.problems(template))

    def test_options_must_match_exactly(self):
        plan = complete(self.template()[0])
        for value in ('yes', '  YES ', 'Yes'):
            answer(plan, 'question_30000004').update(value=value, source='user:chat')
            self.assertEqual(self.problems(plan), set())
            self.assertEqual({a['field']: a['value'] for a in self.validate(plan)['actions']}['question_30000004'], 'Yes')
        for value, code in (('Y', 'option_not_found'), ('Yes, I am', 'option_not_found'), (['Yes'], 'bad_value_type')):
            answer(plan, 'question_30000004').update(value=value, source='user:chat')
            self.assertIn(('question_30000004', code), self.problems(plan))
        answer(plan, 'question_30000004').update(value='Yes', source='user:chat')
        for value, code in ((['SQL', 'sql'], 'duplicate_option'), ('SQL', 'bad_value_type'), (['SQL', 'Excel'], 'option_not_found')):
            answer(plan, 'question_30000010[]')['value'] = value
            self.assertIn(('question_30000010[]', code), self.problems(plan))

    def test_consent_needs_approval_and_the_exact_text(self):
        plan = complete(self.template()[0])
        consent = answer(plan, 'question_30000011[]')
        consent['approved'] = False
        self.assertIn(('question_30000011[]', 'consent_not_approved'), self.problems(plan, 'preview'))
        consent.update(approved=True, consent_text_sha256=gh.sha_text('different wording'))
        self.assertIn(('question_30000011[]', 'consent_text_changed'), self.problems(plan, 'preview'))
        consent.update(consent_text_sha256=gh.sha_text(consent['consent_text']), source='profile.form_answers.anything')
        self.assertIn(('question_30000011[]', 'source_not_allowed'), self.problems(plan))
        consent['source'] = 'user:agreed-in-chat'
        self.assertEqual(self.problems(plan), set())
        optional = answer(plan, 'question_30000012')
        optional.update(value='Yes', source='user:chat', approved=False)
        self.assertIn(('question_30000012', 'consent_not_approved'), self.problems(plan, 'preview'))
        optional.update(value=None, source=None)
        gdpr = answer(plan, 'gdpr_retention_consent_given')
        gdpr.update(value=['I agree'], source='user:chat', approved=True)
        self.assertIn(('gdpr_retention_consent_given', 'consent_manual_only'), self.problems(plan))
        gdpr.update(value=None, source=None, manual=True)
        manual = {a['field']: a for a in self.validate(plan, 'submit')['actions']}['gdpr_retention_consent_given']
        self.assertTrue(manual['manual'])
        changed = job_data()
        changed['questions'][17]['description'] = '<p>New privacy wording.</p>'
        with self.assertRaises(gh.Stop) as ctx:
            gh.validate_plan(plan, gh.fetch(self.url, opener=opener_for(api_routes(changed))), self.ws)
        self.assertEqual(ctx.exception.status, 'questions_changed')

    def test_self_identification_declines_unless_the_user_answers(self):
        plan = complete(self.template()[0])
        si = plan['self_identification']
        si['answers'] = [{'field': 'gender', 'value': 'Female', 'source': 'user:chat', 'approved': True}]
        self.assertIn(('self_identification', 'answers_in_decline_mode'), self.problems(plan))
        si['mode'] = 'answers'
        self.assertEqual(self.problems(plan, 'submit'), set())
        actions = {a['field']: a for a in self.validate(plan, 'submit')['actions']}
        self.assertEqual(actions['gender']['value'], 'Female')
        self.assertTrue(actions['veteran_status']['decline'])  # everything not answered still declines
        si['answers'][0]['source'] = 'profile.hr.self_identification'
        self.assertIn(('gender', 'self_id_source'), self.problems(plan))
        si['answers'][0].update(source='user:chat', approved=False)
        self.assertIn(('gender', 'not_approved'), self.problems(plan, 'submit'))
        si['answers'] = [{'field': 'demographic_5000002', 'value': ['Prefer to self-describe'], 'source': 'user:chat',
                          'approved': True}, {'field': 'shoe_size', 'value': '9', 'source': 'user:chat'}]
        found = self.problems(plan)
        self.assertIn(('demographic_5000002', 'free_form_not_supported'), found)
        self.assertIn(('shoe_size', 'unknown_field'), found)
        si.update(mode='decline', answers=[])
        plan['answers'].append({'field': 'gender', 'value': 'Male', 'source': 'user:chat', 'approved': True})
        self.assertIn(('gender', 'self_identification_in_answers'), self.problems(plan))

    def test_required_self_identification_without_a_decline_option_stops(self):
        data = job_data()
        gender = data['compliance'][1]['questions'][1]
        gender['required'] = True
        gender['fields'][0]['values'] = [{'label': 'Female', 'value': 2}, {'label': 'Male', 'value': 1}]
        self.opener = opener_for(api_routes(data))
        plan = complete(self.template()[0])
        self.assertIn(('gender', 'no_decline_option'), self.problems(plan))

    def test_legal_and_pay_answers_come_only_from_the_profile_or_the_user(self):
        plan = complete(self.template()[0])
        auth = answer(plan, 'question_30000004')
        for source in ('draft:fit-analysis', 'jd:posting says citizens only', 'job_description', 'model'):
            auth['source'] = source
            self.assertIn(('question_30000004', 'source_not_allowed'), self.problems(plan), source)
        auth.update(value='No', source='profile.hr.work_authorization')
        self.assertIn(('question_30000004', 'profile_mismatch'), self.problems(plan))
        auth['source'] = 'user:said no in chat'
        self.assertEqual(self.problems(plan), set())
        pay = answer(plan, 'question_30000007')
        pay.update(value='120000 CAD per year')
        self.assertIn(('question_30000007', 'profile_mismatch'), self.problems(plan))
        pay['source'] = 'draft:negotiation'
        self.assertIn(('question_30000007', 'source_not_allowed'), self.problems(plan))
        why = answer(plan, 'question_30000009')
        why['source'] = 'draft:APP-output-1'
        pay.update(value='105000 CAD per year', source='profile.hr.expected_compensation')
        self.assertEqual(self.problems(plan), set())  # a reviewed draft may answer an open question
        email = answer(plan, 'email')
        email['value'] = 'someone@example.com'
        self.assertIn(('email', 'profile_mismatch'), self.problems(plan))
        email['value'] = 'alex.rivera@mail.example'
        answer(plan, 'phone')['value'] = '(416) 555-0142 '
        self.assertIn(('phone', 'profile_mismatch'), self.problems(plan))  # different digits: no country code
        answer(plan, 'phone')['value'] = '+1 (416) 555 0142'
        self.assertEqual(self.problems(plan), set())

    def test_files_must_be_workspace_pdfs_with_matching_hashes(self):
        plan = complete(self.template()[0])
        resume = plan['files']['resume']
        outside = self.demo.base / 'outside.pdf'
        outside.write_bytes(PDF)
        for path, code in ((str(outside), 'file_outside_workspace'), ('../outside.pdf', 'file_outside_workspace'),
                           ('cv/missing.pdf', 'file_missing'), ('cv/notes.txt', 'file_not_pdf')):
            resume['path'] = path
            self.assertIn(('resume', code), self.problems(plan), path)
        (self.ws / 'cv' / 'fake.pdf').write_bytes(b'not a pdf at all')
        resume['path'] = 'cv/fake.pdf'
        self.assertIn(('resume', 'file_not_pdf'), self.problems(plan))
        (self.ws / 'cv' / 'link.pdf').symlink_to(outside)
        resume['path'] = 'cv/link.pdf'
        self.assertIn(('resume', 'file_outside_workspace'), self.problems(plan))
        resume['path'] = 'cv/Alex_Rivera_CV.pdf'
        (self.ws / 'cv' / 'Alex_Rivera_CV.pdf').write_bytes(PDF + b'% edited after planning\n')
        found = self.problems(plan)
        self.assertIn(('resume', 'file_hash_mismatch'), found)
        self.assertNotIn(('resume', 'file_not_planned'), found)
        plan['files']['resume'] = None
        self.assertIn(('resume', 'file_not_planned'), self.problems(plan))
        answer(plan, 'resume').update(value=str(outside), source='user:chat')
        self.assertIn(('resume', 'source_not_allowed'), self.problems(plan))

    def test_submit_needs_every_answer_approved(self):
        plan = complete(self.template()[0], approve=False)
        answer(plan, 'question_30000011[]')['approved'] = True
        self.assertEqual(self.problems(plan, 'preview'), set())
        missing = {f for f, code in self.problems(plan, 'submit') if code == 'not_approved'}
        self.assertEqual(missing, {a['field'] for a in plan['answers']
                                   if a['value'] is not None and a['field'] != 'question_30000011[]'})

    def test_plan_must_match_the_job_and_its_fields(self):
        plan = complete(self.template()[0])
        other = copy.deepcopy(plan)
        other['job']['board'] = 'othercorp'
        with self.assertRaises(gh.Stop) as ctx:
            self.validate(other)
        self.assertEqual(ctx.exception.status, 'job_mismatch')
        broken = copy.deepcopy(plan)
        broken['schema'] = 'something-else'
        with self.assertRaises(gh.Stop) as ctx:
            self.validate(broken)
        self.assertEqual(ctx.exception.status, 'invalid_plan')
        plan['answers'].append({'field': 'question_99999999', 'value': 'x', 'source': 'user:chat'})
        plan['answers'].append(copy.deepcopy(answer(plan, 'email')))
        plan['answers'] = [a for a in plan['answers'] if a['field'] != 'question_30000001']
        found = self.problems(plan)
        self.assertTrue({('question_99999999', 'unknown_field'), ('email', 'duplicate_field'),
                         ('question_30000001', 'missing_field')} <= found)

    def test_manual_answers_are_the_users_own_clicks(self):
        plan = complete(self.template()[0])
        why = answer(plan, 'question_30000009')
        why.update(value=None, source=None, manual=True, approved=False)
        self.assertIn(('question_30000009', 'not_approved'), self.problems(plan, 'submit'))
        why['approved'] = True
        self.assertTrue({a['field']: a for a in self.validate(plan, 'submit')['actions']}['question_30000009']['manual'])
        why['value'] = 'typed anyway'
        self.assertIn(('question_30000009', 'manual_with_value'), self.problems(plan))

    def test_problem_reports_never_carry_values(self):
        plan = complete(self.template()[0])
        answer(plan, 'email')['value'] = 'someone@example.com'
        answer(plan, 'question_30000004').update(value='Maybe', source='user:chat')
        result = self.validate(plan)
        self.assertTrue(result['problems'])
        self.assertTrue(all(set(p) == {'field', 'code'} for p in result['problems']))
        self.assertNotIn('someone@example.com', json.dumps(result['problems']))


class InternationalShapeTests(WorkspaceCase):
    data_name = 'job-intl.json'

    def test_unclear_legal_questions_stay_with_the_user(self):
        plan, _ = self.template()
        values = {a['field']: a['value'] for a in plan['answers']}
        for field in ('question_40000001', 'question_40000002', 'question_40000003', 'question_40000004',
                      'question_40000005', 'question_40000008', 'question_40000009[]', 'question_40000010',
                      'school--0', 'degree--0', 'gdpr_processing_consent_given'):
            self.assertIsNone(values[field], field)
        self.assertEqual(values['country'], 'Canada')
        self.assertEqual([q['qkey'] for q in plan['self_identification']['questions']],
                         ['question_40000006', 'question_40000007'])
        attestation = answer(plan, 'question_40000008')
        self.assertIn('generative AI', attestation['consent_text'])
        found = self.problems(plan)
        for field in ('question_40000001', 'question_40000005', 'school--0', 'gdpr_processing_consent_given'):
            self.assertIn((field, 'required_unanswered'), found)
        self.assertNotIn(('question_40000006', 'no_decline_option'), found)  # "Prefer not to say" is the decline
        answer(plan, 'question_40000010').update(value='files.resume', source='files.resume')
        self.assertIn(('question_40000010', 'unsupported_file_question'), self.problems(plan))


# ------------------------------------------------------------------ approvals, pacing and caps
class ApprovedCase(WorkspaceCase):
    def approved_plan(self):
        set_mode(self.ws, 'submit-with-approval')
        plan, path = self.template()
        complete(plan)
        gh.stamp(plan, path)
        record = approvals.create(self.ws, 'ats_submit', {'company': 'Example Corp', 'role': 'Senior Data Analyst'},
                                  gh.approval_payload(plan), 'Yes, submit this Example Corp application.', 'claude-code')
        return plan, path, record['approval_id']


class ApprovalTests(ApprovedCase):
    def test_approval_binds_plan_resume_and_form(self):
        plan, path, approval_id = self.approved_plan()
        self.assertEqual(gh.approval_payload(plan), {'plan_sha256': gh.plan_hash(plan),
                                                     'resume_sha256': plan['files']['resume']['sha256'], 'url': EMBED})
        self.assertEqual(gh.check_approval(self.ws, plan, approval_id)['kind'], 'ats_submit')
        edited = copy.deepcopy(plan)
        answer(edited, 'question_30000003')['value'] = '7'
        for bad_plan, bad_id in ((edited, approval_id), (plan, 'apv_bogus'), (plan, 'not-an-id')):
            with self.assertRaises(gh.Stop) as ctx:
                gh.check_approval(self.ws, bad_plan, bad_id)
            self.assertEqual((ctx.exception.status, ctx.exception.code), ('approval_invalid', gh.EXIT_APPROVAL))
        moved = copy.deepcopy(plan)
        moved['files']['resume']['sha256'] = '0' * 64
        with self.assertRaises(gh.Stop):
            gh.check_approval(self.ws, moved, approval_id)
        mail = approvals.create(self.ws, 'email_send', {'to': 'hiring@example.com'}, gh.approval_payload(plan),
                                'Send it.', 'claude-code')
        with self.assertRaises(gh.Stop):
            gh.check_approval(self.ws, plan, mail['approval_id'])
        approvals.mark(self.ws, approval_id, 'consumed', {'outcome': 'submitted'})
        with self.assertRaises(gh.Stop):
            gh.check_approval(self.ws, plan, approval_id)

    def test_policy_downgrade_blocks_existing_approvals(self):
        plan, path, approval_id = self.approved_plan()
        set_mode(self.ws, 'drafts-only')
        with self.assertRaises(gh.Stop) as ctx:
            gh.check_approval(self.ws, plan, approval_id)
        self.assertEqual(ctx.exception.code, gh.EXIT_APPROVAL)

    def test_submit_preconditions_before_any_browser(self):
        plan, path, approval_id = self.approved_plan()
        with self.assertRaises(gh.Stop) as ctx:
            gh.submit(path, approval_id, self.ws, opener=self.opener)
        self.assertEqual(ctx.exception.status, 'sandbox')  # fictional application, no local test form
        with self.assertRaises(ValueError):  # the user-click stand-in exists only for the local test form
            gh.submit(path, approval_id, self.ws, opener=self.opener, simulate_user=print)
        env = {'JOBFATHER_TEST_MODE': '1', 'JOBFATHER_GREENHOUSE_BASE': 'http://127.0.0.1:9'}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(gh.Stop) as ctx:
                gh.submit(path, approval_id, self.ws, opener=self.opener)
            self.assertEqual(ctx.exception.status, 'needs_preview')
            run = gh.application_dir(self.ws, plan) / 'ats' / 'preview-20261018T000000Z-abcdef'
            atomic_json(run / 'fill-report.json', {'plan_sha256': plan['plan_sha256'], 'status': 'ready'})
            with self.assertRaises(gh.Stop) as ctx:
                gh.submit(path, 'apv_20261018T000000Z_deadbeef', self.ws, opener=self.opener)
            self.assertEqual(ctx.exception.code, gh.EXIT_APPROVAL)

            @contextlib.contextmanager
            def closed_page(ws, profile=None):
                raise gh.Stop('closed', 'The job page says the job is closed', gh.EXIT_CLOSED)
                yield
            with mock.patch.object(gh, 'browser_page', closed_page):
                with self.assertRaises(gh.Stop) as ctx:
                    gh.submit(path, approval_id, self.ws, opener=self.opener)
            self.assertEqual(ctx.exception.code, gh.EXIT_CLOSED)
            self.assertEqual(approvals.load(self.ws, approval_id)['status'], 'pending')  # nothing happened
            self.assertEqual([e['event'] for e in gh.read_ledger(self.ws)], ['submit_started', 'submit_aborted'])
            with self.assertRaises(gh.Stop) as ctx:  # one submission a minute
                gh.submit(path, approval_id, self.ws, opener=self.opener)
            self.assertEqual(ctx.exception.status, 'paced')
            answer(plan, 'question_30000003')['value'] = '7'
            atomic_json(path, plan)
            with self.assertRaises(gh.Stop) as ctx:
                gh.submit(path, approval_id, self.ws, opener=self.opener)
            self.assertEqual(ctx.exception.status, 'plan_changed')


class SubmitFlowTests(ApprovedCase):
    """The submit bookkeeping with the browser layer stubbed out (no Playwright needed)."""

    def run_submit(self, path, approval_id, results, waiting):
        class Page:
            def screenshot(self, path, full_page=True):
                Path(path).write_bytes(b'not really a png')

        @contextlib.contextmanager
        def page(ws, profile=None):
            yield Page()
        fill = lambda page, actions, mode: [dict(results.get(a['field'], {'status': 'filled', 'readback': 'match'}),
                                                 field=a['field']) for a in actions]
        with mock.patch.object(gh, 'browser_page', page), mock.patch.object(gh, 'open_form', lambda p, u: None), \
                mock.patch.object(gh, 'fill_form', fill), mock.patch.object(gh, 'unplanned_required', lambda p, a: []), \
                mock.patch.object(gh, 'await_user_submit', waiting), mock.patch.object(gh, 'MIN_GAP_SECONDS', 0), \
                contextlib.redirect_stderr(io.StringIO()):
            return gh.submit(path, approval_id, self.ws, opener=self.opener)

    def test_mismatch_and_interruption(self):
        plan, path, approval_id = self.approved_plan()
        env = {'JOBFATHER_TEST_MODE': '1', 'JOBFATHER_GREENHOUSE_BASE': 'http://127.0.0.1:9'}
        with mock.patch.dict(os.environ, env):
            run = gh.application_dir(self.ws, plan) / 'ats' / 'preview-20261018T000000Z-abcdef'
            atomic_json(run / 'fill-report.json', {'plan_sha256': plan['plan_sha256'], 'status': 'ready'})
            never = mock.Mock(side_effect=AssertionError('must not wait for the user'))
            with self.assertRaises(gh.Stop) as ctx:
                self.run_submit(path, approval_id, {'email': {'status': 'filled', 'readback': 'mismatch'}}, never)
            self.assertEqual((ctx.exception.status, ctx.exception.fields), ('readback_mismatch', ['email']))
            self.assertEqual(approvals.load(self.ws, approval_id)['status'], 'pending')

            def interrupted(*args, **kwargs):
                raise KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                self.run_submit(path, approval_id, {}, interrupted)
        self.assertEqual(approvals.load(self.ws, approval_id)['status'], 'cancelled')  # the user may have clicked
        self.assertEqual([e['event'] for e in gh.read_ledger(self.ws)],
                         ['submit_started', 'submit_aborted', 'submit_started', 'unconfirmed'])


    def test_a_killed_run_uses_up_its_approval_and_a_receipt_records_the_submission(self):
        plan, path, approval_id = self.approved_plan()
        env = {'JOBFATHER_TEST_MODE': '1', 'JOBFATHER_GREENHOUSE_BASE': 'http://127.0.0.1:9'}
        confirmation = 'https://job-boards.greenhouse.io/embed/job_app/confirmation?for=examplecorp&token=4567890'
        with mock.patch.dict(os.environ, env):
            run = gh.application_dir(self.ws, plan) / 'ats' / 'preview-20261018T000000Z-abcdef'
            atomic_json(run / 'fill-report.json', {'plan_sha256': plan['plan_sha256'], 'status': 'ready'})
            gh._ledger_event(self.ws, plan, 'submit_started', 'submit-killed', approval_id)  # no end recorded
            never = mock.Mock(side_effect=AssertionError('must not wait for the user'))
            with self.assertRaises(gh.Stop) as ctx:
                self.run_submit(path, approval_id, {}, never)
            self.assertEqual((ctx.exception.status, ctx.exception.code), ('approval_invalid', gh.EXIT_APPROVAL))
            gh._ledger_event(self.ws, plan, 'submit_aborted', 'submit-killed', approval_id, outcome='error')
            confirmed = lambda *args, **kwargs: ('confirmed', {'url': confirmation, 'text': 'Thank you for applying.'})
            result = self.run_submit(path, approval_id, {}, confirmed)
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(approvals.load(self.ws, approval_id)['status'], 'consumed')
        receipt = read_json(self.ws / result['receipt'])
        jobs = JobStore(self.ws, test=True)
        jobs.ingest({'jobs': [{'company': 'Example Corp', 'role': 'Senior Data Analyst', 'location': 'Toronto, ON',
                               'full_jd': 'Fictional JD for tests.', 'application_url': self.url,
                               'sources': [{'type': 'url', 'label': 'Greenhouse', 'url': self.url,
                                            'access': 'readable'}]}]}, 'claude-code')
        job_id = jobs.show()['jobs'][0]['job_id']
        recorded = jobs.record_submission(job_id, self.app, receipt, 'claude-code')
        self.assertEqual(recorded['stage'], 'Applied')
        record = Store(self.ws, test=True).load(self.app)
        self.assertEqual(record['status'], 'submitted')
        self.assertEqual(record['interactions'][-1]['evidence']['url'], confirmation)

    def test_a_required_field_scan_that_fails_blocks(self):
        class Page:
            def evaluate(self, script, arg):
                raise RuntimeError('the page navigated away')
        self.assertEqual([u['dom_id'] for u in gh.unplanned_required(Page(), [])], ['required-field-scan'])


class PacingTests(WorkspaceCase):
    def entry(self, minutes_ago, event='submitted', ats='greenhouse'):
        at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
        gh.append_ledger(self.ws, {'at': at, 'event': event, 'ats': ats, 'run_id': 'r', 'application_id': self.app})

    def settings(self, **greenhouse):
        path = self.ws / layout.PLATFORMS
        data = read_json(path)
        data['ats']['greenhouse'].update(greenhouse)
        atomic_json(path, data)
        return gh.ats_settings(self.ws)

    def test_a_minute_between_submissions(self):
        settings = self.settings()
        self.assertEqual(settings, {'enabled': True, 'submit_mode': 'user_click', 'daily_cap': 15})
        self.entry(0.5, 'submit_started')
        with self.assertRaises(gh.Stop) as ctx:
            gh.check_pacing(self.ws, settings)
        self.assertEqual(ctx.exception.status, 'paced')
        self.assertTrue(0 < ctx.exception.extra['retry_after_s'] <= 60)
        later = datetime.now(timezone.utc) + timedelta(seconds=61)
        self.assertEqual(gh.check_pacing(self.ws, settings, later)['today'], 0)

    def test_daily_cap_from_platform_settings(self):
        settings = self.settings(daily_cap=2)
        clock = datetime.now(timezone.utc)
        local_midnight = clock.astimezone(gh.tz(self.ws)[0]).replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = (clock - (local_midnight - timedelta(minutes=5)).astimezone(timezone.utc)).total_seconds() / 60
        self.entry(yesterday)
        self.entry(5, ats='lever')
        self.entry(4, 'submit_aborted')
        self.assertEqual(gh.check_pacing(self.ws, settings)['today'], 0)
        self.entry(3)
        self.entry(2, 'unconfirmed')  # may have been submitted: it counts
        with self.assertRaises(gh.Stop) as ctx:
            gh.check_pacing(self.ws, settings)
        self.assertEqual(ctx.exception.status, 'daily_cap')
        with (self.ws / gh.LEDGER).open('ab') as handle:
            handle.write(b'{"torn line')
        with self.assertRaises(gh.Stop):
            gh.check_pacing(self.ws, settings)

    def test_settings_are_checked(self):
        for cap in (0, 101, '15', True):
            with self.assertRaises(ValueError):
                self.settings(daily_cap=cap)
        self.settings(daily_cap=15, enabled=False)
        plan, path = self.template()
        with self.assertRaises(gh.Stop) as ctx:
            gh.preview(path, self.ws, opener=self.opener)
        self.assertIn(ctx.exception.status, ('disabled', 'sandbox'))
        with mock.patch.dict(os.environ, {'JOBFATHER_TEST_MODE': '1', 'JOBFATHER_GREENHOUSE_BASE': 'http://127.0.0.1:9'}):
            with self.assertRaises(gh.Stop) as ctx:
                gh.preview(path, self.ws, opener=self.opener)
            self.assertEqual(ctx.exception.status, 'disabled')
            self.settings(enabled=True, submit_mode='auto_click')
            with self.assertRaises(gh.Stop) as ctx:
                gh.submit(path, 'apv_x', self.ws, opener=self.opener)
            self.assertEqual(ctx.exception.status, 'unsupported_submit_mode')
        with mock.patch.dict(os.environ, {'JOBFATHER_TEST_MODE': '1', 'JOBFATHER_GREENHOUSE_BASE': 'https://example.com'}):
            with self.assertRaises(ValueError):
                gh.local_base()
        with mock.patch.dict(os.environ, {'JOBFATHER_GREENHOUSE_BASE': 'http://127.0.0.1:9'}):
            os.environ.pop('JOBFATHER_TEST_MODE', None)
            self.assertIsNone(gh.local_base())  # ignored outside test mode


# ------------------------------------------------------------------ command line
class CommandLineTests(WorkspaceCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with mock.patch('urllib.request.urlopen', self.opener), contextlib.redirect_stdout(out):
            code = gh.main(['--workspace', str(self.ws)] + list(argv))
        return code, out.getvalue()

    def test_stdout_carries_field_names_and_statuses_only(self):
        code, text = self.run_cli('plan-template', '--url', self.url, '--application-id', self.app, '--test',
                                  '--resume', 'cv/Alex_Rivera_CV.pdf')
        self.assertEqual(code, 0)
        summary = json.loads(text)
        self.assertIn({'field': 'email', 'source': 'profile.identity.email'}, summary['prefilled'])
        self.assertIn({'field': 'question_30000009', 'kind': 'custom'}, summary['needs_input'])
        for value in PERSONAL:
            self.assertNotIn(value, text)
        code, text = self.run_cli('validate', '--plan', summary['plan'])
        self.assertEqual((code, json.loads(text)['status']), (gh.EXIT_STOP, 'needs_input'))
        for value in PERSONAL:
            self.assertNotIn(value, text)
        plan = complete(read_json(Path(summary['plan'])))
        atomic_json(Path(summary['plan']), plan)
        code, text = self.run_cli('validate', '--plan', summary['plan'])
        self.assertEqual((code, json.loads(text)['status']), (0, 'ok'))
        self.assertEqual(read_json(Path(summary['plan']))['plan_sha256'], gh.plan_hash(plan))  # stamped
        code, text = self.run_cli('fetch', '--url', self.url)
        self.assertEqual((code, json.loads(text)['questions_sha256']), (0, self.fetched()['questions_sha256']))
        self.opener = opener_for({'/v1/boards/examplecorp': {'name': 'Example Corp'}})
        code, text = self.run_cli('fetch', '--url', self.url)
        self.assertEqual((code, json.loads(text)['status']), (gh.EXIT_CLOSED, 'closed'))
        code, text = self.run_cli('validate', '--plan', str(self.demo.base / 'elsewhere.json'))
        self.assertEqual(code, gh.EXIT_ERROR)


# ------------------------------------------------------------------ browser tests (local fake form only)
E2E = os.environ.get('JOBFATHER_E2E') == '1' and importlib.util.find_spec('playwright') is not None
CLOSED_PAGE = b'<!doctype html><title>Example Corp</title><h1>Sorry</h1><p>The job you are looking for is no longer open.</p>'


class FakeGreenhouse:
    """Stdlib server for the question API and the fake hosted form; records what the page reports."""

    def __init__(self):
        self.events = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, status, body, kind='application/json'):
                self.send_response(status)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                owner.events.append(query.get('kind', ['?'])[0])
                self.send(204, b'')

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(parts.query)
                if parts.path == '/v1/boards/examplecorp':
                    return self.send(200, (FIXTURES / 'board.json').read_bytes())
                if parts.path.startswith('/v1/boards/examplecorp/jobs/'):
                    job_id = parts.path.rsplit('/', 1)[1]
                    if job_id not in ('4567890', '4567892'):
                        return self.send(404, b'{"status":404,"error":"Job not found"}')
                    return self.send(200, json.dumps(job_data(id=int(job_id))).encode('utf-8'))
                if parts.path == '/embed/job_app' and query.get('for') == ['examplecorp']:
                    if query.get('token') == ['4567890']:
                        return self.send(200, (FIXTURES / 'form.html').read_bytes(), 'text/html; charset=utf-8')
                    return self.send(200, CLOSED_PAGE, 'text/html; charset=utf-8')
                self.send(404, b'not found', 'text/plain')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:%d' % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@unittest.skipUnless(E2E, 'browser tests need Playwright and JOBFATHER_E2E=1')
class BrowserTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.site = FakeGreenhouse()
        self.env = mock.patch.dict(os.environ, {'JOBFATHER_TEST_MODE': '1', 'JOBFATHER_GREENHOUSE_BASE': self.site.base,
                                                'JOBFATHER_ATS_HEADLESS': os.environ.get('JOBFATHER_ATS_HEADLESS', '1')})
        self.env.start()
        self.opener = None  # talk to the local server like the real API

    def tearDown(self):
        self.env.stop()
        self.site.close()
        super().tearDown()

    def ready_plan(self):
        plan, path = self.template()
        atomic_json(path, complete(plan))
        return path

    def test_preview_fills_reads_back_and_never_submits(self):
        path = self.ready_plan()
        summary = gh.preview(path, self.ws)
        statuses = {f['field']: (f['status'], f.get('readback')) for f in summary['fields']}
        self.assertEqual(summary['status'], 'ready', statuses)
        for field in ('first_name', 'email', 'country', 'phone', 'location', 'resume', 'question_30000002',
                      'question_30000004', 'question_30000009', 'question_30000010[]', 'question_30000011[]',
                      'gender', 'hispanic_ethnicity', 'veteran_status', 'demographic_5000001', 'demographic_5000002',
                      'gdpr_retention_consent_given'):
            self.assertEqual(statuses[field], ('filled', 'match'), field)
        self.assertEqual(statuses['race'], ('absent_optional', None))  # shown only after "No"
        self.assertEqual(summary['unplanned_required'], [])
        report = read_json(self.ws / summary['report'])
        self.assertTrue((self.ws / summary['screenshot']).is_file())
        payload = read_json(self.ws / summary['approval_payload_file'])
        self.assertEqual(payload, gh.approval_payload(read_json(path)))
        self.assertEqual(report['plan_sha256'], read_json(path)['plan_sha256'])
        self.assertNotIn('submit_attempt', self.site.events)
        self.assertNotIn('keyboard_select', self.site.events)
        for value in PERSONAL:
            self.assertNotIn(value, json.dumps(summary))

    def test_preview_stops_when_no_option_matches_exactly(self):
        plan, path = self.template()
        complete(plan)
        answer(plan, 'location').update(value='Toronto, ON', source='profile.identity.location')
        atomic_json(path, plan)
        summary = gh.preview(path, self.ws)
        self.assertEqual(summary['status'], 'needs_input')
        self.assertIn('location', summary['blocking'])
        fields = {f['field']: f for f in read_json(self.ws / summary['report'])['fields']}
        self.assertEqual(fields['location']['status'], 'option_not_found')
        self.assertEqual(fields['location']['available_options'], ['Toronto, Ontario, Canada'])
        self.assertNotIn('keyboard_select', self.site.events)

    def test_closed_job_page(self):
        self.url = 'https://job-boards.greenhouse.io/examplecorp/jobs/4567892'
        plan, path = self.template()
        atomic_json(path, complete(plan))
        with self.assertRaises(gh.Stop) as ctx:
            gh.preview(path, self.ws)
        self.assertEqual((ctx.exception.status, ctx.exception.code), ('closed', gh.EXIT_CLOSED))

    def approve(self, path):
        set_mode(self.ws, 'submit-with-approval')
        summary = gh.preview(path, self.ws)
        self.assertEqual(summary['status'], 'ready')
        payload = read_json(self.ws / summary['approval_payload_file'])
        return approvals.create(self.ws, 'ats_submit', {'company': 'Example Corp', 'role': 'Senior Data Analyst'},
                                payload, 'Yes, submit my Example Corp application.', 'claude-code')['approval_id']

    def test_submit_waits_for_the_users_own_click(self):
        path = self.ready_plan()
        approval_id = self.approve(path)
        clicks = []

        def user(page):  # the test plays the user: review, then click Submit
            self.assertNotIn('submit_attempt', self.site.events)
            clicks.append(1)
            page.click('button[type="submit"]')
        os.environ['JOBFATHER_ATS_WAIT_SECONDS'] = '30'
        result = gh.submit(path, approval_id, self.ws, simulate_user=user)
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual((clicks, self.site.events.count('submitted')), ([1], 1))
        receipt = read_json(self.ws / result['receipt'])
        self.assertEqual((receipt['submitted_by'], receipt['approval_id'], receipt['ats']), ('user_click', approval_id, 'greenhouse'))
        self.assertTrue(receipt['observed_confirmation']['url'].endswith('/examplecorp/jobs/4567890/confirmation'))
        self.assertIn('Thank you for applying', receipt['observed_confirmation']['text'])
        self.assertTrue(all((self.ws / shot).is_file() for shot in receipt['screenshots']))
        submission = read_json(self.ws / result['submission'])
        self.assertEqual(submission, receipt['submission'])
        self.assertEqual(set(submission), {'schema', 'channel', 'submitted_at', 'authorized_by_user', 'user_request', 'evidence'})
        self.assertEqual((submission['schema'], submission['authorized_by_user']), ('the-jobfather/submission@1', True))
        self.assertEqual(submission['user_request'], 'Yes, submit my Example Corp application.')
        self.assertEqual(submission['evidence']['kind'], 'confirmation-page')
        self.assertEqual(submission['evidence']['capture_sha256'], digest(self.ws / receipt['screenshots'][1]))
        self.assertIsNotNone(datetime.fromisoformat(submission['submitted_at']).tzinfo)
        self.assertEqual(approvals.load(self.ws, approval_id)['status'], 'consumed')
        self.assertEqual([e['event'] for e in gh.read_ledger(self.ws)], ['submit_started', 'submitted'])
        with self.assertRaises(gh.Stop) as ctx:
            gh.submit(path, approval_id, self.ws)
        self.assertEqual(ctx.exception.code, gh.EXIT_APPROVAL)  # single use

    def test_submit_times_out_when_the_user_does_not_click(self):
        path = self.ready_plan()
        approval_id = self.approve(path)
        os.environ['JOBFATHER_ATS_WAIT_SECONDS'] = '3'
        with self.assertRaises(gh.Stop) as ctx:
            gh.submit(path, approval_id, self.ws)
        self.assertEqual((ctx.exception.status, ctx.exception.code), ('timeout', gh.EXIT_TIMEOUT))
        self.assertNotIn('submit_attempt', self.site.events)
        self.assertEqual(approvals.load(self.ws, approval_id)['status'], 'expired')
        self.assertEqual([e['event'] for e in gh.read_ledger(self.ws)], ['submit_started', 'unconfirmed'])


if __name__ == '__main__':
    unittest.main()
