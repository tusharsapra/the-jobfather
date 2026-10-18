"""Where is the workspace and what needs attention first? (cheap; safe before onboarding)"""
import argparse
import json
from pathlib import Path

import layout
from local import WorkspaceError, add_workspace_arg, read_json, read_text, workspace


def status(explicit=None):
    try:
        ws = workspace(explicit)
    except WorkspaceError as error:
        return {'workspace': None, 'state': 'error', 'message': str(error)}
    if not (ws / layout.CONFIG).is_file():
        return {'workspace': str(ws), 'state': 'missing', 'next': 'onboarding'}
    onboarding = read_json(ws / layout.ONBOARDING) if (ws / layout.ONBOARDING).is_file() else {}
    steps = onboarding.get('steps', {})
    pending = [name for name, item in steps.items() if item.get('state') in ('pending', 'in-progress')]
    policy = read_json(ws / layout.POLICY).get('mode') if (ws / layout.POLICY).is_file() else 'drafts-only'
    inbox = ws / layout.INBOX
    waiting = 0
    if inbox.is_file():
        for line in read_text(inbox).splitlines():
            if line.strip() and json.loads(line).get('status') == 'pending':
                waiting += 1
    connections = ws / layout.CONNECTIONS
    resume = read_json(connections).get('resume') if connections.is_file() else None
    return {'workspace': str(ws), 'state': 'ready' if onboarding.get('phase') == 'complete' else 'onboarding',
            'onboarding_next': pending[0] if pending else None, 'policy_mode': policy,
            'learning_pending': waiting, 'connect_resume': resume,
            'history': (ws / layout.CHANGES).is_file()}


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('where')
    sub.add_parser('status').add_argument('--brief', action='store_true')
    args = parser.parse_args()
    result = status(args.workspace)
    if args.command == 'where':
        print(result['workspace'] or result.get('message'))
        return
    if args.brief:
        line = 'workspace=%s state=%s' % (result['workspace'], result['state'])
        for key in ('onboarding_next', 'policy_mode', 'learning_pending'):
            if result.get(key) not in (None, 0):
                line += ' %s=%s' % (key, result[key])
        if result.get('connect_resume'):
            line += ' connect_resume=yes'
        print(line)
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
