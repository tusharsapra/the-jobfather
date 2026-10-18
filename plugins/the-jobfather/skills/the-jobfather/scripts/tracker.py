"""Refresh jobs/outputs/Tracker.xlsx from the JSON stores, keeping the user's edits (stdlib facade).

Under the pipeline and jobs locks: replay interrupted mail signals, build the projection
(.state/tracker/projection.json), read the workbook's current values, merge them three ways against
the last export (.state/tracker/export-base.json), save the user's edits to Stage, Notes and the
editable outreach columns back into the JSON stores, then write the workbook. Excel work runs in
tracker_xlsx.py: through `uv run --script` when uv is installed, otherwise with this Python when it
has openpyxl.

    tracker.py export [--platform HOST] [--test]
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

# CLI execution and imports from job_intake.py/job_pipeline.py must share one module.
if __name__ == '__main__':
    sys.modules['tracker'] = sys.modules[__name__]

import job_intake
from job_intake import JobStore, parse_time
from job_pipeline import Pipeline
import layout
from local import (PLATFORMS, add_workspace_arg, atomic_json, config, contained, digest, locked, now, platform,
                   read_json, require_workspace, tz)
from outreach import OutreachStore
import tracker_model as model

WRITER = Path(__file__).resolve().with_name('tracker_xlsx.py')
TIMEOUT = 600          # one read or write of the workbook
WARM_TIMEOUT = 900     # uv may download openpyxl the first time
INSTALL_HINT = ('install uv (https://docs.astral.sh/uv/) so the tracker can fetch openpyxl by itself, or run '
                '"%s -m pip install --user \'openpyxl>=3.1,<4\'"')


def writer_command():
    """How to run tracker_xlsx.py: uv when available, else this Python if openpyxl imports, else None."""
    uv = shutil.which('uv')
    if uv:
        return [uv, 'run', '--quiet', '--script', str(WRITER)]
    try:
        import openpyxl  # noqa: F401
    except Exception:
        return None
    return [sys.executable, str(WRITER)]


def _run(command, args, timeout=TIMEOUT):
    try:
        result = subprocess.run(command + args, capture_output=True, encoding='utf-8', errors='replace',
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError('Could not run the tracker workbook writer: %s' % error)
    if result.returncode:
        detail = ' '.join((result.stderr or result.stdout or 'no output').strip().splitlines()[-6:])
        raise RuntimeError('Tracker workbook %s stopped: %s' % (args[0], detail))
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    try:
        return json.loads(lines[-1])
    except (IndexError, ValueError):
        raise RuntimeError('The tracker workbook writer did not return a JSON receipt')


def _paths(ws, test):
    state = layout.sandboxed(layout.TRACKER_STATE, test)
    return {'workbook': contained(ws, layout.sandboxed(layout.TRACKER, test)),
            'projection': contained(ws, state + '/projection.json'),
            'snapshot': contained(ws, state + '/snapshot.json'),
            'base': contained(ws, state + '/export-base.json')}


def _tz_label(ws):
    zone, name, exact = tz(ws)
    if exact:
        return zone, 'in ' + name
    if name == 'UTC':
        return zone, 'in UTC'
    return zone, 'in UTC%s (a fixed offset: time-zone data is missing on this computer)' % name


def _project(ws, pipeline, jobs, outreach, checked_at, notes):
    """The projection of the registry, outreach records and pipeline signals (nothing from the workbook)."""
    registry = jobs.show()
    records = outreach.load()['outreach']
    pipe = pipeline.projection(at=checked_at)
    cv_files = {}
    for record in records:
        draft = record.get('draft') or {}
        if not draft.get('receipt'):
            continue
        try:
            path = contained(ws, draft['receipt'])
            if digest(path) != draft.get('receipt_sha256'):
                raise ValueError('the receipt changed after it was recorded')
            names = [a.get('filename') for a in read_json(path).get('attachments') or [] if a.get('filename')]
            cv_files[record['outreach_id']] = '\n'.join(names) or None
        except (OSError, ValueError) as error:
            notes.append('CV file for %s is unknown: %s' % (record['outreach_id'], error))
    bounces = {}
    for signal in sorted(pipe['signals'], key=lambda s: parse_time(s['message']['occurred_at'])):
        if signal['message']['signal'] == 'bounce':
            bounces[signal['outreach_id']] = signal['message']['quote']
    zone, label = _tz_label(ws)
    settings = config(ws).get('tracker', {})
    ctx = {'zone': zone, 'tz_label': label, 'checked_at': checked_at, 'stages': job_intake.stages(ws),
           'formats': {'date': settings.get('date_format') or '%Y-%m-%d',
                       'datetime': settings.get('datetime_format') or '%Y-%m-%d %H:%M'},
           'cv_files': cv_files, 'bounces': bounces}
    data = {'jobs': registry['jobs'], 'outreach': records, 'signals': pipe['signals'],
            'summary': {r['outreach_id']: r for r in pipe['outreach']}}
    return model.build(data, ctx)


def _pull(host, decisions, jobs, outreach):
    """Save the user's edits to editable cells; an edit that cannot be saved stays in the sheet."""
    applied, rejected = [], []
    for item in decisions['pull']:
        sheet, key, header, value = item['sheet'], item['key'], item['header'], item['sheet_value']
        try:
            if sheet == 'Jobs' and header == 'Stage':
                jobs.set_stage(key, model.text_value(value) or '', host, basis='tracker-edit')
            elif sheet == 'Jobs' and header == 'Notes':
                jobs.set_notes(key, model.text_value(value), host, basis='tracker-edit')
            elif sheet == 'Outreach' and header == 'Follow-up date (editable)':
                outreach.set(key, host, basis='tracker-edit', follow_up_override=model.date_value(value))
            elif sheet == 'Outreach' and header == 'Next action (editable)':
                outreach.set(key, host, basis='tracker-edit', next_action=model.text_value(value))
            elif sheet == 'Outreach' and header == 'Notes (editable)':
                outreach.set(key, host, basis='tracker-edit', notes=model.text_value(value))
            else:
                raise ValueError('this column is not saved back')
            applied.append(item)
        except ValueError as error:
            rejected.append(dict(item, reason=str(error)))
            model._keep(decisions, sheet, key, header)
    return applied, rejected


def export(ws=None, host='unknown', test=False):
    """Refresh the tracker workbook; returns the receipt (edits saved, conflicts, rows added)."""
    platform(host)
    ws = require_workspace(ws)
    files = _paths(ws, test)
    command = writer_command()
    if command and command[0] != sys.executable:
        _run(command, ['check'], timeout=WARM_TIMEOUT)  # fetch dependencies before taking any lock
    pipeline = Pipeline(ws, test)
    jobs = JobStore(ws, test)
    outreach = OutreachStore(ws, test)
    notes = []
    # Lock order: pipeline, then applications (signal replay), then the jobs folder.
    with locked(pipeline.base):
        pipeline.recover()
        with locked(jobs.base):
            checked_at = now()
            projection = _project(ws, pipeline, jobs, outreach, checked_at, notes)
            atomic_json(files['projection'], projection)
            if command is None:
                saved = layout.sandboxed(layout.TRACKER_STATE, test) + '/projection.json'
                raise RuntimeError('Saved the tracker data to %s (your records are safe), but writing Tracker.xlsx '
                                   'needs openpyxl: %s' % (saved, INSTALL_HINT % sys.executable))
            base = read_json(files['base']) if files['base'].is_file() else None
            decisions = {'pull': [], 'keep': {}, 'conflicts': [], 'kept_edits': [], 'restored_rows': []}
            applied, rejected, expected = [], [], None
            workbook = files['workbook']
            if workbook.exists():
                _run(command, ['read', '--workbook', str(workbook), '--projection', str(files['projection']),
                               '--out', str(files['snapshot'])])
                snapshot = read_json(files['snapshot'])
                expected = snapshot['workbook_sha256']
                if base is None:
                    notes.append('No record of the last export was found, so cells that differ from the records '
                                 'were kept as they are in the workbook and listed as conflicts.')
                decisions = model.merge(projection, snapshot, base)
                applied, rejected = _pull(host, decisions, jobs, outreach)
                if applied:
                    projection = _project(ws, pipeline, jobs, outreach, checked_at, [])
            else:
                workbook.parent.mkdir(parents=True, exist_ok=True)
            projection['keep'] = decisions['keep']
            projection['expected_workbook_sha256'] = expected
            atomic_json(files['projection'], projection)
            receipt = _run(command, ['write', '--workbook', str(workbook), '--projection', str(files['projection'])])
            if not workbook.is_file() or digest(workbook) != receipt['sha256']:
                raise RuntimeError('Tracker.xlsx changed right after the export; run the export again')
            atomic_json(files['base'], model.next_base(projection, base if expected else None, decisions['keep'],
                                                       receipt['sha256'], checked_at))
    sanitized = projection['sanitized'] + [c for c in receipt['sanitized_cells'] if c not in projection['sanitized']]
    return {'workbook': str(workbook), 'created': receipt['created'], 'backup': receipt['backup'],
            'sha256': receipt['sha256'], 'sheets': receipt['sheets'],
            'pulled_edits': [{k: item[k] for k in ('sheet', 'key', 'header', 'sheet_value')} for item in applied],
            'conflicts': decisions['conflicts'], 'rejected_edits': rejected, 'kept_edits': decisions['kept_edits'],
            'restored_rows': decisions['restored_rows'], 'sanitized_cells': sanitized,
            'notes': notes + receipt['notes'],
            'projection': layout.sandboxed(layout.TRACKER_STATE, test) + '/projection.json'}


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    parser.add_argument('--test', action='store_true', help='use fictional sandbox storage')
    sub = parser.add_subparsers(dest='command', required=True)
    child = sub.add_parser('export', help='refresh jobs/outputs/Tracker.xlsx')
    child.add_argument('--workspace', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    child.add_argument('--test', action='store_true', default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    child.add_argument('--platform', choices=PLATFORMS, default='unknown')
    args = parser.parse_args()
    print(json.dumps(export(args.workspace, args.platform, args.test), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        raise SystemExit('Tracker export failed: ' + str(error))
