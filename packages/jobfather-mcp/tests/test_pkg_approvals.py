"""Approvals: the package reads, verifies and marks the skill's records byte for byte, and vice versa."""
import json
import threading
import unittest
from unittest import mock

from pkgsupport import TempWorkspace, in_repo, skill_module

from jobfather_mcp import approvals as pkg
from jobfather_mcp.errors import JFError

PAYLOAD = {'job_id': 'gh-123', 'answers_sha256': 'fictional'}


class PackageApprovalTests(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace(demo=False, policy='submit-with-approval')
        self.root = self.ws.root

    def tearDown(self):
        self.ws.close()

    def code_of(self, call, *args, **kwargs):
        with self.assertRaises(pkg.ApprovalError) as caught:
            call(*args, **kwargs)
        self.assertIsInstance(caught.exception, JFError)
        self.assertIsInstance(caught.exception, ValueError)
        return caught.exception.code

    def test_policy_and_statement_are_required(self):
        self.ws.write('config/policy.json', {'schema_version': 1, 'mode': 'drafts-only'})
        self.assertEqual(self.code_of(pkg.create, self.root, 'naukri_apply', {'job': 'x'}, PAYLOAD, 'Yes.', 'codex'),
                         'approval_required')
        self.ws.write('config/policy.json', {'schema_version': 1, 'mode': 'submit-with-approval'})
        self.assertEqual(self.code_of(pkg.create, self.root, 'naukri_apply', {'job': 'x'}, PAYLOAD, ' ', 'codex'),
                         'approval_required')

    def test_error_codes_for_every_failure(self):
        record = pkg.create(self.root, 'naukri_apply', {'job': 'Example Corp'}, PAYLOAD, 'Apply to this one.', 'codex')
        aid = record['approval_id']
        self.assertEqual(self.code_of(pkg.verify, self.root, None, 'naukri_apply', PAYLOAD), 'approval_required')
        self.assertEqual(self.code_of(pkg.verify, self.root, 'apv_missing_1', 'naukri_apply', PAYLOAD), 'approval_invalid')
        self.assertEqual(self.code_of(pkg.verify, self.root, '../x', 'naukri_apply', PAYLOAD), 'approval_invalid')
        self.assertEqual(self.code_of(pkg.verify, self.root, aid, 'ats_submit', PAYLOAD), 'approval_mismatch')
        self.assertEqual(self.code_of(pkg.verify, self.root, aid, 'naukri_apply', {'other': 1}), 'approval_mismatch')
        later = pkg._now() + pkg.timedelta(minutes=30)
        with mock.patch('jobfather_mcp.approvals._now', return_value=later):
            self.assertEqual(self.code_of(pkg.verify, self.root, aid, 'naukri_apply', PAYLOAD), 'approval_expired')
        pkg.consume(self.root, aid, 'naukri_apply', PAYLOAD, {'outcome': 'submitted'})
        self.assertEqual(self.code_of(pkg.verify, self.root, aid, 'naukri_apply', PAYLOAD), 'approval_consumed')
        self.assertEqual(self.code_of(pkg.mark, self.root, aid, 'cancelled'), 'approval_consumed')
        path = self.root / pkg.APPROVALS / (aid + '.json')
        data = json.loads(path.read_bytes())
        data['user_statement'] = 'Forged words.'
        path.write_bytes(pkg.dump_record(data))
        self.assertEqual(self.code_of(pkg.load, self.root, aid), 'approval_invalid')

    def test_result_shape_for_approval_errors(self):
        error = pkg.ApprovalError('approval_expired', 'Approval expired; ask the user again')
        self.assertEqual(error.to_dict()['code'], 'approval_expired')
        self.assertFalse(error.retryable)
        self.assertTrue(error.user_action)

    def test_consume_is_single_use_under_concurrency(self):
        record = pkg.create(self.root, 'naukri_apply', {'job': 'Example Corp'}, PAYLOAD, 'Apply to this one.', 'codex')
        outcomes = []

        def attempt():
            try:
                pkg.consume(self.root, record['approval_id'], 'naukri_apply', PAYLOAD, {'outcome': 'submitted'})
                outcomes.append('ok')
            except pkg.ApprovalError as error:
                outcomes.append(error.code)

        threads = [threading.Thread(target=attempt) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ['approval_consumed'] * 5 + ['ok'])
        consumed = [e for e in pkg.ledger(self.root) if e['event'] == 'consumed']
        self.assertEqual(len(consumed), 1)

    def test_damaged_ledger_fails_closed(self):
        record = pkg.create(self.root, 'email_send', {'to': 'hiring@example.com'}, PAYLOAD, 'Send it.', 'claude-code')
        with (self.root / pkg.APPROVALS / 'ledger.jsonl').open('ab') as fh:
            fh.write(b'{"approval_id": "apv_')
        self.assertEqual(self.code_of(pkg.verify, self.root, record['approval_id'], 'email_send', PAYLOAD),
                         'approval_invalid')


@unittest.skipUnless(in_repo(), 'cross-compatibility needs the skill scripts from the repository')
class CrossCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.ws = TempWorkspace(demo=True, policy='submit-with-approval')
        self.root = self.ws.root
        self.skill = skill_module('approvals')

    def tearDown(self):
        self.ws.close()

    def test_skill_record_verifies_and_is_consumed_in_the_package(self):
        record = self.skill.create(self.root, 'ats_submit', {'job': 'Example Corp'}, PAYLOAD, 'Yes, submit it.', 'codex')
        aid = record['approval_id']
        path = self.root / pkg.APPROVALS / (aid + '.json')
        original = path.read_bytes()
        loaded = pkg.load(self.root, aid)
        self.assertEqual(pkg.dump_record(loaded), original)  # identical serialisation
        self.assertEqual(pkg.verify(self.root, aid, 'ats_submit', PAYLOAD)['approval_id'], aid)
        pkg.mark(self.root, aid, 'confirmed', {'by': 'user click'})
        self.assertEqual(self.skill.load(self.root, aid)['status'], 'confirmed')  # skill accepts the package MAC
        pkg.consume(self.root, aid, 'ats_submit', PAYLOAD, {'outcome': 'submitted'})
        with self.assertRaises(ValueError):
            self.skill.verify(self.root, aid, 'ats_submit', PAYLOAD, allowed=('pending', 'confirmed', 'consumed'))
        events = [e['event'] for e in self.skill.ledger(self.root) if e['approval_id'] == aid]
        self.assertEqual(events, ['created', 'confirmed', 'consumed'])

    def test_package_record_verifies_and_is_consumed_in_the_skill(self):
        record = pkg.create(self.root, 'email_send', {'to': 'hiring@example.com'}, PAYLOAD, 'Send this one.', 'codex')
        aid = record['approval_id']
        self.assertEqual(self.skill.load(self.root, aid), record)
        self.skill.verify(self.root, aid, 'email_send', PAYLOAD)
        self.skill.mark(self.root, aid, 'consumed', {'outcome': 'sent'})
        with self.assertRaises(pkg.ApprovalError) as caught:
            pkg.verify(self.root, aid, 'email_send', PAYLOAD)
        self.assertEqual(caught.exception.code, 'approval_consumed')

    def test_both_sides_use_one_key_and_one_ledger_format(self):
        first = self.skill.create(self.root, 'naukri_apply', {'job': 'A'}, PAYLOAD, 'Apply to A.', 'codex')
        second = pkg.create(self.root, 'naukri_apply', {'job': 'B'}, PAYLOAD, 'Apply to B.', 'codex')
        self.assertEqual(self.skill.ledger(self.root), pkg.ledger(self.root))
        lines = (self.root / pkg.APPROVALS / 'ledger.jsonl').read_bytes().decode('utf-8').splitlines()
        self.assertEqual(lines, [self.skill.canonical(e) for e in pkg.ledger(self.root)])
        self.assertEqual(self.skill.payload_hash(PAYLOAD), pkg.payload_hash(PAYLOAD))
        for record in (first, second):
            self.assertEqual(self.skill._mac(self.root, record), pkg._mac(self.root, record))

    def test_tampering_is_caught_on_both_sides(self):
        record = self.skill.create(self.root, 'naukri_apply', {'job': 'A'}, PAYLOAD, 'Apply to A.', 'codex')
        path = self.root / pkg.APPROVALS / (record['approval_id'] + '.json')
        data = json.loads(path.read_bytes())
        data['payload_sha256'] = pkg.payload_hash({'forged': True})
        path.write_bytes(pkg.dump_record(data))
        with self.assertRaises(ValueError):
            self.skill.load(self.root, record['approval_id'])
        with self.assertRaises(pkg.ApprovalError):
            pkg.load(self.root, record['approval_id'])


if __name__ == '__main__':
    unittest.main()
