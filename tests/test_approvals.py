"""Per-item approvals: policy gate, exact payload binding, expiry, single use, tamper evidence."""
import json
import unittest
from unittest import mock

from support import DemoWorkspace, proposal

import approvals
import layout
from local import read_json
from memory import apply


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.demo = DemoWorkspace()
        self.ws = self.demo.root
        self.payload = {'job_id': 'gh-123', 'answers_sha256': 'abc'}

    def tearDown(self):
        self.demo.close()

    def set_mode(self, mode):
        p = proposal(self.ws, 'mode-' + mode, paths=('config/policy.json',))
        op = p['changes'][0]
        del op['append']
        op['json_set'] = [{'keys': ['mode'], 'value': mode}]
        p['user_request'] = 'Enable %s mode.' % mode
        apply(dict(p, change_type='policy'), self.ws)

    def test_drafts_only_refuses_every_outward_kind(self):
        for kind in approvals.KINDS:
            with self.assertRaises(ValueError):
                approvals.create(self.ws, kind, {'job': 'x'}, self.payload, 'Yes, submit it.', 'claude-code')

    def test_send_mode_allows_email_but_not_submissions(self):
        self.set_mode('send-with-approval')
        record = approvals.create(self.ws, 'email_send', {'to': 'hiring@example.com'}, self.payload, 'Send it.', 'codex')
        self.assertEqual(record['status'], 'pending')
        with self.assertRaises(ValueError):
            approvals.create(self.ws, 'ats_submit', {'job': 'x'}, self.payload, 'Submit.', 'codex')

    def test_binding_expiry_single_use_and_tamper(self):
        self.set_mode('submit-with-approval')
        record = approvals.create(self.ws, 'ats_submit', {'job': 'Example Corp'}, self.payload, 'Yes, submit this one.', 'codex')
        aid = record['approval_id']
        with self.assertRaises(ValueError):
            approvals.verify(self.ws, aid, 'ats_submit', dict(self.payload, answers_sha256='other'))
        with self.assertRaises(ValueError):
            approvals.verify(self.ws, aid, 'email_send', self.payload)
        approvals.verify(self.ws, aid, 'ats_submit', self.payload)
        approvals.mark(self.ws, aid, 'consumed', {'outcome': 'submitted'})
        with self.assertRaises(ValueError):
            approvals.verify(self.ws, aid, 'ats_submit', self.payload, allowed=('pending', 'consumed'))
        other = approvals.create(self.ws, 'ats_submit', {'job': 'Acme'}, self.payload, 'Submit Acme.', 'codex')
        path = self.ws / layout.APPROVALS / (other['approval_id'] + '.json')
        data = read_json(path)
        data['payload_sha256'] = approvals.payload_hash({'forged': True})
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(ValueError):
            approvals.load(self.ws, other['approval_id'])
        late = approvals.create(self.ws, 'ats_submit', {'job': 'Later'}, self.payload, 'Submit later.', 'codex', ttl_minutes=1)
        future = approvals._now() + approvals.timedelta(minutes=5)
        with mock.patch('approvals._now', return_value=future):
            with self.assertRaises(ValueError):
                approvals.verify(self.ws, late['approval_id'], 'ats_submit', self.payload)
        events = [e['event'] for e in approvals.ledger(self.ws)]
        self.assertIn('consumed', events)


if __name__ == '__main__':
    unittest.main()
