"""JFError: validation, the JSON shape, MCP results and the tool decorator (sync, async, MCPServer)."""
import asyncio
import json
import unittest

import pkgsupport  # noqa: F401  (puts src on sys.path)

from mcp.server import MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult

from jobfather_mcp.errors import CODES, JFError, as_jferror, error_result, tool_errors


class ErrorTests(unittest.TestCase):
    def test_codes_are_the_documented_set(self):
        self.assertEqual(len(CODES), len(set(CODES)))
        for code in ('not_enabled', 'workspace_invalid', 'invalid_argument', 'browser_unavailable', 'profile_locked',
                     'auth_required', 'auth_inconclusive', 'checkpoint', 'blocked', 'rate_limited', 'site_changed',
                     'not_found', 'closed', 'approval_required', 'approval_invalid', 'approval_expired',
                     'approval_consumed', 'approval_mismatch', 'flow_expired', 'timeout', 'internal'):
            self.assertIn(code, CODES)
        self.assertEqual(len(CODES), 21)

    def test_validation(self):
        with self.assertRaises(ValueError):
            JFError('nope', 'x', False)
        with self.assertRaises(ValueError):
            JFError('timeout', '', True)
        with self.assertRaises(ValueError):
            JFError('rate_limited', 'Slow down.', True, retry_after_s=-1)
        with self.assertRaises(ValueError):
            JFError('rate_limited', 'Slow down.', True, retry_after_s=True)

    def test_result_carries_the_same_object_in_text_and_structured_content(self):
        result = JFError('rate_limited', 'Daily cap reached.', True, retry_after_s=600,
                         user_action='Wait until tomorrow.').to_result()
        self.assertIsInstance(result, CallToolResult)
        self.assertTrue(result.is_error)
        expected = {'code': 'rate_limited', 'message': 'Daily cap reached.', 'retryable': True,
                    'retry_after_s': 600, 'user_action': 'Wait until tomorrow.'}
        self.assertEqual(json.loads(result.content[0].text), {'error': expected})
        self.assertEqual(result.structured_content, {'error': expected})
        wire = result.model_dump(by_alias=True, mode='json', exclude_none=True)
        self.assertTrue(wire['isError'])
        self.assertEqual(error_result('closed', 'The job is closed.', False).structured_content['error']['code'], 'closed')

    def test_unexpected_exceptions_become_internal_without_details(self):
        with self.assertLogs('jobfather_mcp', level='ERROR'):
            error = as_jferror(KeyError('secret detail'), 'naukri_get_job')
        self.assertEqual(error.code, 'internal')
        self.assertNotIn('secret detail', error.message)
        self.assertEqual(as_jferror(TimeoutError()).code, 'timeout')
        self.assertTrue(as_jferror(TimeoutError()).retryable)

    def test_decorator_sync_and_async_keep_signatures(self):
        @tool_errors
        def lookup(job_id: str) -> dict:
            raise JFError('not_found', 'No job ' + job_id, False)

        @tool_errors
        async def fetch(job_id: str) -> dict:
            if job_id == 'protocol':
                raise MCPError(code=-32602, message='bad params')
            return {'job_id': job_id}

        self.assertEqual(lookup('7').structured_content['error']['code'], 'not_found')
        self.assertEqual(asyncio.run(fetch('8')), {'job_id': '8'})
        with self.assertRaises(MCPError):
            asyncio.run(fetch('protocol'))
        self.assertTrue(asyncio.iscoroutinefunction(fetch))
        self.assertEqual(fetch.__wrapped__.__name__, 'fetch')

    def test_works_inside_mcpserver(self):
        app = MCPServer('errors-test')

        @app.tool()
        @tool_errors
        async def get_job(job_id: str) -> dict:
            """Fictional lookup."""
            if job_id == 'gone':
                raise JFError('closed', 'The job no longer accepts applications.', False)
            return {'job_id': job_id, 'title': 'Data Analyst'}

        tools = asyncio.run(app.list_tools())
        self.assertEqual(tools[0].input_schema['required'], ['job_id'])
        ok = asyncio.run(app.call_tool('get_job', {'job_id': 'j1'}))
        self.assertFalse(ok.is_error)
        failed = asyncio.run(app.call_tool('get_job', {'job_id': 'gone'}))
        self.assertTrue(failed.is_error)
        self.assertEqual(failed.structured_content['error']['code'], 'closed')


if __name__ == '__main__':
    unittest.main()
