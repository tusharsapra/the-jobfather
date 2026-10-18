"""End to end against a loopback fake Naukri with real Chrome (opt-in: JOBFATHER_E2E=1).

The test plays the human: it clicks the fake login button and the confirm tab's buttons in the
server's own browser. Base URL and channel overrides work only because JOBFATHER_TEST_MODE=1 and
the base is a loopback address. Runs headless unless JOBFATHER_E2E_HEADED=1.
"""
import asyncio
import json
import os
import unittest
from unittest import mock

from naukri_support import close_workspace, data_of, error_of, workspace

if os.environ.get('JOBFATHER_E2E') == '1':
    from mcp import Client
    from naukri_fake_site import APPLIED, BLOCKED, CANCEL, EXTERNAL, PLAIN, QUESTIONS, SPARE, FakeNaukri
    from jobfather_mcp.naukri import server, site as site_mod

STATEMENT = 'Yes, please apply to this one for me.'
LIMITS = {'page': {'spacing_s': 0}, 'search': {'spacing_s': 0}, 'job': {'spacing_s': 0},
          'similar': {'spacing_s': 0}, 'recommended': {'spacing_s': 0}, 'saved': {'spacing_s': 0},
          'apply': {'spacing_s': 0, 'daily_cap': 2}}


@unittest.skipUnless(os.environ.get('JOBFATHER_E2E') == '1', 'set JOBFATHER_E2E=1 (needs Google Chrome)')
class FakeSiteEndToEnd(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.site = FakeNaukri().__enter__()
        self.ws = workspace(limits=LIMITS)
        env = {'JOBFATHER_NAUKRI_BASE': self.site.base, 'JOBFATHER_TEST_MODE': '1'}
        if os.environ.get('JOBFATHER_E2E_HEADED') != '1':
            env['JOBFATHER_NAUKRI_HEADLESS'] = '1'
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        site_mod.reset()

    async def asyncTearDown(self):
        if server.STATE.session is not None:
            await server.STATE.session.close()

    def tearDown(self):
        self.env.stop()
        close_workspace(self.ws)
        self.site.__exit__(None, None, None)

    async def call(self, client, name, args=None):
        return await client.call_tool(name, args or {})

    async def human_click(self, title_prefix, button, timeout=30):
        """Act as the user: find the tab and click a button in it."""
        for _ in range(int(timeout * 4)):
            session = server.STATE.session
            if session is not None and session.context is not None:
                for page in reversed(session.context.pages):  # the newest tab first
                    try:
                        if (await page.title()).startswith(title_prefix):
                            await page.get_by_role('button', name=button).click()
                            return
                    except Exception:
                        continue
            await asyncio.sleep(0.25)
        raise AssertionError('no %r tab appeared' % title_prefix)

    async def apply_with_click(self, client, job, approval_id, button, flow_id=None):
        args = {'job': job, 'approval_id': approval_id, 'confirm_timeout_s': 60}
        if flow_id:
            args['flow_id'] = flow_id
        task = asyncio.ensure_future(self.call(client, 'naukri_apply', args))
        await self.human_click('Confirm', button)
        return await asyncio.wait_for(task, 90)

    async def test_full_flow(self):
        async with Client(server.app, read_timeout_seconds=180) as client:
            status = data_of(await self.call(client, 'naukri_status'))
            self.assertFalse(status['browser']['running'])
            self.assertEqual(status['authenticated']['state'], 'unknown')

            # public reads, logged out
            search = data_of(await self.call(client, 'naukri_search_jobs', {'keywords': 'data analyst', 'limit': 5}))
            self.assertEqual(len(search['jobs']), 5)
            self.assertEqual(search['provenance']['method'], 'page_json')
            detail = data_of(await self.call(client, 'naukri_get_job', {'job': PLAIN}))
            self.assertEqual(detail['job']['title'], 'Fictional Role %s' % PLAIN[-4:])
            similar = data_of(await self.call(client, 'naukri_similar_jobs', {'job': PLAIN, 'limit': 3}))
            self.assertEqual(len(similar['jobs']), 3)
            gone = await self.call(client, 'naukri_get_job', {'job': '099999999999'})
            self.assertEqual(error_of(gone)['code'], 'not_found')
            needs_login = await self.call(client, 'naukri_recommended_jobs')
            self.assertEqual(error_of(needs_login)['code'], 'auth_required')
            status = data_of(await self.call(client, 'naukri_status', {'check': 'live'}))
            self.assertEqual(status['authenticated']['state'], 'no')
            self.assertEqual(status['search_verified']['state'], 'yes')
            self.assertTrue(status['browser']['running'])

            # the user signs in by hand
            login = asyncio.ensure_future(self.call(client, 'naukri_login', {'wait_s': 30}))
            await self.human_click('Login', 'Sign in (test)')
            result = data_of(await asyncio.wait_for(login, 60))
            self.assertEqual(result['state'], 'logged_in')
            self.assertEqual(result['authenticated']['state'], 'yes')

            profile = data_of(await self.call(client, 'naukri_my_profile'))
            self.assertEqual(profile['resume']['file_name'], 'demo_resume.pdf')
            self.assertEqual(profile['completeness_pct'], 88)
            self.assertNotIn('example.com', json.dumps(profile))
            self.assertNotIn('0142', json.dumps(profile))
            recommended = data_of(await self.call(client, 'naukri_recommended_jobs', {'limit': 3}))
            self.assertEqual(len(recommended['jobs']), 3)
            saved = data_of(await self.call(client, 'naukri_saved_jobs'))
            self.assertEqual(saved['jobs'], [])

            # apply: confirm click submits once
            prepared = data_of(await self.call(client, 'naukri_prepare_apply', {'job': PLAIN, 'user_statement': STATEMENT}))
            self.assertEqual(prepared['status'], 'ready')
            self.assertEqual(prepared['resume_used']['file_name'], 'demo_resume.pdf')
            self.assertEqual(self.site.applications, [])
            applied = data_of(await self.apply_with_click(client, PLAIN, prepared['approval']['approval_id'],
                                                          'Submit application'))
            self.assertEqual(applied['status'], 'applied', applied)
            self.assertEqual(self.site.applications, [{'job_id': PLAIN, 'answers': {}}])
            self.assertTrue((self.ws.root / applied['evidence']['screenshot']).is_file())

            # cancel sends nothing
            other = data_of(await self.call(client, 'naukri_prepare_apply', {'job': CANCEL, 'user_statement': STATEMENT}))
            cancelled = data_of(await self.apply_with_click(client, CANCEL, other['approval']['approval_id'], 'Cancel'))
            self.assertEqual(cancelled['status'], 'cancelled')
            self.assertEqual(len(self.site.applications), 1)

            # questionnaire: needs_input, approved answers, then submit
            first = data_of(await self.call(client, 'naukri_prepare_apply', {'job': QUESTIONS, 'user_statement': STATEMENT}))
            asked = data_of(await self.apply_with_click(client, QUESTIONS, first['approval']['approval_id'],
                                                        'Submit application'))
            self.assertEqual(asked['status'], 'needs_input', asked)
            labels = {q['label']: q for q in asked['questions']}
            self.assertEqual(set(labels), {'Are you willing to relocate?', 'Notice period in days'})
            self.assertEqual(len(self.site.applications), 1)  # nothing sent yet
            answers = [{'qid': labels['Are you willing to relocate?']['qid'], 'value': 'Yes'},
                       {'qid': labels['Notice period in days']['qid'], 'value': 30}]
            second = data_of(await self.call(client, 'naukri_prepare_apply', {
                'job': QUESTIONS, 'user_statement': STATEMENT, 'answers': answers, 'flow_id': asked['flow_id']}))
            done = data_of(await self.apply_with_click(client, QUESTIONS, second['approval']['approval_id'],
                                                       'Submit application', flow_id=asked['flow_id']))
            self.assertEqual(done['status'], 'applied', done)
            self.assertEqual(self.site.applications[-1], {'job_id': QUESTIONS, 'answers': {'reloc': 'yes',
                                                                                           'notice': '30'}})

            # other job states and the daily apply cap (2 confirmed submits so far)
            external = data_of(await self.call(client, 'naukri_prepare_apply', {'job': EXTERNAL, 'user_statement': STATEMENT}))
            self.assertEqual(external['status'], 'external_apply')
            self.assertEqual(external['external_apply_url'], 'https://careers.example.com/apply/123')
            already = data_of(await self.call(client, 'naukri_prepare_apply', {'job': APPLIED, 'user_statement': STATEMENT}))
            self.assertEqual(already['status'], 'already_applied')
            spare = data_of(await self.call(client, 'naukri_prepare_apply', {'job': SPARE, 'user_statement': STATEMENT}))
            capped = await self.call(client, 'naukri_apply', {'job': SPARE, 'approval_id': spare['approval']['approval_id']})
            self.assertEqual(error_of(capped)['code'], 'rate_limited')

            # a block page stops everything until the user signs in again
            blocked = await self.call(client, 'naukri_get_job', {'job': BLOCKED})
            self.assertEqual(error_of(blocked)['code'], 'blocked')
            paused = await self.call(client, 'naukri_search_jobs', {'keywords': 'data analyst'})
            self.assertEqual(error_of(paused)['code'], 'blocked')
            status = data_of(await self.call(client, 'naukri_status'))
            self.assertTrue(status['checkpoint']['active'])
            relogin = data_of(await self.call(client, 'naukri_login', {'wait_s': 5}))
            self.assertEqual(relogin['state'], 'logged_in')  # still signed in: the pause ends
            self.assertFalse(data_of(await self.call(client, 'naukri_status'))['checkpoint']['active'])

            closed = data_of(await self.call(client, 'naukri_close'))
            self.assertTrue(closed['closed'])
            self.assertFalse(data_of(await self.call(client, 'naukri_status'))['browser']['running'])
        self.assertFalse((self.ws.root / '.state' / 'naukri' / 'browser.lock').exists())


if __name__ == '__main__':
    unittest.main()
