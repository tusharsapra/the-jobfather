"""CV content contract: the editable slots of the user's master CV and their checks.

A content patch maps each slot id in cv/cv-manifest.json to plain text:
paragraph -> string, bullets -> list of exactly `count` strings, list -> list of strings.
Source assembly and PDF validation live in cv_template.py and cv_pdf.py.

    cv_content.py schema | baseline | check PATCH.json
    cv_content.py variants                                   saved role-family variants and their state
    cv_content.py variant --family ID                        the family's starting patch (or the baseline)
    cv_content.py save-variant --family ID PATCH.json [--notes TEXT] [--replace]

A variant is a checked content patch saved as the starting point for one role family's jobs
(cv/variants/<family>.json). It never changes the design; when the master CV no longer fits it,
the baseline is returned instead.
"""
import argparse
import json
from pathlib import Path
import re

import evidence
import layout
from local import add_workspace_arg, atomic_json, contained, digest, now, read_json, read_text, require_workspace

VARIANT_SCHEMA = 'the-jobfather/cv-variant@1'
FAMILY_ID = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')


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


# ------------------------------------------------------------------ role-family variants
def _variant_path(ws, family):
    if not isinstance(family, str) or not FAMILY_ID.match(family):
        raise ValueError('Role family ids are lowercase letters, digits, hyphens and underscores')
    return contained(ws, '%s/%s.json' % (layout.CV_VARIANTS, family))


def _master_sha(ws):
    return digest(Path(ws) / layout.CV_MASTER)


def save_variant(ws, family, patch, notes=None, replace=False):
    """Save a checked content patch as the starting point for one role family in market.json."""
    import market
    ws = require_workspace(ws)
    path = _variant_path(ws, family)
    known = sorted(f['id'] for f in (market.load(ws) or {}).get('role_families', []))
    if family not in known:
        raise ValueError('Unknown role family %r; market.json has: %s' % (family, ', '.join(known) or 'none yet'))
    if notes is not None and (not isinstance(notes, str) or len(notes) > 1000):
        raise ValueError('Notes are text up to 1000 characters')
    result = validate(patch, ws)
    if not result['ok']:
        raise ValueError('The variant fails the content checks: ' + '; '.join(result['errors']))
    existed = path.exists()
    if existed and not replace:
        raise ValueError('A %s variant exists; pass --replace to overwrite it' % family)
    atomic_json(path, {'schema': VARIANT_SCHEMA, 'family': family, 'patch': patch, 'notes': notes,
                       'saved_at': now(), 'master_sha256': _master_sha(ws)})
    return {'family': family, 'path': '%s/%s.json' % (layout.CV_VARIANTS, family), 'replaced': existed}


def _state(ws, record):
    if not isinstance(record, dict) or record.get('schema') != VARIANT_SCHEMA:
        return 'stale', ['Not a saved CV variant']
    check = validate(record.get('patch'), ws)
    if not check['ok']:
        return 'stale', check['errors']
    return ('master_changed' if record.get('master_sha256') != _master_sha(ws) else 'ok'), []


def variant(ws, family):
    """The family's saved patch while it still fits the master CV; otherwise the baseline."""
    ws = require_workspace(ws)
    path = _variant_path(ws, family)
    if not path.is_file():
        return {'family': family, 'source': 'baseline', 'patch': baseline(ws),
                'note': 'No saved variant for this family: start from the baseline.'}
    try:
        record = read_json(path)
    except ValueError:
        record = None
    state, errors = _state(ws, record)
    if state == 'stale':
        return {'family': family, 'source': 'baseline', 'stale': True, 'errors': errors, 'patch': baseline(ws),
                'note': 'The saved variant no longer fits the master CV (slots, bullet counts or tools changed): '
                        'start from the baseline and save a new variant.'}
    result = {'family': family, 'source': 'variant', 'patch': record['patch'], 'saved_at': record.get('saved_at'),
              'notes': record.get('notes'), 'master_changed': state == 'master_changed'}
    if state == 'master_changed':
        result['note'] = ('The master CV changed after this variant was saved: compare it with the baseline and '
                          'keep only wording the current facts support.')
    return result


def variants(ws):
    ws = require_workspace(ws)
    folder = Path(ws) / layout.CV_VARIANTS
    out = []
    for path in sorted(folder.glob('*.json')) if folder.is_dir() else []:
        try:
            record = read_json(path)
        except ValueError:
            record = None
        state, _ = _state(ws, record)
        out.append({'family': path.stem, 'state': state,
                    'saved_at': record.get('saved_at') if isinstance(record, dict) else None})
    return {'variants': out}


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    parser.add_argument('command', choices=('baseline', 'check', 'schema', 'variants', 'variant', 'save-variant'))
    parser.add_argument('file', type=Path, nargs='?')
    parser.add_argument('--family', help='role family id from market.json (variant, save-variant)')
    parser.add_argument('--notes', help='what this variant emphasises (save-variant)')
    parser.add_argument('--replace', action='store_true', help='overwrite a saved variant (save-variant)')
    args = parser.parse_intermixed_args()  # the patch file may follow --family
    ws = require_workspace(args.workspace)
    if args.command in ('variant', 'save-variant') and not args.family:
        parser.error(args.command + ' needs --family')
    if args.command == 'baseline':
        result = baseline(ws)
    elif args.command == 'schema':
        result = schema(ws)
    elif args.command == 'variants':
        result = variants(ws)
    elif args.command == 'variant':
        result = variant(ws, args.family)
    elif args.command == 'save-variant':
        if args.file is None:
            parser.error('save-variant needs a JSON content patch')
        result = save_variant(ws, args.family, json.loads(read_text(args.file)), args.notes, args.replace)
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
