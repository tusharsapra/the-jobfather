"""Onboarding helpers: create a workspace, track steps, import sources, mark files ready.

The host assistant does the conversation (gap interview, voice, approvals);
this script does the file work. Every step can be re-run.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

if __name__ == '__main__':
    sys.modules['onboard'] = sys.modules[__name__]

import layout
import managed
from local import (CODE_ROOT, WorkspaceError, add_workspace_arg, atomic_json, atomic_text, contained, now,
                   platform, read_json, read_text, require_workspace, workspace, write_pointer)

TEMPLATES = CODE_ROOT / 'templates' / 'workspace'
STEPS = ('preflight', 'workspace', 'import', 'profile', 'interview', 'voice', 'market', 'cv', 'connect', 'dry-run')
STEP_STATES = ('pending', 'in-progress', 'done', 'skipped')


def _fill(text, workspace_id, created_at):
    return text.replace('__WORKSPACE_ID__', workspace_id).replace('__CREATED_AT__', created_at)


def init(path=None, set_pointer=True):
    """Create a workspace from templates. Never overwrites an existing file."""
    ws = workspace(path)
    if (ws / layout.CONFIG).exists():
        raise WorkspaceError('A workspace already exists at %s' % ws)
    if ws.exists() and any(p for p in ws.iterdir() if p.name != '.DS_Store'):
        raise WorkspaceError('Folder %s is not empty; choose an empty or new folder' % ws)
    workspace_id = 'ws-' + uuid.uuid4().hex[:16]
    created = now()
    for rel in layout.SKELETON:
        (ws / rel).mkdir(parents=True, exist_ok=True)
    for source in sorted(TEMPLATES.rglob('*')):
        if source.is_file() and source.name != '.DS_Store':
            rel = source.relative_to(TEMPLATES).as_posix()
            target = contained(ws, rel)
            atomic_text(target, _fill(read_text(source), workspace_id, created))
    if os.name != 'nt':
        os.chmod(ws, 0o700)
    atomic_json(ws / layout.ONBOARDING, {'schema_version': 1, 'phase': 'collecting', 'workspace_id': workspace_id,
                                         'created_at': created,
                                         'steps': {s: {'state': 'pending', 'updated_at': None, 'note': None}
                                                   for s in STEPS}})
    set_step(ws, 'workspace', 'done', 'Workspace created')
    pointer = None
    if set_pointer and not os.environ.get('JOBFATHER_TEST'):
        pointer = str(write_pointer(ws))
    return {'workspace': str(ws), 'workspace_id': workspace_id, 'pointer': pointer}


def state(ws):
    path = Path(ws) / layout.ONBOARDING
    return read_json(path) if path.is_file() else None


def set_step(ws, name, value, note=None):
    if name not in STEPS or value not in STEP_STATES:
        raise ValueError('Unknown step or state')
    path = Path(ws) / layout.ONBOARDING
    data = read_json(path)
    data['steps'][name] = {'state': value, 'updated_at': now(), 'note': note}
    atomic_json(path, data)
    return data


def next_step(ws):
    data = state(ws)
    if not data:
        return 'workspace'
    for name in STEPS:
        if data['steps'][name]['state'] in ('pending', 'in-progress'):
            return name
    return None


def mark_ready(ws, host, confirmation=''):
    """Validate every managed file and record their hashes for the INIT event."""
    platform(host)
    texts = managed.read_all(ws)
    missing = [p for p in (layout.IDENTITY, layout.EXPERIENCE, layout.PROFILE, layout.VOICE, layout.HR) if p not in texts]
    if missing:
        raise ValueError('Fill these before finishing onboarding: ' + ', '.join(missing))
    for path, text in texts.items():
        managed.validate_file(path, None, text)
    managed.validate_references(texts)
    data = state(ws)
    if data is None:
        raise WorkspaceError('No onboarding state; run init first')
    data.update({'phase': 'ready-to-init', 'files': managed.raw_revision(ws)['files'], 'ready_at': now(),
                 'platform': host, 'confirmation': (confirmation or '')[:4000]})
    atomic_json(Path(ws) / layout.ONBOARDING, data)
    return {'phase': data['phase'], 'files': len(texts)}


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    sub = parser.add_subparsers(dest='command', required=True)
    child = sub.add_parser('init')
    child.add_argument('--path', help='folder for the new workspace (default: ~/Jobfather)')
    child.add_argument('--no-pointer', action='store_true', help='do not remember this folder as the default')
    sub.add_parser('status')
    child = sub.add_parser('step')
    child.add_argument('name', choices=STEPS)
    child.add_argument('state', choices=STEP_STATES)
    child.add_argument('--note')
    child = sub.add_parser('mark-ready')
    child.add_argument('--platform', required=True)
    child.add_argument('--confirmation', default='')
    args = parser.parse_args()
    if args.command == 'init':
        result = init(args.path or args.workspace, set_pointer=not args.no_pointer)
    else:
        ws = require_workspace(args.workspace)
        if args.command == 'status':
            data = state(ws) or {}
            result = {'workspace': str(ws), 'phase': data.get('phase'), 'next_step': next_step(ws),
                      'steps': {k: v['state'] for k, v in data.get('steps', {}).items()}}
        elif args.command == 'step':
            result = set_step(ws, args.name, args.state, args.note)['steps'][args.name]
        else:
            result = mark_ready(ws, args.platform, args.confirmation)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit('Onboarding failed: ' + str(error))
