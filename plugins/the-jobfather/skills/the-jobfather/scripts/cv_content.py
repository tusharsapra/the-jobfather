"""CV content contract: the editable slots of the user's master CV and their checks.

A content patch maps each slot id in cv/cv-manifest.json to plain text:
paragraph -> string, bullets -> list of exactly `count` strings, list -> list of strings.
Source assembly and PDF validation live in cv_template.py and cv_pdf.py.
"""
import argparse
import json
from pathlib import Path
import re

import evidence
from local import add_workspace_arg, read_text, require_workspace


def schema(ws):
    from cv_template import template
    data = template(ws)
    return {'slots': data['manifest']['slots'],
            'entries': [e for e in data['manifest']['entries'] if e.get('editable')],
            'note': 'Tailor primary entries most, secondary lightly; keep facts within EXPERIENCE claims.'}


def baseline(ws):
    from cv_template import template
    return template(ws)['baseline_patch']


def validate(patch, ws=None, data=None):
    from cv_template import template, unsupported_chars
    errors = []
    try:
        data = data or template(ws)
    except ValueError as error:
        return {'ok': False, 'errors': [str(error)]}
    manifest = data['manifest']
    slots = manifest['slots']
    if not isinstance(patch, dict) or set(patch) != set(slots):
        return {'ok': False, 'errors': ['Patch fields must be exactly: ' + ', '.join(sorted(slots))]}
    values = []
    for ident, policy in slots.items():
        item = patch[ident]
        if policy['kind'] == 'paragraph':
            if not isinstance(item, str):
                errors.append(ident + ' must be text')
            else:
                values.append(item)
        elif policy['kind'] == 'bullets':
            if not isinstance(item, list) or len(item) != policy['count']:
                errors.append('%s must keep exactly %d bullets' % (ident, policy['count']))
            else:
                values.extend(item)
        else:
            if not isinstance(item, list) or len(item) < policy.get('min_items', 1):
                errors.append('%s needs at least %d items' % (ident, policy.get('min_items', 1)))
            else:
                values.extend(item)
    if any(not isinstance(x, str) or not x.strip() for x in values):
        errors.append('All content must be non-empty plain text')
    else:
        cat = evidence.load(ws) if ws is not None else evidence.Catalog()
        for text in values:
            if re.search(r'\\[A-Za-z]+|\*\*|\b(?:TODO|TBD)\b', text):
                errors.append('TeX commands, ** markup and placeholders are not content: ' + text[:60])
            if evidence.leaks(text, cat):
                errors.append('Evidence IDs are not content: ' + text[:60])
            bad = unsupported_chars(text)
            if bad:
                errors.append('Characters the classic template cannot typeset: ' + ' '.join(bad))
        known_tools = {t.casefold() for t in data['baseline_patch'].get('tools', [])} | evidence.vocabulary(cat)
        for ident, policy in slots.items():
            if policy['kind'] == 'list' and policy.get('vocabulary') == 'tools' and isinstance(patch[ident], list):
                unknown = [t for t in patch[ident] if t.casefold() not in known_tools]
                if unknown:
                    errors.append('Not in your tools list (confirm and add to EXPERIENCE first): ' + ', '.join(unknown))
    return {'ok': not errors, 'errors': errors, 'pdf_verified': False,
            'note': 'Content checks only. The host reviews facts and scope; cv_pdf.py assembles and verifies PDFs.'}


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    parser.add_argument('command', choices=('baseline', 'check', 'schema'))
    parser.add_argument('file', type=Path, nargs='?')
    args = parser.parse_args()
    ws = require_workspace(args.workspace)
    if args.command == 'baseline':
        result = baseline(ws)
    elif args.command == 'schema':
        result = schema(ws)
    else:
        if args.file is None:
            parser.error('check needs a JSON content patch')
        result = validate(json.loads(read_text(args.file)), ws)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get('ok') is False else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        raise SystemExit('CV content operation failed: ' + str(error))
