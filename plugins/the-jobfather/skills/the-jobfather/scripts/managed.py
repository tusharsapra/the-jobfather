"""Managed workspace files: owner, dependencies, validation, and dependency groups.

Only these files change through memory.py transactions; everything else in the
workspace is ordinary state. Revisions hash these files only.
"""
import hashlib
import json
from pathlib import Path
import re

import evidence
import layout
from local import digest, read_json, read_text, atomic_json

MANAGED = {
    layout.IDENTITY: {'type': 'json', 'policy': 'closed', 'owner': 'identity', 'depends_on': []},
    layout.EXPERIENCE: {'type': 'md', 'owner': 'career_facts', 'depends_on': []},
    layout.HR: {'type': 'json', 'policy': 'closed', 'owner': 'hr', 'depends_on': []},
    layout.PROFILE: {'type': 'md', 'owner': 'summaries_and_targets',
                     'depends_on': [layout.EXPERIENCE, layout.HR, layout.IDENTITY]},
    layout.STORY_BANK: {'type': 'md', 'owner': 'examples', 'depends_on': [layout.EXPERIENCE]},
    layout.VOICE: {'type': 'md', 'owner': 'voice', 'depends_on': []},
    layout.FORM_ANSWERS: {'type': 'json', 'policy': 'open', 'owner': 'form_answers', 'depends_on': [layout.HR]},
    layout.MARKET_JSON: {'type': 'json', 'policy': 'open', 'owner': 'market', 'depends_on': []},
    layout.MARKET_MD: {'type': 'md', 'owner': 'market_notes', 'depends_on': [layout.MARKET_JSON], 'rewritable': True},
    layout.LEARNED: {'type': 'md', 'owner': 'lessons', 'depends_on': [], 'rewritable': True},
    layout.POLICY: {'type': 'json', 'policy': 'closed', 'owner': 'policy', 'depends_on': []},
    layout.CV_MASTER: {'type': 'generated', 'owner': 'cv_master', 'depends_on': [layout.IDENTITY, layout.EXPERIENCE]},
    layout.CV_MANIFEST: {'type': 'generated', 'owner': 'cv_master', 'depends_on': [layout.IDENTITY, layout.EXPERIENCE]},
}

_FIT = [layout.IDENTITY, layout.PROFILE, layout.EXPERIENCE, layout.HR]
GROUPS = {
    'fit': _FIT,
    'triage': _FIT + [layout.MARKET_JSON],
    'writing': _FIT + [layout.VOICE, layout.LEARNED],
    'forms': _FIT + [layout.FORM_ANSWERS, layout.VOICE],
    'cv': _FIT + [layout.CV_MASTER, layout.CV_MANIFEST],
    'all': list(MANAGED),
}

ALIASES = {Path(p).name: p for p in MANAGED}
ALIASES.update({'MARKET.md': layout.MARKET_MD, 'market.json': layout.MARKET_JSON})

POLICY_MODES = ('drafts-only', 'send-with-approval', 'submit-with-approval')
SENSITIVE_HR = {'current_compensation', 'expected_compensation', 'minimum_compensation', 'notice_period',
                'last_working_date', 'resignation_status', 'current_address', 'date_of_birth'}
REVISION_INDEX_LIMIT = 64


def resolve(name):
    """Accept 'PROFILE.md' or 'profile/PROFILE.md'."""
    path = ALIASES.get(name, name)
    if path not in MANAGED:
        raise ValueError('Not a managed workspace file: ' + name)
    return path


def raw_revision(ws):
    ws = Path(ws)
    files = {p: (digest(ws / p) if (ws / p).is_file() else 'missing') for p in sorted(MANAGED)}
    revision = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {'revision': revision, 'files': files}


def remember_revision(ws, revision):
    path = Path(ws) / layout.REVISION_INDEX
    entries = read_json(path) if path.is_file() else []
    if not entries or entries[-1]['revision'] != revision['revision']:
        entries = [e for e in entries if e['revision'] != revision['revision']]
        entries.append(revision)
        atomic_json(path, entries[-REVISION_INDEX_LIMIT:])


def files_for(ws, revision_id):
    path = Path(ws) / layout.REVISION_INDEX
    if path.is_file():
        for entry in read_json(path):
            if entry['revision'] == revision_id:
                return entry['files']
    return None


def group_changes(ws, revision_id, group):
    """Paths in `group` that changed since `revision_id` (all of them if the revision is unknown)."""
    current = raw_revision(ws)
    if revision_id == current['revision']:
        return []
    old = files_for(ws, revision_id)
    paths = GROUPS[group]
    if old is None:
        return list(paths)
    return [p for p in paths if old.get(p) != current['files'].get(p)]


def assert_fresh(ws, revision_id, group, what='This work'):
    changed = group_changes(ws, revision_id, group)
    if changed:
        raise ValueError('%s used an older version of %s; reread and redo it' % (what, ', '.join(changed)))


# ------------------------------------------------------------------ validation
def _markdown(path, text):
    if not text.startswith('# ') or re.search(r'(?im)^\s*(TODO|TBD)\b', text):
        raise ValueError('%s needs a "# " title and no TODO/TBD lines' % path)


def _json(path, text):
    try:
        value = json.loads(text)
    except ValueError as error:
        raise ValueError('%s is not valid JSON: %s' % (path, error))
    if not isinstance(value, dict):
        raise ValueError('%s must hold a JSON object' % path)
    return value


def validate_identity(value):
    if value.get('schema_version') != 1:
        raise ValueError('identity.json needs schema_version 1')
    if not isinstance(value.get('full_name'), str) or not value['full_name'].strip():
        raise ValueError('identity.json needs full_name')
    contacts = value.get('contacts', {})
    if not isinstance(contacts, dict) or not isinstance(contacts.get('links', []), list):
        raise ValueError('identity.json contacts must be an object with a links list')
    locale = value.get('locale', {})
    if locale.get('paper') not in (None, 'A4', 'Letter'):
        raise ValueError('identity.locale.paper must be A4 or Letter')


def validate_hr(old, new):
    for key in ('schema_version', 'owner'):
        if old is not None and old.get(key) != new.get(key):
            raise ValueError('HR-DETAILS %s cannot change' % key)
    if old is not None:
        if set(old) - set(new):
            raise ValueError('HR-DETAILS fields cannot be removed')
        for key, value in old.items():
            if isinstance(value, dict) and key != 'custom':
                if not isinstance(new.get(key), dict) or set(value) != set(new[key]):
                    raise ValueError('HR-DETAILS field %s must keep its shape' % key)
        extra = set(new) - set(old)
        if extra:
            raise ValueError('New HR facts go under "custom" (json_add): ' + ', '.join(sorted(extra)))
    for key, value in new.get('custom', {}).items():
        if not re.fullmatch(r'[a-z][a-z0-9_]{1,63}', key):
            raise ValueError('Custom HR fact names are lower_snake_case: ' + key)
        if not isinstance(value, dict) or not {'value', 'confirmed_at', 'description'} <= set(value):
            raise ValueError('Custom HR fact %s needs value, confirmed_at and description' % key)


def validate_form_answers(value):
    answers = value.get('answers')
    if value.get('schema_version') != 1 or not isinstance(answers, dict):
        raise ValueError('FORM-ANSWERS.json needs schema_version 1 and an answers object')
    for key, item in answers.items():
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{1,79}', key):
            raise ValueError('Form answer keys are normalized slugs: ' + key)
        if not isinstance(item, dict) or not {'question', 'answer', 'confirmed_at'} <= set(item):
            raise ValueError('Form answer %s needs question, answer and confirmed_at' % key)


def validate_policy(value):
    if value.get('schema_version') != 1 or value.get('mode') not in POLICY_MODES:
        raise ValueError('policy.json mode must be one of: ' + ', '.join(POLICY_MODES))


def validate_file(path, old_text, new_text, experience_old=None, rollback=False):
    """Validate one managed file's new content (old_text None for a new file)."""
    spec = MANAGED[path]
    if not isinstance(new_text, str) or not new_text.strip() or len(new_text) > 400000 or '\x00' in new_text:
        raise ValueError('Invalid or oversized content for ' + path)
    if spec['type'] == 'md':
        _markdown(path, new_text)
        if path == layout.EXPERIENCE:
            new_cat = evidence.parse(new_text)
            if old_text is not None and not rollback:
                old_cat = evidence.parse(old_text)
                if not set(old_cat.prefixes) <= set(new_cat.prefixes):
                    raise ValueError('Evidence prefixes cannot be removed; retire claims instead')
                if not old_cat.defined <= new_cat.defined:
                    raise ValueError('Evidence IDs cannot be deleted; mark them "(retired ...)" instead')
    elif spec['type'] == 'json':
        new = _json(path, new_text)
        old = _json(path, old_text) if old_text is not None else None
        if path == layout.IDENTITY:
            validate_identity(new)
        elif path == layout.HR:
            validate_hr(old, new)
        elif path == layout.FORM_ANSWERS:
            validate_form_answers(new)
        elif path == layout.POLICY:
            validate_policy(new)
        elif path == layout.MARKET_JSON:
            import market
            market.validate(new)


def validate_references(texts):
    """Every evidence reference in managed Markdown must resolve to EXPERIENCE."""
    cat = evidence.parse(texts[layout.EXPERIENCE]) if texts.get(layout.EXPERIENCE) else evidence.Catalog()
    for path, text in texts.items():
        if path != layout.EXPERIENCE and MANAGED[path]['type'] == 'md' and text:
            missing = evidence.references(text, cat) - cat.defined
            if missing:
                raise ValueError('%s refers to unknown evidence: %s' % (path, ', '.join(sorted(missing))))


def read_all(ws):
    ws = Path(ws)
    return {p: read_text(ws / p) for p in MANAGED if (ws / p).is_file()}
