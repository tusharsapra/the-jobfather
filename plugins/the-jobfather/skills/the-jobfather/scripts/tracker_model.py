"""Tracker workbook model (stdlib only): sheets, cells, row builders and the three-way edit merge.

tracker.py builds a projection of the JSON stores with this module; tracker_xlsx.py (openpyxl) reads
and writes jobs/outputs/Tracker.xlsx only from that projection. For every kept cell, against the
values written by the last export (.state/tracker/export-base.json):

    sheet == JSON                 nothing to do
    sheet == last export          the JSON changed: write the JSON value
    JSON == last export           the user edited the sheet: an editable cell is saved back into the
                                  JSON stores; any other cell keeps the user's correction
    otherwise (both changed)      keep the sheet value, report a conflict, leave the JSON alone

Derived columns (sources, review state, outreach status and mail activity) always show the JSON.
"""
import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta, timezone

SCHEMA_VERSION = 1
TITLE_ROW, NOTE_ROW, HEADER_ROW, FIRST_ROW = 2, 3, 5, 6
FREEZE = 'D6'
FONT = 'Arial'
HEADER_FILL = '243447'
# Same pattern as openpyxl.cell.cell.ILLEGAL_CHARACTERS_RE (characters XML 1.0 cannot hold).
ILLEGAL_CHARACTERS_RE = re.compile(r'[\000-\010]|[\013-\014]|[\016-\037]')
CELL_LIMIT = 32767
CHUNK_CHARS, CHUNK_LINES = 1000, 22
COMPACT_AT, COMPACT_KEEP = 1500, 1450
KEY_JOIN = '\x1f'

JOB_COLUMNS = (
    ('job_id', 'Job ID', 18), ('company', 'Company', 26), ('role', 'Role', 34),
    ('recruiter_name', 'Person name', 24), ('email', 'Email', 36),
    ('location', 'Location', 23), ('work_mode', 'Work mode', 17),
    ('employment_type', 'Job type', 20), ('experience', 'Experience', 20),
    ('salary', 'Salary', 24), ('skills', 'Skills', 38), ('jd_summary', 'JD summary', 58),
    ('application_method', 'How to apply', 28), ('application_url', 'Application link', 42),
    ('recruiter_company', 'Recruiter company', 27), ('recruiter_title', 'Person title', 25),
    ('phone', 'Phone', 22), ('recruiter_linkedin', 'Person LinkedIn', 42),
    ('post_author', 'Post author', 24), ('posted_date', 'Posted date', 20),
    ('deadline', 'Deadline', 20), ('joining_timeline', 'Joining timeline', 24),
    ('shift', 'Shift', 30), ('working_days', 'Working days', 23),
    ('source_label', 'Source', 32), ('source_url', 'Source link', 44),
    ('source_access', 'Source access', 18), ('created_at', 'Added on', 23),
    ('review', 'Review', 27), ('missing', 'Missing information', 44),
    ('workflow_status', 'Stage', 23), ('user_notes', 'Notes', 52),
    ('application_id', 'Application ID', 24),
)
JOB_DERIVED = ('Job ID', 'Source', 'Source link', 'Source access', 'Added on', 'Review', 'Missing information',
               'Application ID')
JOB_EDITABLE = ('Stage', 'Notes')
MISSING = (('company', 'Company'), ('role', 'Role'), ('recruiter_name', 'Person name'), ('email', 'Email'),
           ('location', 'Location'), ('full_jd', 'Full JD'))
DETAIL_HEADERS = ('Job ID', 'Company', 'Role', 'Field', 'Part', 'Text', 'Source', 'Source link', 'Source access')
DETAIL_WIDTHS = (18, 26, 34, 27, 8, 100, 32, 44, 18)
DETAIL_FIELDS = (('full_jd', 'Full JD'), ('responsibilities', 'Responsibilities'), ('requirements', 'Requirements'),
                 ('benefits', 'Benefits'), ('requisition_id', 'Requisition ID'),
                 ('additional_contacts', 'Additional contacts'), ('uncertain_fields', 'Uncertain fields'),
                 ('conflicts', 'Conflicting values'), ('duplicate_candidates', 'Possible duplicate Job IDs'),
                 ('notes', 'Captured notes'))
OUTREACH_COLUMNS = (
    ('Outreach ID', 20), ('Company', 26), ('Recipient', 32), ('Channel', 12), ('Status', 16), ('Outreach type', 16),
    ('Roles', 34), ('Job IDs', 20), ('Application ID', 20), ('Subject', 40), ('Drafted on', 20), ('Sent on', 20),
    ('CV file', 36), ('Thread ID', 24), ('Message ID', 24), ('Draft ID', 24), ('Sent link', 44),
    ('Bounce reason', 40), ('Reply status', 18), ('Latest reply on', 20), ('Reply link', 44), ('Reply action', 32),
    ('Follow-ups sent', 14), ('Next follow-up', 20), ('Follow-up date (editable)', 20),
    ('Next action (editable)', 36), ('Notes (editable)', 48), ('Checked on', 20),
)
OUTREACH_EDITABLE = ('Follow-up date (editable)', 'Next action (editable)', 'Notes (editable)')
ACTIVITY_COLUMNS = (
    ('Signal ID', 30), ('Occurred on', 20), ('Direction', 12), ('Signal', 20), ('Company', 26), ('Outreach ID', 20),
    ('Job IDs', 20), ('Application ID', 20), ('From', 32), ('Subject', 40), ('Quote', 60), ('Message link', 44),
    ('Action', 32), ('Recorded on', 20),
)
STATUS_LABELS = {'drafted': 'Drafted', 'sent': 'Sent', 'bounced': 'Bounced', 'draft-deleted': 'Draft deleted',
                 'cancelled': 'Cancelled'}
TYPE_LABELS = {'application': 'Application', 'enquiry': 'Enquiry', 'follow_up': 'Follow-up', 'referral': 'Referral'}
SIGNAL_LABELS = {'acknowledgement': 'Acknowledgement', 'hr_question': 'HR question', 'assessment': 'Assessment',
                 'interview': 'Interview', 'offer': 'Offer', 'rejection': 'Rejection', 'bounce': 'Delivery failure',
                 'follow_up': 'Follow-up sent', 'sent_reply': 'Reply sent', 'other_reply': 'Recruiter reply'}
_DIRECTIVES = {'Y': 'yyyy', 'y': 'yy', 'm': 'mm', 'd': 'dd', 'b': 'mmm', 'B': 'mmmm', 'a': 'ddd', 'A': 'dddd',
               'H': 'hh', 'I': 'hh', 'M': 'mm', 'S': 'ss', 'p': 'AM/PM'}


# ------------------------------------------------------------------ sheet specs
def sheet_specs(stages, tz_label):
    times = 'Times are %s.' % tz_label
    return [
        {'name': 'Jobs', 'table': 'JobListings', 'title': 'Job listings',
         'note': 'Stage and Notes are yours to edit: the next export saves them. Full descriptions are in '
                 'Job Details. ' + times,
         'headers': [h for _, h, _ in JOB_COLUMNS], 'widths': [w for _, _, w in JOB_COLUMNS], 'key': ['Job ID'],
         'mode': 'merge', 'editable': list(JOB_EDITABLE), 'derived': list(JOB_DERIVED),
         'date': ['Posted date', 'Deadline'], 'datetime': ['Added on'], 'integer': [],
         'links': ['Application link', 'Source link', 'Person LinkedIn'], 'row_height': 72,
         'validation': {'Stage': list(stages)}},
        {'name': 'Job Details', 'table': 'JobDetails', 'title': 'Job descriptions',
         'note': 'Filter by Job ID and read the numbered parts in order for the complete text.',
         'headers': list(DETAIL_HEADERS), 'widths': list(DETAIL_WIDTHS), 'key': ['Job ID', 'Field', 'Part'],
         'mode': 'merge', 'editable': [], 'derived': list(DETAIL_HEADERS), 'date': [], 'datetime': [],
         'integer': ['Part'], 'links': ['Source link'], 'row_height': None, 'validation': {}},
        {'name': 'Outreach', 'table': 'OutreachLog', 'title': 'Outreach',
         'note': 'Follow-up date, Next action and Notes are yours to edit. Sent does not mean delivered, and a '
                 'bounce does not close the job. ' + times,
         'headers': [h for h, _ in OUTREACH_COLUMNS], 'widths': [w for _, w in OUTREACH_COLUMNS],
         'key': ['Outreach ID'], 'mode': 'merge', 'editable': list(OUTREACH_EDITABLE),
         'derived': [h for h, _ in OUTREACH_COLUMNS if h not in OUTREACH_EDITABLE],
         'date': ['Follow-up date (editable)'],
         'datetime': ['Drafted on', 'Sent on', 'Latest reply on', 'Next follow-up', 'Checked on'],
         'integer': ['Follow-ups sent'], 'links': ['Sent link', 'Reply link'], 'row_height': 60, 'validation': {}},
        {'name': 'Activity', 'table': 'ActivityLog', 'title': 'Mail activity',
         'note': 'One row per verified message recorded by the tracker; rows are only ever added. ' + times,
         'headers': [h for h, _ in ACTIVITY_COLUMNS], 'widths': [w for _, w in ACTIVITY_COLUMNS],
         'key': ['Signal ID'], 'mode': 'append', 'editable': [], 'derived': [h for h, _ in ACTIVITY_COLUMNS],
         'date': [], 'datetime': ['Occurred on', 'Recorded on'], 'integer': [], 'links': ['Message link'],
         'row_height': 45, 'validation': {}},
    ]


def excel_format(fmt):
    """Excel number format for a strftime pattern such as %Y-%m-%d %H:%M."""
    if not isinstance(fmt, str) or not fmt:
        raise ValueError('Tracker date formats must be strftime patterns such as %Y-%m-%d')
    out, i = [], 0
    while i < len(fmt):
        char = fmt[i]
        if char == '%':
            code = fmt[i + 1:i + 2]
            if code == '%':
                out.append('\\%')
            elif code in _DIRECTIVES:
                out.append(_DIRECTIVES[code])
            else:
                raise ValueError('Unsupported tracker date directive %%%s (use %s)'
                                 % (code, ' '.join('%' + k for k in _DIRECTIVES)))
            i += 2
            continue
        out.append(char if char in ' -/:.,()' else '\\' + char)
        i += 1
    return ''.join(out)


# ------------------------------------------------------------------ cells
def clean_text(text):
    """Spreadsheet-safe text: newlines normalized and XML-illegal control characters removed."""
    return ILLEGAL_CHARACTERS_RE.sub('', text.replace('\r\n', '\n').replace('\r', '\n'))


def utf16_len(text):
    return len(text.encode('utf-16-le')) // 2


def normalize(value):
    """Comparable form of a cell from the projection or from the workbook."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return int(value) if value.is_integer() else float('%.12g' % value)
    if isinstance(value, str):
        text = clean_text(value)
        return text if text else None
    if isinstance(value, dict):
        for kind in ('date', 'datetime', 'formula'):
            if kind in value:
                return {kind: str(value[kind])}
    return str(value)


def cell_hash(value):
    data = json.dumps(normalize(value), sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(data.encode('utf-8')).hexdigest()[:16]


def text_of(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def compact(value):
    """A main-sheet value; long text is shortened here and kept in full in Job Details."""
    text = text_of(value)
    if text is not None and len(text) > COMPACT_AT:
        return text[:COMPACT_KEEP] + '\nFull value in Job Details.'
    return text if isinstance(value, (dict, list)) else value


def chunks(value, limit=CHUNK_CHARS, max_lines=CHUNK_LINES):
    """Split text into numbered parts of at most `limit` UTF-16 units and `max_lines` lines."""
    if value is None or value == '':
        return []
    parts, current, size, lines = [], [], 0, 1
    for character in str(value):
        width = 2 if ord(character) > 0xFFFF else 1
        if size + width > limit or lines >= max_lines:
            parts.append(''.join(current))
            current, size, lines = [], 0, 1
        current.append(character)
        size += width
        if character == '\n':
            lines += 1
    if current:
        parts.append(''.join(current))
    return parts


def _parse(value):
    """An aware datetime from an ISO timestamp, or None."""
    if not isinstance(value, str) or 'T' not in value:
        return None
    text = value.strip()
    if text[-1:] in ('Z', 'z'):
        text = text[:-1] + '+00:00'
    text = re.sub(r'(\.\d+)(?=[+-]\d{2}:\d{2}$|$)', lambda m: (m.group(1) + '000000')[:7], text)
    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def _seconds(moment):
    rounded = moment.replace(microsecond=0)
    return rounded + timedelta(seconds=1) if moment.microsecond >= 500000 else rounded


def local_datetime(value, zone):
    """{'datetime': local wall time} for an ISO timestamp; a bare date becomes {'date'}; other text stays."""
    moment = _parse(value)
    if moment is not None:
        return {'datetime': _seconds(moment.astimezone(zone)).replace(tzinfo=None).isoformat()}
    return local_date(value, zone)


def local_date(value, zone):
    """{'date': YYYY-MM-DD} for an ISO date or timestamp (in local time); other text stays text."""
    if value is None or value == '':
        return None
    moment = _parse(value)
    if moment is not None:
        return {'date': moment.astimezone(zone).date().isoformat()}
    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value.strip()):
        try:
            return {'date': date.fromisoformat(value.strip()).isoformat()}
        except ValueError:
            return value
    return value


def text_value(value):
    """A sheet value saved into a text field (numbers or dates typed into Excel become text)."""
    v = normalize(value)
    if isinstance(v, dict):
        if 'formula' in v:
            raise ValueError('formulas are not saved; type the text instead')
        return v.get('date') or v['datetime'].replace('T', ' ')
    return str(v) if isinstance(v, (int, float)) else v


def date_value(value):
    """A sheet value saved into a date field, as YYYY-MM-DD."""
    v = normalize(value)
    if v is None:
        return None
    if isinstance(v, dict):
        if 'formula' in v:
            raise ValueError('formulas are not saved; type a date instead')
        return v.get('date') or v['datetime'][:10]
    if isinstance(v, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', v.strip()):
        return v.strip()
    raise ValueError('type a date (YYYY-MM-DD)')


def _finish(spec, raw, sanitized):
    """Header-ordered projection cells; records where control characters had to be removed."""
    row = []
    for header in spec['headers']:
        value = raw.get(header)
        if isinstance(value, float) and not math.isfinite(value):
            value = None
        if isinstance(value, str):
            plain = value.replace('\r\n', '\n').replace('\r', '\n')
            value = clean_text(value)
            if value != plain:
                sanitized.append({'sheet': spec['name'], 'header': header,
                                  'key': KEY_JOIN.join(str(raw.get(k)) for k in spec['key'])})
            if utf16_len(value) > CELL_LIMIT:
                value = value[:CELL_LIMIT // 2] + '\n[Shortened: the full text is in the JSON store.]'
            value = value or None
        row.append(value)
    return row


def row_key(spec, values):
    """The row identity (a string) from header -> cell values; None when a key cell is blank."""
    parts = []
    for header in spec['key']:
        value = normalize(values.get(header))
        if value is None:
            return None
        parts.append(json.dumps(value, sort_keys=True) if isinstance(value, dict) else str(value))
    return KEY_JOIN.join(parts)


# ------------------------------------------------------------------ row builders
def source_info(job):
    sources = job.get('sources') or []
    return {'Source': '\n'.join(s['label'] for s in sources if s.get('label')) or None,
            'Source link': '\n'.join(dict.fromkeys(s['url'] for s in sources if s.get('url'))) or None,
            'Source access': ', '.join(dict.fromkeys(s['access'] for s in sources if s.get('access'))) or None}


def job_row(job, ctx):
    raw = {}
    for field, header, _ in JOB_COLUMNS:
        value = job.get(field)
        raw[header] = value if header in JOB_EDITABLE else compact(value)
    raw.update(source_info(job))
    raw['Review'] = 'Needs review' if job.get('review_status') == 'needs_review' else 'Captured'
    raw['Missing information'] = ', '.join(label for field, label in MISSING if not job.get(field)) or None
    raw['Added on'] = local_datetime(job.get('created_at'), ctx['zone'])
    for header, field in (('Posted date', 'posted_date'), ('Deadline', 'deadline')):
        raw[header] = local_date(compact(job.get(field)), ctx['zone'])
    return raw


def _submissions(job, zone):
    lines = []
    for item in job.get('history') or []:
        if item.get('action') == 'application-submitted':
            when = local_datetime(item.get('submitted_at'), zone)
            stamp = when['datetime'].replace('T', ' ')[:16] if isinstance(when, dict) and 'datetime' in when else when
            lines.append('%s via %s (%s, %s)' % (stamp, item.get('channel'), item.get('evidence', {}).get('kind'),
                                                 item.get('application_id')))
    return '\n'.join(lines) or None


def detail_rows(job, ctx):
    rows = []
    sources = job.get('sources') or [{}]
    primary = sources[0]

    def add(field, value, source):
        for index, part in enumerate(chunks(text_of(value)), 1):
            rows.append({'Job ID': job['job_id'], 'Company': job.get('company'), 'Role': job.get('role'),
                         'Field': field, 'Part': index, 'Text': part, 'Source': source.get('label'),
                         'Source link': source.get('url'), 'Source access': source.get('access')})

    for field, label in DETAIL_FIELDS:
        value = job.get(field)
        if value not in (None, '', [], {}):
            add(label, value, primary)
    for field, header, _ in JOB_COLUMNS:
        text = text_of(job.get(field))
        if header not in JOB_EDITABLE and text and len(text) > COMPACT_AT:
            add(header, job.get(field), primary)
    if job.get('review_reasons'):
        add('Review reasons', '\n'.join(job['review_reasons']), primary)
    submitted = _submissions(job, ctx['zone'])
    if submitted:
        add('Applications', submitted, primary)
    for index, source in enumerate(job.get('sources') or [], 1):
        suffix = '' if index == 1 else ' (source %d)' % index
        if source.get('text'):
            add('Captured source text' + suffix, source['text'], source)
        else:
            add('Source' + suffix, source.get('label'), source)
    return rows


def outreach_row(record, summary, ctx):
    draft, sent = record.get('draft') or {}, record.get('sent') or {}
    latest = summary.get('latest_reply')
    drafted = next((h['at'] for h in record.get('history', []) if h.get('action') == 'drafted'), None)
    return {
        'Outreach ID': record['outreach_id'], 'Company': record.get('company'), 'Recipient': record.get('recipient'),
        'Channel': record.get('channel'), 'Status': STATUS_LABELS.get(record['status'], record['status']),
        'Outreach type': TYPE_LABELS.get(record.get('outreach_type'), record.get('outreach_type')),
        'Roles': '\n'.join(record.get('roles') or []) or None,
        'Job IDs': '\n'.join(record.get('job_ids') or []) or None,
        'Application ID': record.get('application_id'), 'Subject': record.get('subject'),
        'Drafted on': local_datetime(drafted, ctx['zone']),
        'Sent on': local_datetime(sent.get('sent_at'), ctx['zone']),
        'CV file': ctx['cv_files'].get(record['outreach_id']),
        'Thread ID': sent.get('thread_id') or draft.get('thread_id'),
        'Message ID': sent.get('message_id') or draft.get('message_id'), 'Draft ID': draft.get('draft_id'),
        'Sent link': sent.get('url'), 'Bounce reason': compact(ctx['bounces'].get(record['outreach_id'])),
        'Reply status': SIGNAL_LABELS.get(latest['signal']) if latest else None,
        'Latest reply on': local_datetime(latest['occurred_at'], ctx['zone']) if latest else None,
        'Reply link': latest.get('url') if latest else None, 'Reply action': summary.get('reply_action'),
        'Follow-ups sent': summary.get('follow_ups_sent', 0),
        'Next follow-up': local_datetime(summary.get('next_follow_up'), ctx['zone']),
        'Follow-up date (editable)': local_date(record.get('follow_up_override'), ctx['zone']),
        'Next action (editable)': record.get('next_action'), 'Notes (editable)': record.get('notes'),
        'Checked on': local_datetime(ctx['checked_at'], ctx['zone']),
    }


def activity_row(signal, ctx):
    m = signal['message']
    record = ctx['records'].get(signal['outreach_id']) or {}
    return {'Signal ID': signal['signal_id'], 'Occurred on': local_datetime(m['occurred_at'], ctx['zone']),
            'Direction': 'Incoming' if m.get('direction') == 'incoming' else 'Outgoing',
            'Signal': SIGNAL_LABELS.get(m['signal'], m['signal']), 'Company': record.get('company'),
            'Outreach ID': signal['outreach_id'], 'Job IDs': '\n'.join(record.get('job_ids') or []) or None,
            'Application ID': signal.get('application_id'), 'From': m.get('sender'), 'Subject': m.get('subject'),
            'Quote': compact(m.get('quote')), 'Message link': m.get('url'), 'Action': m.get('action') or None,
            'Recorded on': local_datetime(signal.get('recorded_at'), ctx['zone'])}


def build(data, ctx):
    """The projection: every managed sheet's rows, built from the JSON stores only.

    data: jobs (registry rows), outreach (records), summary (outreach_id -> pipeline summary),
    signals (committed). ctx: zone (tzinfo), tz_label, checked_at, stages, formats, cv_files, bounces.
    """
    ctx = dict(ctx, records={r['outreach_id']: r for r in data['outreach']})
    specs = sheet_specs(ctx['stages'], ctx['tz_label'])
    sanitized = []
    builders = {
        'Jobs': [job_row(job, ctx) for job in data['jobs']],
        'Job Details': [row for job in data['jobs'] for row in detail_rows(job, ctx)],
        'Outreach': [outreach_row(r, data['summary'].get(r['outreach_id'], {}), ctx) for r in data['outreach']],
        'Activity': [activity_row(s, ctx) for s in sorted(data['signals'], key=lambda s: (
            s['message']['occurred_at'], s['signal_id']))],
    }
    for spec in specs:
        spec['rows'] = [_finish(spec, raw, sanitized) for raw in builders[spec['name']]]
        keys = [row_key(spec, dict(zip(spec['headers'], row))) for row in spec['rows']]
        if len(set(keys)) != len(keys) or None in keys:
            raise ValueError('%s rows need unique, non-blank keys' % spec['name'])
    return {'schema_version': SCHEMA_VERSION, 'generated_at': ctx['checked_at'], 'timezone': ctx['tz_label'],
            'formats': {'date': excel_format(ctx['formats']['date']),
                        'datetime': excel_format(ctx['formats']['datetime'])},
            'sheets': specs, 'sanitized': sanitized, 'keep': {}, 'expected_workbook_sha256': None}


# ------------------------------------------------------------------ the three-way merge
def _keep(decisions, sheet, key, header):
    decisions['keep'].setdefault(sheet, {}).setdefault(key, [])
    if header not in decisions['keep'][sheet][key]:
        decisions['keep'][sheet][key].append(header)


def merge(projection, snapshot, base):
    """Decide each kept cell (see the module docstring). Returns pulls, keeps, conflicts and notes.

    snapshot: {'sheets': {name: {'present': bool, 'rows': {key: {header: cell}}}}} from tracker_xlsx.py.
    base: the last export's {'cells': {name: {key: {header: hash}}}}, or None.
    """
    decisions = {'pull': [], 'keep': {}, 'conflicts': [], 'kept_edits': [], 'restored_rows': []}
    cells = (base or {}).get('cells', {})
    for spec in projection['sheets']:
        sheet = (snapshot or {}).get('sheets', {}).get(spec['name']) or {}
        if spec['mode'] != 'merge' or not sheet.get('present'):
            continue
        current = sheet.get('rows', {})
        compared = [h for h in spec['headers'] if h not in spec['derived']]
        for row in spec['rows']:
            values = dict(zip(spec['headers'], row))
            key = row_key(spec, values)
            known = cells.get(spec['name'], {}).get(key)
            if key not in current:
                if known is not None:
                    decisions['restored_rows'].append({'sheet': spec['name'], 'key': key})
                continue
            for header in compared:
                mine, theirs = values.get(header), current[key].get(header)
                json_hash, sheet_hash = cell_hash(mine), cell_hash(theirs)
                if sheet_hash == json_hash:
                    continue
                last = (known or {}).get(header)
                if last is not None and sheet_hash == last:
                    continue  # the user left it alone: the JSON value is written
                item = {'sheet': spec['name'], 'key': key, 'header': header,
                        'sheet_value': normalize(theirs), 'json_value': normalize(mine)}
                if last is not None and json_hash == last:
                    if header in spec['editable']:
                        decisions['pull'].append(item)
                    else:
                        decisions['kept_edits'].append(item)
                        _keep(decisions, spec['name'], key, header)
                    continue
                item['reason'] = ('changed in both the workbook and the records' if last is not None
                                  else 'no record of the last export for this cell')
                decisions['conflicts'].append(item)
                _keep(decisions, spec['name'], key, header)
    return decisions


def next_base(projection, base, keep, workbook_sha256, exported_at):
    """What this export leaves in the workbook, for the next merge (kept cells keep the old base)."""
    old = (base or {}).get('cells', {})
    cells = {}
    for spec in projection['sheets']:
        compared = [h for h in spec['headers'] if h not in spec['derived']]
        if spec['mode'] != 'merge' or not compared:
            continue
        out = {}
        for row in spec['rows']:
            values = dict(zip(spec['headers'], row))
            key = row_key(spec, values)
            kept = keep.get(spec['name'], {}).get(key, [])
            entry = {}
            for header in compared:
                if header in kept:
                    previous = old.get(spec['name'], {}).get(key, {}).get(header)
                    if previous is not None:
                        entry[header] = previous
                else:
                    entry[header] = cell_hash(values.get(header))
            out[key] = entry
        cells[spec['name']] = out
    return {'schema_version': SCHEMA_VERSION, 'workbook_sha256': workbook_sha256, 'exported_at': exported_at,
            'cells': cells}
