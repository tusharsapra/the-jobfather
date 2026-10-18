"""Live checks against naukri.com (opt-in: JOBFATHER_LIVE=1). Never applies; three page loads at most.

Uses a fresh temporary workspace (so a logged-out visitor) unless JOBFATHER_LIVE_HOME points at a
workspace with Naukri enabled. JOBFATHER_LIVE_STATUS=0 skips the protected-page status check.
A real Chrome window opens; loads are at least 10 seconds apart.
"""
import asyncio
import os
import unittest
from unittest import mock

from naukri_support import close_workspace, data_of, workspace

LIVE = os.environ.get('JOBFATHER_LIVE') == '1'


@unittest.skipUnless(LIVE, 'set JOBFATHER_LIVE=1 to check the real site (opens Chrome)')
class LiveNaukri(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from jobfather_mcp.naukri import site as site_mod
        home = os.environ.get('JOBFATHER_LIVE_HOME')
        self.ws = None
        if home:
            self.env = mock.patch.dict(os.environ, {'JOBFATHER_HOME': home, 'JOBFATHER_TEST_MODE': ''})
        else:
            self.ws = workspace(limits={})
            self.env = mock.patch.dict(os.environ, {'JOBFATHER_TEST_MODE': ''})  # real pacing, real site
        self.env.start()
        site_mod.reset()

    async def asyncTearDown(self):
        from jobfather_mcp.naukri import server
        if server.STATE.session is not None:
            await server.STATE.session.close()
            server.STATE.session = None

    def tearDown(self):
        self.env.stop()
        if self.ws is not None:
            close_workspace(self.ws)

    async def test_status_search_and_one_job(self):
        from mcp import Client
        from jobfather_mcp.naukri import server
        async with Client(server.app, read_timeout_seconds=180) as client:
            if os.environ.get('JOBFATHER_LIVE_STATUS', '1') != '0':
                status = data_of(await client.call_tool('naukri_status', {'check': 'live'}))
                self.assertIn(status['authenticated']['state'], ('yes', 'no', 'inconclusive'))
                await asyncio.sleep(10)
            search = data_of(await client.call_tool('naukri_search_jobs', {'keywords': 'data analyst', 'limit': 5}))
            self.assertGreater(len(search['jobs']), 0, search['warnings'])
            self.assertLessEqual(len(search['jobs']), 5)
            self.assertEqual(search['provenance']['method'], 'page_json')
            first = search['jobs'][0]
            self.assertRegex(first['job_id'], r'^\d{6,20}$')
            await asyncio.sleep(10)
            detail = data_of(await client.call_tool('naukri_get_job', {'job': first['url']}))
            self.assertEqual(detail['job']['job_id'], first['job_id'])
            self.assertTrue(detail['job']['description_text'])
            self.assertEqual(detail['provenance']['method'], 'page_json')
            status = data_of(await client.call_tool('naukri_status'))
            self.assertEqual(status['search_verified']['state'], 'yes')
            self.assertFalse(status['checkpoint']['active'])


if __name__ == '__main__':
    unittest.main()
