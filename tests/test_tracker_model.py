"""Tracker model (stdlib): sheet specs, chunking, cells, row builders and the three-way edit merge."""
from datetime import timedelta, timezone
import unittest

import support  # noqa: F401  (puts the skill scripts on sys.path)

import tracker_model as model

TORONTO = timezone(timedelta(hours=-4))
CTX = {'zone': TORONTO, 'tz_label': 'in America/Toronto', 'checked_at': '2026-10-08T14:00:00+00:00',
       'stages': ['New', 'Reviewing', 'Applied'], 'formats': {'date': '%Y-%m-%d', 'datetime': '%Y-%m-%d %H:%M'},
       'cv_files': {}, 'bounces': {}}
OUTREACH_HEADERS = ['Outreach ID', 'Company', 'Recipient', 'Channel', 'Status', 'Outreach type', 'Roles', 'Job IDs',
                    'Application ID', 'Subject', 'Drafted on', 'Sent on', 'CV file', 'Thread ID', 'Message ID',
                    'Draft ID', 'Sent link', 'Bounce reason', 'Reply status', 'Latest reply on', 'Reply link',
                    'Reply action', 'Follow-ups sent', 'Next follow-up', 'Follow-up date (editable)',
                    'Next action (editable)', 'Notes (editable)', 'Checked on']


def job(**changes):
    value = {'job_id': 'JOB-aaaaaaaaaaaa', 'company': 'Example Corp', 'role': 'Senior Data Analyst',
             'recruiter_name': None, 'email': 'hiring@example.com', 'location': 'Toronto, ON',
             'full_jd': 'Build SQL models.', 'workflow_status': 'New', 'user_notes': None, 'notes': None,
             'created_at': '2026-10-08T14:00:00.123456+00:00', 'posted_date': '2026-10-01', 'deadline': 'ASAP',
             'review_status': 'ready', 'review_reasons': [], 'application_id': None, 'history': [],
             'sources': [{'type': 'url', 'label': 'Listing', 'url': 'https://example.test/jobs/1', 'access': 'readable',
                          'text': 'Build SQL models.'}]}
    value.update(changes)
    return value


def project(jobs=None, outreach=(), summary=None, signals=()):
    data = {'jobs': list(jobs if jobs is not None else [job()]), 'outreach': list(outreach),
            'summary': summary or {}, 'signals': list(signals)}
    return model.build(data, CTX)


def sheet(projection, name):
    return next(s for s in projection['sheets'] if s['name'] == name)


def snapshot_of(projection, edits=None, drop=()):
    """What the reader would return for a workbook that holds exactly this projection, plus edits."""
    out = {'sheets': {}}
    for spec in projection['sheets']:
        rows = {}
        for row in spec['rows']:
            values = dict(zip(spec['headers'], row))
            key = model.row_key(spec, values)
            if (spec['name'], key) in drop:
                continue
            rows[key] = {h: model.normalize(values[h]) for h in spec['headers'] if h not in spec['derived']}
            rows[key].update((edits or {}).get((spec['name'], key), {}))
        out['sheets'][spec['name']] = {'present': True, 'rows': rows}
    return out


class TrackerModelTests(unittest.TestCase):
    def test_sheet_specs(self):
        specs = {s['name']: s for s in model.sheet_specs(['New'], 'in UTC')}
        self.assertEqual(list(specs), ['Jobs', 'Job Details', 'Outreach', 'Activity'])
        self.assertEqual(len(specs['Jobs']['headers']), 33)
        self.assertIn('Added on', specs['Jobs']['headers'])
        self.assertNotIn('Added on (UTC)', specs['Jobs']['headers'])
        self.assertEqual(specs['Jobs']['headers'][30:], ['Stage', 'Notes', 'Application ID'])
        self.assertEqual(specs['Job Details']['headers'], ['Job ID', 'Company', 'Role', 'Field', 'Part', 'Text', 'Source',
                                                           'Source link', 'Source access'])
        self.assertEqual(specs['Outreach']['headers'], OUTREACH_HEADERS)
        self.assertEqual(specs['Activity']['mode'], 'append')
        self.assertIn('in UTC', specs['Jobs']['note'])
        self.assertEqual(model.ILLEGAL_CHARACTERS_RE.pattern, r'[\000-\010]|[\013-\014]|[\016-\037]')

    def test_chunks_keep_every_character_within_limits(self):
        text = '=This is captured source text, not a formula.\n' + ('Full description, ₹ and \U0001F600.\n' * 1000)
        parts = model.chunks(text)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(model.utf16_len(p) <= 1000 and p.count('\n') <= 21 for p in parts))
        self.assertEqual(''.join(model.chunks('\n' * 2000)), '\n' * 2000)
        self.assertEqual(model.chunks(''), [])
        self.assertEqual(model.chunks(None), [])

    def test_excel_formats(self):
        self.assertEqual(model.excel_format('%Y-%m-%d'), 'yyyy-mm-dd')
        self.assertEqual(model.excel_format('%Y-%m-%d %H:%M'), 'yyyy-mm-dd hh:mm')
        self.assertEqual(model.excel_format('%d %b %Y, %I:%M %p'), 'dd mmm yyyy, hh:mm AM/PM')
        self.assertEqual(model.excel_format('%Y-%m-%dT%H'), 'yyyy-mm-dd\\Thh')
        with self.assertRaises(ValueError):
            model.excel_format('%-d/%m')

    def test_job_rows_and_details(self):
        long_summary = 'Summary line. ' * 200
        submitted = {'action': 'application-submitted', 'submitted_at': '2026-10-03T09:00:00-04:00',
                     'channel': 'greenhouse', 'evidence': {'kind': 'confirmation-page'}, 'application_id': 'APP-bbbbbbbbbbbb'}
        projection = project([job(jd_summary=long_summary, user_notes='Ask about hybrid days', notes='Captured note',
                                  review_status='needs_review', review_reasons=['Source access is partial or blocked'],
                                  history=[submitted], sources=[
                                      {'type': 'url', 'label': 'Listing', 'url': 'https://example.test/jobs/1',
                                       'access': 'readable', 'text': 'Build SQL models.'},
                                      {'type': 'text', 'label': 'Pasted', 'access': 'partial', 'text': None}])])
        jobs = sheet(projection, 'Jobs')
        row = dict(zip(jobs['headers'], jobs['rows'][0]))
        self.assertEqual(row['Added on'], {'datetime': '2026-10-08T10:00:00'})
        self.assertEqual(row['Posted date'], {'date': '2026-10-01'})
        self.assertEqual(row['Deadline'], 'ASAP')
        self.assertEqual(row['Notes'], 'Ask about hybrid days')
        self.assertEqual(row['Review'], 'Needs review')
        self.assertEqual(row['Missing information'], 'Person name')
        self.assertEqual(row['Source access'], 'readable, partial')
        self.assertTrue(row['JD summary'].endswith('Full value in Job Details.'))
        details = sheet(projection, 'Job Details')
        fields = {}
        for values in details['rows']:
            value = dict(zip(details['headers'], values))
            fields.setdefault(value['Field'], []).append(value)
        self.assertEqual(''.join(p['Text'] for p in fields['JD summary']), long_summary)
        self.assertEqual(fields['Captured notes'][0]['Text'], 'Captured note')
        self.assertIn('2026-10-03 09:00 via greenhouse', fields['Applications'][0]['Text'])
        self.assertEqual(fields['Captured source text'][0]['Text'], 'Build SQL models.')
        self.assertEqual(fields['Source (source 2)'][0]['Text'], 'Pasted')
        self.assertEqual(fields['Review reasons'][0]['Source access'], 'readable')
        self.assertEqual([p['Part'] for p in fields['JD summary']], list(range(1, len(fields['JD summary']) + 1)))

    def test_outreach_and_activity_rows(self):
        record = {'outreach_id': 'OUT-aaaaaaaaaaaa', 'company': 'Example Corp', 'recipient': 'hiring@example.com',
                  'channel': 'email', 'status': 'bounced', 'outreach_type': 'follow_up', 'roles': ['Senior Data Analyst'],
                  'job_ids': ['JOB-aaaaaaaaaaaa'], 'application_id': None, 'subject': 'Data role',
                  'draft': {'draft_id': 'd-1', 'message_id': 'm-1', 'thread_id': 't-1'},
                  'sent': {'message_id': 'm-1', 'thread_id': 't-1', 'sent_at': '2026-10-05T13:30:00Z',
                           'url': 'https://mail.google.com/mail/?authuser=alex.rivera@mail.example#all/m-1'},
                  'next_action': 'Find another contact', 'follow_up_override': '2026-10-20', 'notes': None,
                  'history': [{'action': 'drafted', 'at': '2026-10-04T20:00:00+00:00'}]}
        message = {'message_id': 'b-1', 'occurred_at': '2026-10-05T14:00:00Z', 'direction': 'incoming', 'signal': 'bounce',
                   'sender': 'mailer-daemon@example.test', 'subject': 'Undeliverable', 'quote': 'Address not found.',
                   'url': 'https://mail.google.com/mail/?authuser=alex.rivera@mail.example#all/b-1'}
        signal = {'signal_id': 'SIG-1', 'outreach_id': 'OUT-aaaaaaaaaaaa', 'application_id': None, 'message': message,
                  'recorded_at': '2026-10-05T15:00:00+00:00'}
        summary = {'OUT-aaaaaaaaaaaa': {'latest_reply': message, 'reply_action': None, 'follow_ups_sent': 1,
                                        'next_follow_up': None}}
        ctx_projection = model.build({'jobs': [job()], 'outreach': [record], 'summary': summary, 'signals': [signal]},
                                     dict(CTX, bounces={'OUT-aaaaaaaaaaaa': 'Address not found.'}))
        outreach = sheet(ctx_projection, 'Outreach')
        row = dict(zip(outreach['headers'], outreach['rows'][0]))
        self.assertEqual((row['Status'], row['Outreach type'], row['Bounce reason']),
                         ('Bounced', 'Follow-up', 'Address not found.'))
        self.assertEqual(row['Sent on'], {'datetime': '2026-10-05T09:30:00'})
        self.assertEqual(row['Drafted on'], {'datetime': '2026-10-04T16:00:00'})
        self.assertEqual(row['Follow-up date (editable)'], {'date': '2026-10-20'})
        self.assertEqual((row['Reply status'], row['Follow-ups sent']), ('Delivery failure', 1))
        activity = sheet(ctx_projection, 'Activity')
        values = dict(zip(activity['headers'], activity['rows'][0]))
        self.assertEqual((values['Signal ID'], values['Signal'], values['Company'], values['Job IDs']),
                         ('SIG-1', 'Delivery failure', 'Example Corp', 'JOB-aaaaaaaaaaaa'))

    def test_control_characters_are_removed_and_reported(self):
        projection = project([job(role='Analyst\x07 role\r\nsecond line')])
        row = dict(zip(sheet(projection, 'Jobs')['headers'], sheet(projection, 'Jobs')['rows'][0]))
        self.assertEqual(row['Role'], 'Analyst role\nsecond line')
        self.assertIn({'sheet': 'Jobs', 'header': 'Role', 'key': 'JOB-aaaaaaaaaaaa'}, projection['sanitized'])

    def test_three_way_merge(self):
        before = project()
        base = model.next_base(before, None, {}, 'sha-1', CTX['checked_at'])
        self.assertNotIn('Job Details', base['cells'])  # derived only: nothing to merge
        decided = model.merge(before, snapshot_of(before), base)
        self.assertEqual((decided['pull'], decided['conflicts'], decided['keep']), ([], [], {}))
        key = 'JOB-aaaaaaaaaaaa'
        # The user edited Stage and Email; the records are unchanged.
        edits = {('Jobs', key): {'Stage': 'Applied', 'Email': 'corrected@example.com', 'Source': 'Edited derived cell'}}
        decided = model.merge(before, snapshot_of(before, edits), base)
        self.assertEqual([(p['header'], p['sheet_value']) for p in decided['pull']], [('Stage', 'Applied')])
        self.assertEqual([k['header'] for k in decided['kept_edits']], ['Email'])
        self.assertEqual(decided['keep'], {'Jobs': {key: ['Email']}})
        # The records changed and the sheet did not: the JSON wins, nothing is kept.
        after = project([job(role='Lead Data Analyst')])
        decided = model.merge(after, snapshot_of(before), base)
        self.assertEqual((decided['pull'], decided['kept_edits'], decided['conflicts']), ([], [], []))
        # Both changed: the sheet value stays, a conflict is reported, the base keeps the old hash.
        decided = model.merge(after, snapshot_of(before, {('Jobs', key): {'Role': 'Principal Analyst'}}), base)
        self.assertEqual([(c['header'], c['sheet_value'], c['json_value']) for c in decided['conflicts']],
                         [('Role', 'Principal Analyst', 'Lead Data Analyst')])
        new_base = model.next_base(after, base, decided['keep'], 'sha-2', CTX['checked_at'])
        self.assertEqual(new_base['cells']['Jobs'][key]['Role'], base['cells']['Jobs'][key]['Role'])
        # Without any record of the last export, a difference is a conflict, never a silent overwrite.
        decided = model.merge(after, snapshot_of(before), None)
        self.assertEqual(decided['conflicts'][0]['reason'], 'no record of the last export for this cell')
        # A row deleted from the sheet comes back, and is reported.
        decided = model.merge(before, snapshot_of(before, drop={('Jobs', key)}), base)
        self.assertEqual(decided['restored_rows'], [{'sheet': 'Jobs', 'key': key}])

    def test_sheet_values_saved_back(self):
        self.assertEqual(model.text_value(42), '42')
        self.assertEqual(model.text_value({'date': '2026-10-20'}), '2026-10-20')
        self.assertIsNone(model.text_value(None))
        self.assertEqual(model.date_value({'datetime': '2026-10-20T00:00:00'}), '2026-10-20')
        self.assertEqual(model.date_value('2026-10-21'), '2026-10-21')
        with self.assertRaises(ValueError):
            model.text_value({'formula': '=A1'})
        with self.assertRaises(ValueError):
            model.date_value('next Monday')
        self.assertEqual(model.normalize(3.0), 3)
        self.assertEqual(model.cell_hash('a\r\nb'), model.cell_hash('a\nb'))
        self.assertEqual(model.cell_hash(''), model.cell_hash(None))


if __name__ == '__main__':
    unittest.main()
