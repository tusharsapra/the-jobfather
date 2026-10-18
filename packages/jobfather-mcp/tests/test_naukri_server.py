"""The Naukri MCP server through an in-memory MCP client, with an offline fake browser session."""
import asyncio
import json
import unittest
from unittest import mock

from naukri_support import close_workspace, data_of, error_of, fixture, fixture_job_id, install, workspace

from mcp import Client

from jobfather_mcp import approvals
from jobfather_mcp.naukri import TOOL_NAMES, server, store
from jobfather_mcp.naukri.session import Session

STATEMENT = 'Yes, apply to this job for me.'
MINIMAL_ARGS = {
    'naukri_status': {}, 'naukri_login': {'wait_s': 0}, 'naukri_search_jobs': {'keywords': 'data analyst'},
    'naukri_recommended_jobs': {}, 'naukri_get_job': {'job': '012345678901'},
    'naukri_similar_jobs': {'job': '012345678901'}, 'naukri_saved_jobs': {}, 'naukri_my_profile': {},
    'naukri_prepare_apply': {'job': '012345678901', 'user_statement': STATEMENT},
    'naukri_apply': {'job': '012345678901', 'approval_id': 'apv_x'}, 'naukri_close': {},
}


class Base(unittest.IsolatedAsyncioTestCase):
    enabled = True
    policy = 'submit-with-approval'
    limits = None

    def setUp(self):
        self.ws = workspace(enabled=self.enabled, policy=self.policy, limits=self.limits)

    def tearDown(self):
        close_workspace(self.ws)

    async def call(self, name, args=None):
        async with Client(server.app) as client:
            return await client.call_tool(name, args or {})


class RegistryTests(Base):
    async def test_exactly_the_eleven_tools_with_output_schemas(self):
        async with Client(server.app) as client:
            tools = (await client.list_tools()).tools
        self.assertEqual(sorted(t.name for t in tools), sorted(TOOL_NAMES))
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertIsNotNone(tool.output_schema)
                self.assertIn('ok', tool.output_schema['properties'])
                self.assertNotIn('ctx', tool.input_schema.get('properties', {}))
        apply_tool = next(t for t in tools if t.name == 'naukri_apply')
        self.assertTrue(apply_tool.annotations.destructive_hint)
        text = server.INSTRUCTIONS
        for needle in ('untrusted', 'naukri_prepare_apply', 'Submit application', 'installed', 'authenticated',
                       'search-verified'):
            self.assertIn(needle, text)

    async def test_no_background_tasks_at_startup(self):
        before = set(asyncio.all_tasks())
        async with Client(server.app) as client:
            await client.list_tools()
            result = await client.call_tool('naukri_status', {})
            self.assertFalse(result.is_error)
            ours = [t for t in asyncio.all_tasks() - before
                    if 'jobfather_mcp' in getattr(t.get_coro(), 'cr_code', type('x', (), {'co_filename': ''})).co_filename]
        self.assertEqual(ours, [])
        self.assertIsNone(server.STATE.session)  # nothing was started

    async def test_status_cached_never_launches_the_browser(self):
        with mock.patch.object(Session, 'ensure', side_effect=AssertionError('browser started')):
            data = data_of(await self.call('naukri_status', {'check': 'cached'}))
        self.assertTrue(data['enabled'])
        self.assertEqual(data['tos_acknowledged_at'], '2026-10-19T09:00:00+00:00')
        self.assertEqual(data['authenticated']['state'], 'unknown')
        self.assertEqual(data['search_verified']['state'], 'unknown')
        self.assertFalse(data['browser']['running'])
        self.assertFalse(data['checkpoint']['active'])
        self.assertEqual({u['action'] for u in data['usage']},
                         {'page', 'search', 'recommended', 'saved', 'similar', 'job', 'apply'})
        self.assertFalse(data['site_profile']['verified']['apply'])
        self.assertIn('chrome', data['installed'])

    async def test_invalid_arguments_use_the_error_shape(self):
        for name, args in (('naukri_search_jobs', {'keywords': 'a'}), ('naukri_search_jobs', {'keywords': 'ab', 'limit': 99}),
                           ('naukri_apply', {'job': '1', 'confirm_timeout_s': 5}), ('naukri_status', {'check': 'x'})):
            with self.subTest(name=name, args=args):
                result = await self.call(name, args)
                self.assertTrue(result.is_error)
                self.assertEqual(error_of(result)['code'], 'invalid_argument')
                self.assertIs(result.structured_content['ok'], False)

    async def test_other_sites_are_rejected(self):
        install(self.ws)
        for name in ('naukri_get_job', 'naukri_similar_jobs', 'naukri_prepare_apply', 'naukri_apply'):
            args = dict(MINIMAL_ARGS[name], job='https://jobs.example.com/job-listings-x-012345678901')
            with self.subTest(name=name):
                result = await self.call(name, args)
                self.assertEqual(error_of(result)['code'], 'invalid_argument')
        self.assertEqual(server.STATE.session.loads, [])


class DisabledTests(Base):
    enabled = False

    async def test_every_tool_but_status_needs_the_opt_in(self):
        for name in TOOL_NAMES:
            with self.subTest(tool=name):
                result = await self.call(name, MINIMAL_ARGS[name])
                if name == 'naukri_status':
                    self.assertFalse(data_of(result)['enabled'])
                else:
                    self.assertEqual(error_of(result)['code'], 'not_enabled')

    async def test_live_status_needs_the_opt_in(self):
        result = await self.call('naukri_status', {'check': 'live'})
        self.assertEqual(error_of(result)['code'], 'not_enabled')


class PolicyTests(Base):
    policy = 'drafts-only'

    async def test_apply_tools_need_submit_policy(self):
        session = install(self.ws)
        for name in ('naukri_prepare_apply', 'naukri_apply'):
            result = await self.call(name, MINIMAL_ARGS[name])
            self.assertEqual(error_of(result)['code'], 'not_enabled')
            self.assertIn('policy', error_of(result)['user_action'])
        self.assertEqual(session.loads, [])
        self.assertFalse(data_of(await self.call('naukri_search_jobs', {'keywords': 'data analyst'}))['partial'])


class ReadToolTests(Base):
    async def test_search_marks_search_verified(self):
        session = install(self.ws)
        data = data_of(await self.call('naukri_search_jobs', {'keywords': 'Data Analyst', 'location': 'Northvale',
                                                              'limit': 3, 'work_modes': ['hybrid']}))
        self.assertEqual(len(data['jobs']), 3)
        self.assertEqual(data['provenance']['method'], 'page_json')
        self.assertTrue(data['provenance']['untrusted_content'])
        self.assertFalse(data['total_is_exact'])
        self.assertGreater(data['total_approx'], 3)
        self.assertEqual(data['usage']['action'], 'search')
        self.assertEqual(data['usage']['used_today'], 1)
        self.assertIn('wfhType=3', data['query']['url'])
        self.assertTrue(session.loads[0].startswith('https://www.naukri.com/data-analyst-jobs-in-northvale?'))
        status = data_of(await self.call('naukri_status'))
        self.assertEqual(status['search_verified']['state'], 'yes')
        self.assertEqual(status['search_verified']['jobs_seen'], 3)

    async def test_get_job_and_similar(self):
        install(self.ws)
        job_id = fixture_job_id()
        data = data_of(await self.call('naukri_get_job', {'job': job_id}))
        self.assertEqual(data['job']['job_id'], job_id)
        self.assertTrue(data['job']['description_text'])
        similar = data_of(await self.call('naukri_similar_jobs', {'job': job_id, 'limit': 4}))
        self.assertEqual(similar['source_job_id'], job_id)
        self.assertEqual(len(similar['jobs']), 4)
        self.assertNotIn(job_id, [j['job_id'] for j in similar['jobs']])
        gone = await self.call('naukri_get_job', {'job': '011111111111'})
        self.assertEqual(error_of(gone)['code'], 'not_found')

    async def test_logged_in_lists_need_a_login(self):
        install(self.ws, logged_in=False)
        for name in ('naukri_recommended_jobs', 'naukri_saved_jobs', 'naukri_my_profile'):
            with self.subTest(name=name):
                self.assertEqual(error_of(await self.call(name))['code'], 'auth_required')

    async def test_recommended_from_any_job_list_json(self):
        session = install(self.ws)
        responses, _, _ = fixture('search')
        items = responses[0]['body']['jobDetails']

        def page(loaded):
            loaded.responses['recommended'] = [('https://www.naukri.com/jobapi/v2/recommended', {'data': {'list': items}})]
        session.pages[r'/mnjuser/recommendedjobs'] = page
        data = data_of(await self.call('naukri_recommended_jobs', {'limit': 2}))
        self.assertEqual(len(data['jobs']), 2)

        def saved(loaded):
            loaded.html = '<main><p>You have not saved any jobs yet.</p></main>'
        session.pages[r'/mnjuser/savedjobs'] = saved
        data = data_of(await self.call('naukri_saved_jobs'))
        self.assertEqual((data['jobs'], data['total_approx']), ([], 0))

    async def test_my_profile_never_returns_contact_fields(self):
        session = install(self.ws)
        phone = '+91 9' + '8765 4' + '3210'

        def profile(loaded):
            loaded.html = ('<main><h1>Demo Person</h1><p>demo.person@example.com</p><p>%s</p><section><h2>Resume</h2>'
                           '<p>demo_resume.pdf</p><p>Uploaded on 02 Oct, 2026</p></section><p>Profile last updated - '
                           'Yesterday</p><p>Profile completeness 92%%</p><p>Notice period: 15 Days or less</p>'
                           '<p>Preferred locations: Northvale</p><p>Address: 1 Example Road</p></main>' % phone)
            loaded.responses['profile'] = [('https://www.naukri.com/cloudgateway-mynaukri/x',
                                            {'profile': {'email': 'demo.person@example.com', 'mobile': phone}})]
        session.pages[r'/mnjuser/profile'] = profile
        data = data_of(await self.call('naukri_my_profile'))
        self.assertEqual(set(data), {'ok', 'resume', 'profile_updated_on', 'completeness_pct', 'notice_period_text',
                                     'preferred_locations', 'provenance', 'usage'})
        self.assertEqual(data['resume']['file_name'], 'demo_resume.pdf')
        self.assertEqual(data['completeness_pct'], 92)
        self.assertEqual(data['notice_period_text'], '15 Days or less')
        text = json.dumps(data)
        for secret in ('example.com', '8765', 'Demo Person', 'Example Road'):
            self.assertNotIn(secret, text)

    async def test_checkpoint_stops_and_pauses_other_tools(self):
        session = install(self.ws)
        session.checkpoint_next = 'captcha'
        result = await self.call('naukri_search_jobs', {'keywords': 'data analyst'})
        self.assertEqual(error_of(result)['code'], 'checkpoint')
        self.assertFalse(error_of(result)['retryable'])
        again = await self.call('naukri_get_job', {'job': fixture_job_id()})
        self.assertEqual(error_of(again)['code'], 'checkpoint')
        self.assertEqual(len(session.loads), 1)  # nothing loaded during the cooldown
        status = data_of(await self.call('naukri_status'))
        self.assertTrue(status['checkpoint']['active'])
        self.assertEqual(status['checkpoint']['kind'], 'captcha')
        store.clear_checkpoint(self.ws.root)
        session.checkpoint_next = 'http_403'
        blocked = await self.call('naukri_get_job', {'job': fixture_job_id()})
        self.assertEqual(error_of(blocked)['code'], 'blocked')


class ApplyTests(Base):
    async def asyncSetUp(self):
        self.session = install(self.ws)
        self.job = fixture_job_id()

    async def prepare(self, **extra):
        return data_of(await self.call('naukri_prepare_apply', dict({'job': self.job, 'user_statement': STATEMENT},
                                                                   **extra)))

    async def test_apply_needs_an_approval(self):
        result = await self.call('naukri_apply', {'job': self.job})
        self.assertEqual(error_of(result)['code'], 'approval_required')
        self.assertEqual(self.session.loads, [])

    async def test_prepare_binds_and_apply_consumes_once(self):
        prepared = await self.prepare()
        self.assertEqual(prepared['status'], 'ready')
        approval = prepared['approval']
        self.assertEqual(approval['binds'], {'job_id': self.job, 'answers_sha256': approvals.payload_hash([]),
                                             'flow_id': None})
        self.assertIn('Submit application', approval['summary_for_user'])
        self.assertEqual(prepared['resume_used']['source'], 'naukri_profile')
        record = approvals.load(self.ws.root, approval['approval_id'])
        self.assertEqual((record['kind'], record['status'], record['user_statement']),
                         ('naukri_apply', 'pending', STATEMENT))
        self.assertEqual(self.session.page_actions.clicks, 0)  # preparing never clicks Apply

        self.session.decisions = ['submit']
        self.session.page_actions.outcomes = [{'kind': 'applied', 'text': 'You have successfully applied', 'url': 'x'}]
        done = data_of(await self.call('naukri_apply', {'job': self.job, 'approval_id': approval['approval_id']}))
        self.assertEqual(done['status'], 'applied')
        self.assertTrue(done['evidence']['screenshot'].startswith('.state/naukri/evidence/'))
        self.assertEqual(self.session.page_actions.clicks, 1)
        self.assertEqual(self.session.confirm_data['job_id'], self.job)
        self.assertEqual(approvals.load(self.ws.root, approval['approval_id'])['status'], 'consumed')
        self.assertEqual(done['usage']['used_today'], 1)

        again = await self.call('naukri_apply', {'job': self.job, 'approval_id': approval['approval_id']})
        self.assertEqual(error_of(again)['code'], 'approval_consumed')
        self.assertEqual(self.session.page_actions.clicks, 1)

    async def test_mismatch_expiry_cancel_and_timeout(self):
        approval = (await self.prepare())['approval']['approval_id']
        other = await self.call('naukri_apply', {'job': '012345678901', 'approval_id': approval})
        self.assertEqual(error_of(other)['code'], 'approval_mismatch')
        later = approvals._now() + approvals.timedelta(minutes=20)
        with mock.patch('jobfather_mcp.approvals._now', return_value=later):
            expired = await self.call('naukri_apply', {'job': self.job, 'approval_id': approval})
        self.assertEqual(error_of(expired)['code'], 'approval_expired')

        self.session.decisions = ['timeout']
        timed = data_of(await self.call('naukri_apply', {'job': self.job, 'approval_id': approval,
                                                         'confirm_timeout_s': 30}))
        self.assertEqual(timed['status'], 'confirmation_timeout')
        self.assertEqual(approvals.load(self.ws.root, approval)['status'], 'pending')
        self.session.decisions = ['cancel']
        cancelled = data_of(await self.call('naukri_apply', {'job': self.job, 'approval_id': approval}))
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertEqual(self.session.page_actions.clicks, 0)
        reuse = await self.call('naukri_apply', {'job': self.job, 'approval_id': approval})
        self.assertEqual(error_of(reuse)['code'], 'approval_invalid')

    async def test_questionnaire_flow(self):
        approval = (await self.prepare())['approval']['approval_id']
        questions = [
            {'qid': 'q_aaaaaaaaaaaa', 'label': 'Willing to relocate?', 'type': 'single_select', 'required': True,
             'options': [{'value': 'y', 'label': 'Yes'}, {'value': 'n', 'label': 'No'}], 'max_length': None,
             'min': None, 'max': None, '_locator': {'kind': 'radio', 'name': 'reloc'}},
            {'qid': 'q_bbbbbbbbbbbb', 'label': 'Notice period in days', 'type': 'number', 'required': True,
             'options': None, 'max_length': None, 'min': 0, 'max': 180, '_locator': {'kind': 'input', 'id': 'np'}}]
        self.session.decisions = ['submit']
        self.session.page_actions.outcomes = [{'kind': 'questions', 'questions': questions, 'url': 'x'}]
        first = data_of(await self.call('naukri_apply', {'job': self.job, 'approval_id': approval}))
        self.assertEqual(first['status'], 'needs_input')
        flow_id = first['flow_id']
        self.assertRegex(flow_id, r'^flw_[0-9a-f]{16}$')
        self.assertEqual([q['qid'] for q in first['questions']], ['q_aaaaaaaaaaaa', 'q_bbbbbbbbbbbb'])
        self.assertNotIn('_locator', json.dumps(first))
        self.assertEqual(self.session.page_actions.filled, [])  # nothing answered yet
        self.assertEqual(approvals.load(self.ws.root, approval)['status'], 'consumed')
        saved = store.read_flow(self.ws.root, flow_id)
        self.assertEqual((saved['status'], saved['job_id']), ('needs_input', self.job))

        bad = await self.call('naukri_prepare_apply', {'job': self.job, 'user_statement': STATEMENT, 'flow_id': flow_id,
                                                        'answers': [{'qid': 'q_aaaaaaaaaaaa', 'value': 'yes'},
                                                                    {'qid': 'q_bbbbbbbbbbbb', 'value': 30}]})
        self.assertEqual(error_of(bad)['code'], 'invalid_argument')
        no_flow = await self.call('naukri_prepare_apply', {'job': self.job, 'user_statement': STATEMENT,
                                                            'answers': [{'qid': 'q_aaaaaaaaaaaa', 'value': 'Yes'}]})
        self.assertEqual(error_of(no_flow)['code'], 'invalid_argument')
        answers = [{'qid': 'q_aaaaaaaaaaaa', 'value': 'Yes'}, {'qid': 'q_bbbbbbbbbbbb', 'value': 30}]
        prepared = await self.prepare(flow_id=flow_id, answers=answers)
        second = prepared['approval']
        self.assertEqual(second['binds']['flow_id'], flow_id)
        self.assertNotEqual(second['binds']['answers_sha256'], approvals.payload_hash([]))
        self.assertIn('Willing to relocate?: Yes', second['summary_for_user'])

        stale = await self.call('naukri_apply', {'job': self.job, 'approval_id': second['approval_id']})
        self.assertEqual(error_of(stale)['code'], 'approval_mismatch')  # flow approval used without its flow
        self.session.decisions = ['submit']
        self.session.page_actions.outcomes = [{'kind': 'applied', 'text': 'Applied to the job', 'url': 'x'}]
        done = data_of(await self.call('naukri_apply', {'job': self.job, 'approval_id': second['approval_id'],
                                                        'flow_id': flow_id}))
        self.assertEqual(done['status'], 'applied')
        self.assertEqual(self.session.page_actions.filled, [{'q_aaaaaaaaaaaa': 'Yes', 'q_bbbbbbbbbbbb': 30}])
        self.assertIn('Notice period in days: 30.', second['summary_for_user'])
        self.assertEqual([a['label'] for a in self.session.confirm_data['answers']],
                         ['Willing to relocate?', 'Notice period in days'])
        self.assertEqual(self.session.page_actions.clicks, 1)
        self.assertEqual(store.read_flow(self.ws.root, flow_id)['status'], 'applied')
        expired = await self.call('naukri_apply', {'job': self.job, 'approval_id': second['approval_id'],
                                                   'flow_id': flow_id})
        self.assertEqual(error_of(expired)['code'], 'flow_expired')

    async def test_a_claim_by_another_process_wins_and_nothing_is_clicked(self):
        prepared = await self.prepare()
        approval = prepared['approval']['approval_id']
        binds = prepared['approval']['binds']
        original = self.session.confirm

        async def other_host_clicks_first(data, timeout_s, tick=None):
            # Another host holding the same approval gets the user's click first.
            approvals.claim(self.ws.root, approval, 'naukri_apply', binds, {'by': 'other host'})
            return await original(data, timeout_s, tick)

        self.session.confirm = other_host_clicks_first
        self.session.decisions = ['submit']
        result = await self.call('naukri_apply', {'job': self.job, 'approval_id': approval})
        self.assertEqual(error_of(result)['code'], 'approval_invalid')
        self.assertEqual(self.session.page_actions.clicks, 0)
        self.assertEqual(approvals.load(self.ws.root, approval)['confirmation'], {'by': 'other host'})
        self.assertEqual(data_of(await self.call('naukri_status'))['usage'][-1]['used_today'], 0)  # nothing counted

    async def test_cancel_never_undoes_another_claim(self):
        prepared = await self.prepare()
        approval = prepared['approval']['approval_id']
        approvals.claim(self.ws.root, approval, 'naukri_apply', prepared['approval']['binds'], {'by': 'other host'})
        with mock.patch('jobfather_mcp.approvals.verify'):  # get past the up-front check to reach the cancel path
            self.session.decisions = ['cancel']
            result = data_of(await self.call('naukri_apply', {'job': self.job, 'approval_id': approval}))
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(approvals.load(self.ws.root, approval)['status'], 'confirmed')

    async def test_closed_flow_page_expires(self):
        approval = (await self.prepare())['approval']['approval_id']
        self.session.decisions = ['submit']
        self.session.page_actions.outcomes = [{'kind': 'questions', 'url': 'x', 'questions': [
            {'qid': 'q_cccccccccccc', 'label': 'Ready to join?', 'type': 'boolean', 'required': True, 'options':
             [{'value': 'on', 'label': 'Ready to join?'}], 'max_length': None, 'min': None, 'max': None,
             '_locator': {'kind': 'checkbox', 'name': 'ready'}}]}]
        flow_id = data_of(await self.call('naukri_apply', {'job': self.job, 'approval_id': approval}))['flow_id']
        self.session.flows[flow_id]['page'].closed = True
        result = await self.call('naukri_prepare_apply', {'job': self.job, 'user_statement': STATEMENT,
                                                           'flow_id': flow_id,
                                                           'answers': [{'qid': 'q_cccccccccccc', 'value': True}]})
        self.assertEqual(error_of(result)['code'], 'flow_expired')

    async def test_not_ready_jobs_get_no_approval(self):
        for state in ('already_applied', 'external_apply', 'closed'):
            self.session.page_actions.state_value = state
            prepared = await self.prepare()
            self.assertEqual(prepared['status'], state)
            self.assertIsNone(prepared['approval'])
        logged_out = install(self.ws, logged_in=False)
        result = await self.call('naukri_prepare_apply', {'job': self.job, 'user_statement': STATEMENT})
        self.assertEqual(error_of(result)['code'], 'auth_required')
        self.assertEqual(logged_out.page_actions.clicks, 0)


class ApplyCapTests(Base):
    limits = {'page': {'spacing_s': 0}, 'job': {'spacing_s': 0}, 'apply': {'spacing_s': 0, 'daily_cap': 1}}

    async def test_apply_cap_counts_confirmed_submits(self):
        session = install(self.ws)
        job = fixture_job_id()
        first = data_of(await self.call('naukri_prepare_apply', {'job': job, 'user_statement': STATEMENT}))
        session.decisions = ['cancel']
        await self.call('naukri_apply', {'job': job, 'approval_id': first['approval']['approval_id']})
        second = data_of(await self.call('naukri_prepare_apply', {'job': job, 'user_statement': STATEMENT}))
        session.decisions = ['submit']
        session.page_actions.outcomes = [{'kind': 'applied', 'text': 'Applied to the job', 'url': 'x'}]
        self.assertEqual(data_of(await self.call('naukri_apply', {
            'job': job, 'approval_id': second['approval']['approval_id']}))['status'], 'applied')
        third = data_of(await self.call('naukri_prepare_apply', {'job': job, 'user_statement': STATEMENT}))
        capped = await self.call('naukri_apply', {'job': job, 'approval_id': third['approval']['approval_id']})
        self.assertEqual(error_of(capped)['code'], 'rate_limited')
        self.assertTrue(error_of(capped)['retryable'])


if __name__ == '__main__':
    unittest.main()
