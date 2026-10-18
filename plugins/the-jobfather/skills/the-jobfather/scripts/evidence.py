"""EXPERIENCE.md convention and evidence-ID helpers (stdlib only).

Convention (see templates/workspace/profile/EXPERIENCE.md):

    ## [LKM] Lakeshore Metrics          <- declares ID prefix LKM for this entry
    ```jobfather-entry
    kind: role                           # role | group | education | project | certification
    org: Lakeshore Metrics
    title: Senior Data Analyst
    location: Toronto, ON
    start: 2023-04                       # YYYY-MM | YYYY
    end: present                         # YYYY-MM | YYYY | present
    cv: tailor                           # tailor | locked | omit
    cv_weight: primary                   # primary | secondary | light (tailor only)
    ```
    ### Claims
    | ID | Claim | Scope / metric definition |
    | --- | --- | --- |
    | LKM-01 | ... | ... |
    ### Historical detail
    - **LKM-H01:** ...
    ### CV bullets
    1. Built a **churn model** that cut churn **18%**. [LKM-01]

Special sections: `## CV summary` (paragraph), `## CV skills` (```jobfather-cv-lists),
`## Tools and platforms` (```jobfather-vocabulary tools).
"""
from collections import OrderedDict
import re

RESERVED = {'APP', 'JOB', 'SRC', 'TX', 'EV', 'SIG', 'MAIL', 'OUT', 'IN', 'EM', 'INIT', 'REQ', 'HR', 'CV', 'EXT', 'ID'}
DECL = re.compile(r'^##[ \t]+\[([A-Z][A-Z0-9]{1,7})\][ \t]+(\S.*?)[ \t]*$')
H2 = re.compile(r'^##[ \t]+(\S.*?)[ \t]*$')
H3 = re.compile(r'^###[ \t]+(\S.*?)[ \t]*$')
FENCE = re.compile(r'^```(jobfather-entry|jobfather-cv-lists|jobfather-vocabulary)(?:[ \t]+([a-z_]+))?[ \t]*$')
ENTRY_KEYS = ('kind', 'org', 'title', 'location', 'start', 'end', 'cv', 'cv_weight')
KINDS = ('role', 'group', 'education', 'project', 'certification')
CV_MODES = ('tailor', 'locked', 'omit')
WEIGHTS = ('primary', 'secondary', 'light')
DATE = re.compile(r'^\d{4}(?:-(0[1-9]|1[0-2]))?$')
LIST_ITEM = re.compile(r'^\s*(?:\d+[.)]|[-*])\s+(\S.*?)\s*$')
TRAILING_REFS = re.compile(r'\s*\[([A-Z][A-Z0-9]{1,7}-H?\d{2,3}(?:\s*,\s*[A-Z][A-Z0-9]{1,7}-H?\d{2,3})*)\]\s*$')
NEVER = re.compile(r'(?!x)x')
COMMENT = re.compile(r'<!--.*?-->', re.S)


def strip_comments(text):
    """Drop <!-- guidance --> while keeping line numbers."""
    return COMMENT.sub(lambda m: '\n' * m.group(0).count('\n'), text)


class Catalog:
    """Parsed EXPERIENCE.md."""

    def __init__(self):
        self.prefixes = []
        self.sections = OrderedDict()   # prefix -> dict(heading, entry, claims, bullets, line)
        self.defined = set()
        self.retired = set()
        self.vocab = {}
        self.cv_summary = None
        self.cv_summary_refs = []
        self.cv_lists = {}
        self.pattern = NEVER

    @property
    def active(self):
        return self.defined - self.retired

    @property
    def entries(self):
        return [dict(s['entry'], id=p, heading=s['heading']) for p, s in self.sections.items() if s['entry']]

    def current_role(self):
        for entry in self.entries:
            if entry.get('kind') == 'role' and entry.get('end') == 'present':
                return entry
        return None


def _id_pattern(prefixes):
    if not prefixes:
        return NEVER
    return re.compile(r'(?<![A-Za-z0-9-])(?:%s)-H?\d{2,3}(?![A-Za-z0-9])' % '|'.join(sorted(prefixes, key=len, reverse=True)))


def _split_list(value):
    return [item.strip() for item in value.split(';') if item.strip()]


def _parse_entry(lines, start_line):
    entry = {}
    for offset, raw in enumerate(lines):
        line = re.split(r'\s+#\s', raw, maxsplit=1)[0].strip()
        if not line:
            continue
        if ':' not in line:
            raise ValueError('Line %d: expected "key: value" in jobfather-entry' % (start_line + offset))
        key, value = (part.strip() for part in line.split(':', 1))
        if key not in ENTRY_KEYS:
            raise ValueError('Line %d: unknown jobfather-entry key %r' % (start_line + offset, key))
        if key in entry:
            raise ValueError('Line %d: duplicate key %r' % (start_line + offset, key))
        entry[key] = value
    kind = entry.get('kind')
    if kind not in KINDS:
        raise ValueError('Line %d: kind must be one of %s' % (start_line, ', '.join(KINDS)))
    if not entry.get('org'):
        raise ValueError('Line %d: org is required' % start_line)
    if kind in ('role', 'education') and not entry.get('title'):
        raise ValueError('Line %d: title is required for %s entries' % (start_line, kind))
    if entry.get('start') and not DATE.match(entry['start']):
        raise ValueError('Line %d: start must be YYYY or YYYY-MM' % start_line)
    if entry.get('end') and entry['end'] != 'present' and not DATE.match(entry['end']):
        raise ValueError('Line %d: end must be YYYY, YYYY-MM or present' % start_line)
    entry.setdefault('cv', 'locked')
    if entry['cv'] not in CV_MODES:
        raise ValueError('Line %d: cv must be one of %s' % (start_line, ', '.join(CV_MODES)))
    if entry['cv'] == 'tailor':
        entry.setdefault('cv_weight', 'secondary')
        if entry['cv_weight'] not in WEIGHTS:
            raise ValueError('Line %d: cv_weight must be one of %s' % (start_line, ', '.join(WEIGHTS)))
    elif 'cv_weight' in entry:
        raise ValueError('Line %d: cv_weight applies only to cv: tailor' % start_line)
    return entry


def parse(text):
    """Parse EXPERIENCE.md text into a Catalog; raises ValueError with line numbers."""
    cat = Catalog()
    text = strip_comments(text)
    lines = text.splitlines()
    # Pass 1: declarations, so references anywhere can be recognised.
    for number, line in enumerate(lines, 1):
        m = DECL.match(line)
        if m:
            prefix = m.group(1)
            if prefix in RESERVED:
                raise ValueError('Line %d: prefix %s is reserved' % (number, prefix))
            if prefix in cat.sections:
                raise ValueError('Line %d: prefix %s declared twice' % (number, prefix))
            cat.prefixes.append(prefix)
            cat.sections[prefix] = {'heading': m.group(2), 'entry': None, 'claims': OrderedDict(),
                                    'bullets': [], 'line': number}
    cat.pattern = _id_pattern(cat.prefixes)
    definition_row = re.compile(r'^\|\s*(%s)\s*\|\s*(.*?)\s*\|' % cat.pattern.pattern) if cat.prefixes else None
    definition_bullet = re.compile(r'^\s*[-*]\s+\*\*(%s):\*\*\s*(.*)$' % cat.pattern.pattern) if cat.prefixes else None

    section, subsection, fence, fence_kind, fence_arg, fence_start = None, None, None, None, None, 0
    summary_lines = []
    for number, line in enumerate(lines, 1):
        if fence is not None:
            if line.strip() == '```':
                body = fence
                fence = None
                if fence_kind == 'jobfather-entry':
                    if section not in cat.sections:
                        raise ValueError('Line %d: jobfather-entry must sit in a [PREFIX] section' % fence_start)
                    if cat.sections[section]['entry'] is not None:
                        raise ValueError('Line %d: one jobfather-entry per section' % fence_start)
                    cat.sections[section]['entry'] = _parse_entry(body, fence_start + 1)
                elif fence_kind == 'jobfather-cv-lists':
                    for raw in body:
                        if raw.strip():
                            key, _, value = raw.partition(':')
                            key = key.strip()
                            if not re.fullmatch(r'[a-z][a-z0-9_]{1,31}', key):
                                raise ValueError('Line %d: bad CV list name %r' % (fence_start, key))
                            cat.cv_lists[key] = _split_list(value)
                else:
                    name = fence_arg or 'tools'
                    cat.vocab[name] = cat.vocab.get(name, []) + [i for raw in body for i in _split_list(raw)]
            else:
                fence.append(line)
            continue
        m = FENCE.match(line)
        if m:
            fence, fence_kind, fence_arg, fence_start = [], m.group(1), m.group(2), number
            continue
        m = DECL.match(line)
        if m:
            section, subsection = m.group(1), None
            continue
        m = H2.match(line)
        if m and not line.startswith('###'):
            section, subsection = m.group(1).strip().casefold(), None
            continue
        m = H3.match(line)
        if m:
            subsection = m.group(1).strip().casefold()
            continue
        if section == 'cv summary':
            summary_lines.append(line)
            continue
        if not cat.prefixes:
            continue
        found = (definition_row.match(line) if definition_row else None) or (definition_bullet.match(line) if definition_bullet else None)
        if found:
            claim_id, claim_text = found.group(1), found.group(2).strip()
            owner = claim_id.split('-', 1)[0]
            if owner != section:
                raise ValueError('Line %d: %s must be defined inside its own [%s] section' % (number, claim_id, owner))
            if claim_id in cat.defined:
                raise ValueError('Line %d: %s defined twice' % (number, claim_id))
            cat.defined.add(claim_id)
            cat.sections[owner]['claims'][claim_id] = claim_text
            if claim_text.casefold().startswith('(retired'):
                cat.retired.add(claim_id)
            continue
        if section in cat.sections and subsection == 'cv bullets':
            item = LIST_ITEM.match(line)
            if item:
                body = item.group(1)
                refs = []
                tail = TRAILING_REFS.search(body)
                if tail:
                    refs = [r.strip() for r in tail.group(1).split(',')]
                    body = body[:tail.start()].rstrip()
                cat.sections[section]['bullets'].append({'text': body, 'refs': refs, 'line': number})
    if fence is not None:
        raise ValueError('Line %d: unclosed ``` block' % fence_start)
    summary = '\n'.join(summary_lines).strip()
    if summary:
        tail = TRAILING_REFS.search(summary)
        if tail:
            cat.cv_summary_refs = [r.strip() for r in tail.group(1).split(',')]
            summary = summary[:tail.start()].rstrip()
        cat.cv_summary = re.sub(r'\s+', ' ', summary)
    for prefix, data in cat.sections.items():
        if data['entry'] is None:
            raise ValueError('Line %d: section [%s] needs a jobfather-entry block' % (data['line'], prefix))
    missing = references(text, cat) - cat.defined
    if missing:
        raise ValueError('Unknown evidence IDs referenced: ' + ', '.join(sorted(missing)))
    return cat


def references(text, cat):
    return set(cat.pattern.findall(strip_comments(text))) if cat.prefixes else set()


def require_known(ids, cat, active_only=True, what='Evidence IDs'):
    ids = set(ids)
    allowed = cat.active if active_only else cat.defined
    unknown = ids - allowed
    if unknown:
        raise ValueError('%s must refer to current EXPERIENCE claims: %s' % (what, ', '.join(sorted(unknown))))
    return ids


def leaks(text, cat):
    """Internal IDs that would leak into outgoing text."""
    return sorted(references(text, cat))


def vocabulary(cat, name='tools'):
    return {item.casefold() for item in cat.vocab.get(name, [])}


def load(ws):
    from pathlib import Path
    import layout
    from local import read_text
    path = Path(ws) / layout.EXPERIENCE
    return parse(read_text(path)) if path.is_file() else Catalog()
