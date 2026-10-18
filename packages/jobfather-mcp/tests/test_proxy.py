"""jobfather-proxy end to end: a real child MCP server behind the proxy, driven by a real MCP client."""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import anyio

from pkgsupport import FAKE_CHILD, SRC, TempWorkspace

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from jobfather_mcp import proxy
from jobfather_mcp.errors import JFError


def proxy_params(*options, child=(sys.executable, str(FAKE_CHILD))):
    env = dict(os.environ, PYTHONPATH=str(SRC) + os.pathsep + os.environ.get('PYTHONPATH', ''))
    return StdioServerParameters(command=sys.executable, env=env,
                                 args=['-m', 'jobfather_mcp.proxy', *options, '--', *child])


def error_of(result):
    return json.loads(result.content[0].text)['error'] if result.is_error else None


class ArgumentTests(unittest.TestCase):
    def test_parse_and_validate(self):
        config = proxy.parse_args(['--server-name', 'linkedin', '--allow', 'search_jobs, get_job_details,search_jobs',
                                   '--daily-cap', 'search_jobs=40', '--', 'uvx', 'mcp-server-linkedin@4.27.0',
                                   '--transport', 'stdio'])
        self.assertEqual(config.allow, ('search_jobs', 'get_job_details'))
        self.assertEqual(config.caps, {'search_jobs': 40})
        self.assertEqual((config.command, config.args), ('uvx', ('mcp-server-linkedin@4.27.0', '--transport', 'stdio')))
        bad = (['--server-name', 'LinkedIn!', '--allow', 'a', '--', 'x'],
               ['--server-name', 'x', '--allow', ' , ', '--', 'x'],
               ['--server-name', 'x', '--allow', 'a', '--daily-cap', 'b=3', '--', 'x'],
               ['--server-name', 'x', '--allow', 'a', '--daily-cap', 'a=-1', '--', 'x'],
               ['--server-name', 'x', '--allow', 'a b', '--', 'x'],
               ['--server-name', 'x', '--allow', 'a'])
        for argv in bad:
            with self.subTest(argv=argv), mock.patch('sys.stderr'):
                with self.assertRaises(SystemExit):
                    proxy.parse_args(argv)


class CapTests(unittest.TestCase):
    def test_caps_count_per_local_day_in_the_ledger(self):
        ws = TempWorkspace(demo=False)
        try:
            caps = proxy.DailyCaps('demo', {'search': 2}, ws.root)
            day_one = datetime(2026, 10, 18, 23, 50, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            with mock.patch.object(proxy.DailyCaps, 'clock', staticmethod(lambda: day_one)):
                caps.reserve_sync('search')
                caps.reserve_sync('search')
                caps.reserve_sync('other')  # uncapped tools are not counted
                with self.assertRaises(JFError) as caught:
                    caps.reserve_sync('search')
            self.assertEqual(caught.exception.code, 'rate_limited')
            self.assertEqual(caught.exception.retry_after_s, 600)
            with mock.patch.object(proxy.DailyCaps, 'clock', staticmethod(lambda: day_one + timedelta(minutes=15))):
                caps.reserve_sync('search')  # a new local day
            lines = [json.loads(x) for x in (ws.root / '.state' / 'proxy' / 'demo-ledger.jsonl').read_text().splitlines()]
            self.assertEqual([x['event'] for x in lines], ['call', 'call', 'refused', 'call'])
            self.assertEqual({tuple(sorted(x)) for x in lines}, {('at', 'day', 'event', 'tool')})  # no arguments stored
            memory = proxy.DailyCaps('demo', {'search': 1}, None)
            memory.reserve_sync('search')
            with self.assertRaises(JFError):
                memory.reserve_sync('search')
        finally:
            ws.close()


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ws = TempWorkspace(demo=False)

    def tearDown(self):
        self.ws.close()

    async def test_filters_refuses_caps_forwards_and_restarts(self):
        logs, progress = [], []

        async def on_log(params):
            logs.append((params.level, params.data))

        async def on_progress(value, total, message):
            progress.append((value, total, message))

        params = proxy_params('--server-name', 'fake', '--allow', 'echo,count,crash', '--daily-cap', 'echo=2')
        with anyio.fail_after(90), open(os.devnull, 'w') as sink:
            async with stdio_client(params, errlog=sink) as (read, write):
                async with ClientSession(read, write, logging_callback=on_log) as session:
                    init = await session.initialize()
                    self.assertEqual(init.server_info.name, 'jobfather-fake')
                    listed = await session.list_tools()
                    self.assertEqual(sorted(t.name for t in listed.tools), ['count', 'crash', 'echo'])
                    self.assertEqual(listed.tools[0].input_schema['type'], 'object')

                    first = await session.call_tool('echo', {'text': 'hello'})
                    self.assertEqual((first.is_error, first.content[0].text), (False, 'echo:hello'))

                    refused = await session.call_tool('send_message', {'to': 'someone', 'body': 'hi'})
                    self.assertEqual(error_of(refused)['code'], 'not_enabled')
                    self.assertEqual(refused.structured_content['error']['code'], 'not_enabled')

                    counted = await session.call_tool('count', {'n': 3}, progress_callback=on_progress)
                    self.assertEqual(counted.content[0].text, 'counted:3')
                    self.assertEqual([p[0] for p in progress], [1, 2, 3])
                    await anyio.sleep(0.2)
                    self.assertIn(('info', 'counted to 3'), logs)

                    await session.call_tool('echo', {'text': 'two'})
                    capped = await session.call_tool('echo', {'text': 'three'})
                    self.assertEqual(error_of(capped)['code'], 'rate_limited')
                    self.assertTrue(error_of(capped)['retryable'])

                    crashed = await session.call_tool('crash', {})
                    self.assertEqual(error_of(crashed)['code'], 'browser_unavailable')
                    again = await session.call_tool('count', {'n': 1})
                    self.assertEqual(again.content[0].text, 'counted:1')  # the child was restarted
        ledger = (self.ws.root / '.state' / 'proxy' / 'fake-ledger.jsonl').read_text().splitlines()
        self.assertEqual([json.loads(x)['event'] for x in ledger], ['call', 'call', 'refused'])

    async def test_a_child_that_cannot_start_is_reported(self):
        params = proxy_params('--server-name', 'broken', '--allow', 'echo', '--startup-timeout', '20',
                              child=('jobfather-no-such-command-for-tests',))
        with anyio.fail_after(60), open(os.devnull, 'w') as sink:
            async with stdio_client(params, errlog=sink) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    with self.assertRaises(Exception) as caught:
                        await session.list_tools()
                    self.assertIn('broken server is not available', str(caught.exception))
                    result = await session.call_tool('echo', {'text': 'x'})
                    self.assertEqual(error_of(result)['code'], 'browser_unavailable')


if __name__ == '__main__':
    unittest.main()
