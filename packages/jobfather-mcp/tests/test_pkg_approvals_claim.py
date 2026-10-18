"""approvals.claim: the user's final click is claimed atomically, exactly once."""
import threading
import unittest

from pkgsupport import TempWorkspace

from jobfather_mcp import approvals as pkg

PAYLOAD = {'job_id': '012345678901', 'answers_sha256': pkg.payload_hash([]), 'flow_id': None}


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace(demo=False, policy='submit-with-approval')
        self.root = self.ws.root
        self.aid = pkg.create(self.root, 'naukri_apply', {'job': 'Example Corp'}, PAYLOAD, 'Apply to this one.',
                              'codex')['approval_id']

    def tearDown(self):
        self.ws.close()

    def code_of(self, call, *args, **kwargs):
        with self.assertRaises(pkg.ApprovalError) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def test_claim_once_then_consume(self):
        record = pkg.claim(self.root, self.aid, 'naukri_apply', PAYLOAD, {'by': 'user click'})
        self.assertEqual((record['status'], record['confirmation']), ('confirmed', {'by': 'user click'}))
        self.assertEqual(self.code_of(pkg.claim, self.root, self.aid, 'naukri_apply', PAYLOAD), 'approval_invalid')
        pkg.consume(self.root, self.aid, 'naukri_apply', PAYLOAD, {'outcome': 'applied'})
        self.assertEqual(self.code_of(pkg.claim, self.root, self.aid, 'naukri_apply', PAYLOAD), 'approval_consumed')
        events = [e['event'] for e in pkg.ledger(self.root) if e['approval_id'] == self.aid]
        self.assertEqual(events, ['created', 'confirmed', 'consumed'])

    def test_claim_checks_payload_kind_and_status(self):
        self.assertEqual(self.code_of(pkg.claim, self.root, self.aid, 'naukri_apply', dict(PAYLOAD, flow_id='flw_x')),
                         'approval_mismatch')
        self.assertEqual(self.code_of(pkg.claim, self.root, self.aid, 'ats_submit', PAYLOAD), 'approval_mismatch')
        self.assertEqual(self.code_of(pkg.claim, self.root, None, 'naukri_apply', PAYLOAD), 'approval_required')
        pkg.mark(self.root, self.aid, 'cancelled')
        self.assertEqual(self.code_of(pkg.claim, self.root, self.aid, 'naukri_apply', PAYLOAD), 'approval_invalid')

    def test_concurrent_claims_have_one_winner(self):
        outcomes = []

        def attempt():
            try:
                pkg.claim(self.root, self.aid, 'naukri_apply', PAYLOAD, {'by': 'click'})
                outcomes.append('ok')
            except pkg.ApprovalError as error:
                outcomes.append(error.code)

        threads = [threading.Thread(target=attempt) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ['approval_invalid'] * 5 + ['ok'])
        confirmed = [e for e in pkg.ledger(self.root) if e['event'] == 'confirmed']
        self.assertEqual(len(confirmed), 1)


if __name__ == '__main__':
    unittest.main()
