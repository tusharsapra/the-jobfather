"""Naukri pacing, daily caps, cooldowns and the login truth table (offline)."""
import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from naukri_support import close_workspace, workspace

from jobfather_mcp.errors import JFError
from jobfather_mcp.naukri import auth, limits, store


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now


class LimiterTests(unittest.TestCase):
    def setUp(self):
        self.ws = workspace(limits={})
        self.start = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc).timestamp()

    def tearDown(self):
        close_workspace(self.ws)

    def limiter(self, overrides=None, clock=None):
        return limits.Limiter(self.ws.root, overrides or {}, clock=clock or Clock(self.start))

    def test_fixed_spacing_per_action(self):
        clock = Clock(self.start)
        limiter = self.limiter(clock=clock)
        self.assertEqual(limiter.try_reserve(['page', 'search']), 0)
        self.assertEqual(limiter.try_reserve(['page', 'search']), 20)  # search spacing dominates
        clock.now += 6
        self.assertEqual(limiter.try_reserve(['page', 'job']), 0)        # page spacing (6 s) passed
        self.assertEqual(limiter.try_reserve(['page', 'search']), 14)
        clock.now += 14
        self.assertEqual(limiter.try_reserve(['page', 'search']), 0)
        usage = limiter.usage('search')
        self.assertEqual((usage['used_today'], usage['daily_cap']), (2, 30))
        self.assertIsNotNone(usage['next_allowed_at'])
        lines = [json.loads(x) for x in limiter.path.read_text().splitlines()]
        self.assertEqual({tuple(sorted(x)) for x in lines}, {('action', 'at', 'day', 'event', 't')})  # no arguments

    def test_daily_cap_and_local_midnight(self):
        local = datetime(2026, 10, 19, 23, 50).astimezone()
        clock = Clock(local.timestamp())
        limiter = self.limiter({'search': {'daily_cap': 2}}, clock)
        self.assertEqual(limiter.limits['search'][1], 2)
        for _ in range(2):
            self.assertEqual(limiter.try_reserve(['search']), 0)
            clock.now += 20
        with self.assertRaises(JFError) as caught:
            limiter.try_reserve(['search'])
        self.assertEqual(caught.exception.code, 'rate_limited')
        self.assertTrue(0 < caught.exception.retry_after_s <= 600)
        clock.now += 15 * 60  # a new local day
        self.assertEqual(limiter.try_reserve(['search']), 0)

    def test_acquire_sleeps_short_waits_and_refuses_long_ones(self):
        clock = Clock(self.start)
        limiter = self.limiter(clock=clock)
        limiter.try_reserve(['page', 'job'])
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)
            clock.now += seconds

        with mock.patch('asyncio.sleep', fake_sleep):
            asyncio.run(limiter.acquire(['page', 'job']))
            self.assertEqual(slept, [8])
            limiter.try_reserve(['apply'])
            with self.assertRaises(JFError) as caught:
                asyncio.run(limiter.wait_allowed(['apply']))
        self.assertEqual(caught.exception.code, 'rate_limited')
        self.assertEqual(caught.exception.retry_after_s, 120)

    def test_overrides_are_bounded_outside_test_mode(self):
        with mock.patch.dict(os.environ, {'JOBFATHER_TEST_MODE': ''}):
            values, notes = limits.effective_limits({'apply': {'daily_cap': 99, 'spacing_s': 1},
                                                     'search': {'spacing_s': 40, 'daily_cap': 5}, 'bogus': {}})
        self.assertEqual(values['apply'], (60, 25))
        self.assertEqual(values['search'], (40, 5))
        self.assertEqual(len(notes), 3)
        relaxed, _ = limits.effective_limits({'page': {'spacing_s': 0}})
        self.assertEqual(relaxed['page'][0], 0)  # test mode only

    def test_ledger_is_shared_between_limiters(self):
        clock = Clock(self.start)
        first, second = self.limiter(clock=clock), self.limiter(clock=clock)
        first.try_reserve(['page'])
        self.assertEqual(second.try_reserve(['page']), 6)


class AuthAndStatusTests(unittest.TestCase):
    def setUp(self):
        self.ws = workspace()

    def tearDown(self):
        close_workspace(self.ws)

    def test_truth_table(self):
        self.assertEqual(auth.classify(logged_in_marker=True)[0], 'yes')
        self.assertEqual(auth.classify(redirected_to_login=True, logged_in_marker=True)[0], 'no')
        self.assertEqual(auth.classify(login_form=True)[0], 'no')
        self.assertEqual(auth.classify(error='timeout')[0], 'inconclusive')
        self.assertEqual(auth.classify(checkpoint=True, logged_in_marker=True)[0], 'inconclusive')
        self.assertEqual(auth.classify()[0], 'inconclusive')
        self.assertEqual(auth.cached_auth(store.read_status(self.ws.root))['state'], 'unknown')  # never checked

    def test_status_file_holds_states_only_and_cooldown(self):
        store.set_auth(self.ws.root, 'yes', 'protected page with logged-in marker')
        store.set_search_verified(self.ws.root, 0)
        self.assertEqual(store.read_status(self.ws.root)['search_verified']['state'], 'inconclusive')
        store.set_search_verified(self.ws.root, 7)
        store.set_search_verified(self.ws.root, 0)
        self.assertEqual(store.read_status(self.ws.root)['search_verified']['state'], 'yes')
        store.set_checkpoint(self.ws.root, 'captcha', 'el_recaptcha')
        status = store.read_status(self.ws.root)
        self.assertEqual(status['auth']['state'], 'inconclusive')
        checkpoint = store.active_checkpoint(status)
        self.assertEqual(checkpoint['kind'], 'captcha')
        later = store.now_utc() + timedelta(hours=25)
        self.assertIsNone(store.active_checkpoint(status, later))
        store.clear_checkpoint(self.ws.root)
        self.assertIsNone(store.active_checkpoint(store.read_status(self.ws.root)))
        raw = json.loads((self.ws.root / '.state' / 'naukri' / 'status.json').read_text())
        self.assertEqual(set(raw), {'schema', 'auth', 'search_verified', 'checkpoint', 'login_poll', 'updated_at'})


if __name__ == '__main__':
    unittest.main()
