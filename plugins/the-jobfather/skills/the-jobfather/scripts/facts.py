"""Answer factual fields (forms, recruiter questions) or name the one unknown to ask about."""
import argparse
import json
import re

import evidence
import layout
from local import add_workspace_arg, read_json, require_workspace
from memory import read_context


def _slug(text):
    return re.sub(r'[^a-z0-9]+', '-', text.casefold()).strip('-')[:80]


def field(name, ws=None):
    ws = require_workspace(ws)
    if name in ('current_employer', 'current_job_title', 'full_name', 'email', 'phone', 'location'):
        content = read_context([layout.IDENTITY, layout.EXPERIENCE], ws)['content']
        identity = json.loads(content[layout.IDENTITY])
        if name in ('full_name',):
            value = identity.get('full_name') or None
        elif name in ('email', 'phone', 'location'):
            value = (identity.get('contacts') or {}).get(name) or None
        else:
            role = evidence.parse(content[layout.EXPERIENCE] or '').current_role()
            value = (role or {}).get('org' if name == 'current_employer' else 'title')
        result = {'field': name, 'known': value is not None, 'value': value,
                  'source': layout.IDENTITY if name in ('full_name', 'email', 'phone', 'location') else layout.EXPERIENCE}
    elif name.startswith('answer:'):
        answers = json.loads(read_context([layout.FORM_ANSWERS], ws)['content'][layout.FORM_ANSWERS])['answers']
        key = _slug(name[7:])
        item = answers.get(key)
        result = {'field': name, 'known': item is not None, 'value': item['answer'] if item else None,
                  'details': item, 'source': layout.FORM_ANSWERS}
    else:
        hr = json.loads(read_context([layout.HR], ws)['content'][layout.HR])
        key = name[7:] if name.startswith('custom.') else name
        source = hr.get('custom', {}) if name.startswith('custom.') else hr
        if key not in source or not isinstance(source[key], dict) or 'value' not in source[key]:
            raise ValueError('Unknown field name: ' + name)
        value = source[key]['value']
        result = {'field': name, 'known': value is not None, 'value': value, 'details': source[key], 'source': layout.HR}
    if not result['known']:
        result['needs_confirmation'] = name
    return result


if __name__ == '__main__':
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__))
    parser.add_argument('field', help='e.g. notice_period, current_job_title, custom.night_shifts, "answer:How did you hear about us?"')
    args = parser.parse_args()
    try:
        print(json.dumps(field(args.field, args.workspace), ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit(str(error))
