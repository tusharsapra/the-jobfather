"""Pure parsers: page JSON and page HTML in, plain dicts out (no browser, no network).

The server collects what a page loaded (the site's own XHR/fetch JSON responses, matched by the
URL patterns in ``site_profile.json``) and the rendered HTML. These functions turn either into
``JobSummary`` / ``JobDetail`` / profile / questionnaire dicts. Key names and selectors come from
the site profile so a site change is a data change. Everything returned is third-party text and
is marked untrusted by the server.

Includes a small HTML tree builder and CSS selector matcher (tags, ``#id``, ``.class``,
attribute operators ``= ~= ^= $= *=`` with an optional ``i`` flag, descendant and child
combinators, comma groups) so DOM fallbacks are testable offline.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

IST = timezone(timedelta(hours=5, minutes=30))
DESCRIPTION_LIMIT = 20000
SNIPPET_LIMIT = 300
VOID = frozenset('area base br col embed hr img input link meta param source track wbr'.split())
BLOCK = frozenset('address article aside blockquote br dd div dl dt fieldset figcaption figure footer form h1 h2 h3 '
                  'h4 h5 h6 header hr li main nav ol p pre section table tbody td tfoot th thead tr ul label '
                  'button option'.split())
SKIP = frozenset('script style noscript template svg head title'.split())
CONTROL = re.compile(r'[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f​-‏  ⁠﻿]')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')
PHONE = re.compile(r'(?<![\w])(?:\+?\d[\d\s().-]{5,}\d)(?![\w])')


# ====================================================================== text helpers
def clean(text: Any, limit: int | None = None) -> str:
    """Collapse whitespace and drop control / zero-width characters."""
    if text is None:
        return ''
    value = CONTROL.sub('', html_lib.unescape(str(text)))
    value = re.sub(r'\s+', ' ', value).strip()
    return value[:limit] if limit else value


def scrub_contacts(text: str) -> str:
    """Remove e-mail addresses and phone-like numbers (used on profile text)."""
    return PHONE.sub('[removed]', EMAIL.sub('[removed]', text or ''))


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in SKIP:
            self.skip += 1
        elif tag == 'li':
            self.parts.append('\n• ')
        elif tag in BLOCK:
            self.parts.append('\n')

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        if tag in BLOCK:
            self.parts.append('\n')

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in BLOCK and tag != 'li':
            self.parts.append('\n')

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)


def html_to_text(fragment: str | None) -> str:
    """Readable text from an HTML fragment (paragraphs and list items keep their breaks)."""
    if not fragment:
        return ''
    parser = _TextExtractor()
    parser.feed(str(fragment))
    parser.close()
    text = CONTROL.sub('', ''.join(parser.parts)).replace('\xa0', ' ')
    lines = [re.sub(r'[ \t\r\f\v]+', ' ', line).strip() for line in text.split('\n')]
    out: list[str] = []
    for line in lines:
        if line in ('', '•'):
            if out and out[-1] != '':
                out.append('')
            continue
        out.append(line)
    return '\n'.join(out).strip()


def limited(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip(), True


def snippet(fragment: str | None) -> str | None:
    text = clean(html_to_text(fragment).replace('\n', ' '))
    if not text:
        return None
    return text if len(text) <= SNIPPET_LIMIT else text[:SNIPPET_LIMIT - 1].rstrip() + '…'


# ====================================================================== field parsers
def experience(text: str | None, low: Any = None, high: Any = None) -> dict[str, Any]:
    """``6-11 Yrs`` → {min_years: 6, max_years: 11, text}."""
    label = clean(text) or None
    lo, hi = _num(low), _num(high)
    if label and (lo is None or hi is None):
        match = re.search(r'(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)', label)
        plus = re.search(r'(\d+(?:\.\d+)?)\s*\+', label)
        if match:
            lo = lo if lo is not None else float(match.group(1))
            hi = hi if hi is not None else float(match.group(2))
        elif plus:
            lo = lo if lo is not None else float(plus.group(1))
        elif re.search(r'\bfresher', label, re.I):
            lo, hi = 0.0, 0.0
    return {'min_years': _int_or_float(lo), 'max_years': _int_or_float(hi), 'text': label}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _int_or_float(value: float | None) -> float | int | None:
    if value is None:
        return None
    return int(value) if float(value).is_integer() else value


_UNIT = {'lac': 100000, 'lacs': 100000, 'lakh': 100000, 'lakhs': 100000, 'l': 100000, 'lpa': 100000,
         'cr': 10000000, 'crore': 10000000, 'crores': 10000000, 'k': 1000}


def salary(label: str | None, detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Salary text plus annual INR bounds; ``disclosed`` is False for 'Not disclosed' and hidden salaries."""
    text = clean(label) or None
    detail = detail or {}
    hidden = detail.get('hideSalary') is True
    lo, hi = _num(detail.get('minimumSalary')), _num(detail.get('maximumSalary'))
    if not text:
        text = clean(detail.get('salaryText') or detail.get('label')) or None
    if text and re.search(r'not\s+disclosed', text, re.I):
        return {'text': text, 'min_inr': None, 'max_inr': None, 'disclosed': False}
    if not hidden and lo and hi and lo > 0 and hi >= lo:
        return {'text': text, 'min_inr': int(lo), 'max_inr': int(hi), 'disclosed': True}
    if text:
        parsed = _salary_from_text(text)
        if parsed:
            return {'text': text, 'min_inr': parsed[0], 'max_inr': parsed[1], 'disclosed': True}
        return {'text': text, 'min_inr': None, 'max_inr': None, 'disclosed': not hidden}
    return {'text': None, 'min_inr': None, 'max_inr': None, 'disclosed': False}


def _salary_from_text(text: str) -> tuple[int | None, int | None] | None:
    lowered = text.lower()
    match = re.search(r'([\d.,]+)\s*(lacs?|lakhs?|cr|crores?|k)?\s*(?:-|to)\s*([\d.,]+)\s*(lacs?|lakhs?|cr|crores?|k|lpa)?',
                      lowered)
    single = None if match else re.search(r'([\d.,]+)\s*(lacs?|lakhs?|cr|crores?|lpa|k)\b', lowered)
    if not match and not single:
        return None
    monthly = 12 if re.search(r'\bp\.?\s*m\b|per month|/\s*month|monthly', lowered) else 1

    def value(number: str, unit: str | None) -> int | None:
        raw = number.replace(',', '')
        try:
            amount = float(raw)
        except ValueError:
            return None
        if ',' in number or amount >= 1000:
            factor = 1  # already rupees (e.g. 50,000)
        else:
            factor = _UNIT.get((unit or '').rstrip('.'), 100000)
        return int(round(amount * factor * monthly))

    if match:
        high_unit = match.group(4)
        return value(match.group(1), match.group(2) or high_unit), value(match.group(3), high_unit or match.group(2))
    assert single is not None
    amount = value(single.group(1), single.group(2))
    return amount, amount


def posted(text: str | None, created: Any = None, today: date | None = None) -> dict[str, Any]:
    """``5 Days Ago`` / epoch milliseconds / ``2026-10-13 11:16:47`` → {text, date (ISO, IST)}."""
    label = clean(text) or None
    today = today or datetime.now(IST).date()
    day: date | None = None
    if isinstance(created, (int, float)) and not isinstance(created, bool) and created > 10**11:
        day = datetime.fromtimestamp(created / 1000, IST).date()
    elif isinstance(created, str) and re.match(r'\d{4}-\d{2}-\d{2}', created):
        try:
            day = date.fromisoformat(created[:10])
        except ValueError:
            day = None
    if day is None and label:
        low = label.lower()
        match = re.search(r'(\d+)\s*(day|week|month)s?\s+ago', low)
        if '+' in low:
            day = None  # "30+ days ago" is only a bound
        elif match:
            n = int(match.group(1))
            unit = match.group(2)
            if unit == 'day':
                day = today - timedelta(days=n)
            elif unit == 'week':
                day = today - timedelta(weeks=n)
        elif re.search(r'just now|today|hours? ago|minutes? ago|few hours', low):
            day = today
        elif 'yesterday' in low:
            day = today - timedelta(days=1)
    return {'text': label, 'date': day.isoformat() if day else None}


def split_locations(label: str | None) -> tuple[list[str], str | None]:
    """``Hybrid - Example City, Sample Town`` → (['Example City', 'Sample Town'], 'hybrid')."""
    text = clean(label)
    mode = None
    match = re.match(r'^(hybrid|remote|work from home|wfh|temp\.?\s*wfh[^-]*)\s*(?:-\s*|$)', text, re.I)
    if match:
        word = match.group(1).lower()
        mode = 'hybrid' if word.startswith('hybrid') else 'remote'
        text = text[match.end():]
    places = [clean(p) for p in re.split(r',(?![^()]*\))', text) if clean(p)]
    return places, mode


def work_mode_from(value: Any, mapping: Mapping[str, str]) -> str | None:
    if value is None:
        return None
    key = str(value).strip()
    if key in mapping:
        return mapping[key]
    low = key.lower()
    if low.startswith('hybrid'):
        return 'hybrid'
    if low.startswith(('remote', 'work from home', 'wfh')):
        return 'remote'
    if 'office' in low:
        return 'office'
    return None


def skills_from(item: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    tags = item.get('tagsAndSkills')
    if isinstance(tags, str):
        out += [clean(t) for t in tags.split(',')]
    keys = item.get('keySkills')
    if isinstance(keys, Mapping):
        order = keys.get('tagsOrder') if isinstance(keys.get('tagsOrder'), list) else None
        for name in (order or [k for k in keys if k != 'tagsOrder']):
            for entry in keys.get(name) or ():
                out.append(clean(entry.get('label') if isinstance(entry, Mapping) else entry))
    elif isinstance(keys, list):
        out += [clean(k.get('label') if isinstance(k, Mapping) else k) for k in keys]
    return list(dict.fromkeys(s for s in out if s))[:60]


def _get(data: Any, path: str) -> Any:
    """Dotted path lookup (``a.b.0.c``); None when missing."""
    current = data
    for part in path.split('.') if path else ():
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


# ====================================================================== JSON parsers
def summary_from_json(item: Mapping[str, Any], site: Any, today: date | None = None) -> dict[str, Any] | None:
    """One job tuple from the site's JSON (search, similar, recommended and saved share this shape)."""
    spec = site.section('json_fields')
    job_id = clean(_first(item, spec['job_id']))
    if not re.fullmatch(r'\d{6,20}', job_id or ''):
        return None
    holders = {clean(p.get('type')).lower(): clean(p.get('label')) for p in item.get('placeholders') or ()
               if isinstance(p, Mapping)}
    places, mode = split_locations(holders.get('location') or _first(item, spec['location_text']))
    exp = experience(holders.get('experience') or _first(item, spec['experience_text']),
                     item.get('minimumExperience'), item.get('maximumExperience'))
    pay = salary(holders.get('salary'), item.get('salaryDetail') if isinstance(item.get('salaryDetail'), Mapping) else None)
    mode = work_mode_from(item.get('wfhType'), site.section('json_fields').get('wfh_types', {})) or mode
    company_site = _first(item, spec['company_apply_flags'])
    redirect = clean(_first(item, spec['external_apply_url'])) or None
    if company_site is True or redirect:
        apply_type = 'company_site'
    elif company_site is False:
        apply_type = 'naukri'
    else:
        apply_type = 'unknown'
    applied = _first(item, spec['already_applied'])
    return {
        'job_id': job_id,
        'url': site.canonical_job_url(job_id, clean(_first(item, spec['url'])) or None),
        'title': clean(_first(item, spec['title']), 300) or None,
        'company': clean(_first(item, spec['company']), 200) or None,
        'locations': places,
        'experience': exp,
        'salary': pay,
        'skills': skills_from(item),
        'posted': posted(_first(item, spec['posted_text']), item.get('createdDate'), today),
        'work_mode': mode,
        'apply_type': apply_type,
        'already_applied': applied if isinstance(applied, bool) else None,
        'snippet': snippet(_first(item, spec['snippet'])),
    }


def _first(item: Mapping[str, Any], paths: Sequence[str] | str) -> Any:
    for path in [paths] if isinstance(paths, str) else paths:
        value = _get(item, path)
        if value not in (None, ''):
            return value
    return None


def find_job_lists(data: Any, depth: int = 0) -> list[list[Mapping[str, Any]]]:
    """Every list of job-like dicts (with an id and a title) inside a JSON document."""
    found: list[list[Mapping[str, Any]]] = []
    if depth > 6:
        return found
    if isinstance(data, list):
        jobs = [x for x in data if isinstance(x, Mapping) and _looks_like_job(x)]
        if jobs and len(jobs) >= max(1, len(data) // 2):
            found.append(jobs)
            return found
        for value in data[:50]:
            found += find_job_lists(value, depth + 1)
    elif isinstance(data, Mapping):
        for value in data.values():
            found += find_job_lists(value, depth + 1)
    return found


def _looks_like_job(item: Mapping[str, Any]) -> bool:
    ident = item.get('jobId', item.get('jobid', item.get('job_id')))
    return ident is not None and re.fullmatch(r'\d{6,20}', str(ident)) is not None and bool(item.get('title'))


def jobs_from_json_list(items: Iterable[Mapping[str, Any]], site: Any, today: date | None = None,
                        seen: set[str] | None = None) -> tuple[list[dict[str, Any]], int]:
    """Parsed summaries plus how many items could not be parsed."""
    seen = set() if seen is None else seen
    jobs, failed = [], 0
    for item in items:
        try:
            job = summary_from_json(item, site, today)
        except Exception:
            job = None
        if job is None:
            failed += 1
            continue
        if job['job_id'] in seen:
            continue
        seen.add(job['job_id'])
        jobs.append(job)
    return jobs, failed


def search_from_json(body: Mapping[str, Any], site: Any, today: date | None = None) -> dict[str, Any]:
    """{jobs, total, failed, empty} from the search response."""
    spec = site.section('search')['response']
    items = _get(body, spec['jobs_key'])
    total = _num(_get(body, spec['total_key']))
    if items is None:
        if total == 0:
            return {'jobs': [], 'total': 0, 'failed': 0, 'empty': True}
        raise ValueError('search response has no job list')
    if not isinstance(items, list):
        raise ValueError('search response job list is not a list')
    jobs, failed = jobs_from_json_list(items, site, today)
    return {'jobs': jobs, 'total': int(total) if total is not None else None, 'failed': failed,
            'empty': not items}


def similar_from_json(body: Mapping[str, Any], site: Any, today: date | None = None) -> dict[str, Any]:
    spec = site.section('similar')['response']
    seen: set[str] = set()
    jobs: list[dict[str, Any]] = []
    failed = 0
    present = False
    for path in spec['lists']:
        items = _get(body, path)
        if isinstance(items, list):
            present = True
            more, bad = jobs_from_json_list(items, site, today, seen)
            jobs += more
            failed += bad
    if not present:
        raise ValueError('similar-jobs response has no job list')
    return {'jobs': jobs, 'failed': failed}


def detail_from_json(body: Mapping[str, Any], site: Any, today: date | None = None) -> dict[str, Any]:
    """A ``JobDetail`` dict from the job-detail response."""
    spec = site.section('job')['response']
    jd = _get(body, spec['detail_key'])
    if not isinstance(jd, Mapping):
        raise ValueError('job response has no job details')
    fields = site.section('json_fields')
    job_id = clean(jd.get('jobId'))
    if not re.fullmatch(r'\d{6,20}', job_id):
        raise ValueError('job response has no job id')
    places = [clean(loc.get('label') if isinstance(loc, Mapping) else loc) for loc in jd.get('locations') or ()]
    places = [p for p in places if p]
    company = jd.get('companyDetail') if isinstance(jd.get('companyDetail'), Mapping) else {}
    redirect = clean(_first(jd, fields['external_apply_url'])) or None
    description, truncated = limited(html_to_text(jd.get('description')), DESCRIPTION_LIMIT)
    education = []
    edu = jd.get('education') if isinstance(jd.get('education'), Mapping) else {}
    for level in ('ug', 'pg', 'ppg'):
        for entry in edu.get(level) or ():
            text = clean(entry.get('label') if isinstance(entry, Mapping) else entry)
            if text:
                education.append('%s: %s' % (level.upper(), text))
    skills = skills_from({'keySkills': {'tagsOrder': ['preferred', 'other'], **(jd.get('keySkills') or {})}}
                         if isinstance(jd.get('keySkills'), Mapping) else jd)
    mode = work_mode_from(jd.get('wfhType'), fields.get('wfh_types', {})) or work_mode_from(jd.get('wfhLabel'), {})
    applied = _first(jd, fields['already_applied'])
    if redirect:
        apply_type = 'company_site'
    elif jd.get('hideApplyButton') is True or jd.get('mode') in fields.get('company_site_modes', ()):
        apply_type = 'unknown'
    else:
        apply_type = 'naukri'
    recruiter = None
    for key in fields.get('recruiter_keys', ()):
        value = jd.get(key)
        if isinstance(value, Mapping) and (value.get('name') or value.get('designation')):
            recruiter = {'name': clean(value.get('name'), 120) or None,
                         'designation': clean(value.get('designation'), 120) or None}
            break
    sal_detail = jd.get('salaryDetail') if isinstance(jd.get('salaryDetail'), Mapping) else {}
    return {
        'job_id': job_id,
        'url': site.canonical_job_url(job_id, clean(jd.get('staticUrl')) or None),
        'title': clean(jd.get('title'), 300) or None,
        'company': clean(company.get('name'), 200) or None,
        'locations': places,
        'experience': experience(jd.get('experienceText'), jd.get('minimumExperience'), jd.get('maximumExperience')),
        'salary': salary(sal_detail.get('salaryText') or sal_detail.get('label'), sal_detail),
        'skills': skills,
        'posted': posted(None, jd.get('createdDate'), today),
        'work_mode': mode,
        'apply_type': apply_type,
        'already_applied': applied if isinstance(applied, bool) else None,
        'snippet': snippet(jd.get('shortDescription')) or (description[:SNIPPET_LIMIT] or None),
        'description_text': description,
        'truncated': truncated,
        'role': clean(jd.get('jobRole') or jd.get('roleCategory'), 200) or None,
        'industry': clean(jd.get('industry'), 200) or None,
        'department': clean(jd.get('functionalArea'), 200) or None,
        'employment_type': clean(jd.get('employmentType'), 120) or None,
        'education': education,
        'openings': _count(jd.get('vacancy')),
        'applicants': _count(jd.get('applyCount')),
        'recruiter': recruiter,
        'external_apply_url': redirect,
    }


def _count(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None and number >= 0 else None


def response_matches(url: str, pattern: str) -> re.Match[str] | None:
    return re.search(pattern, url or '')


# ====================================================================== mini DOM
class Node:
    __slots__ = ('tag', 'attrs', 'children', 'parent')

    def __init__(self, tag: str, attrs: dict[str, str], parent: Node | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node | str] = []
        self.parent = parent

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.attrs.get(name, default)

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get('class') or '').split()

    def elements(self) -> Iterator[Node]:
        """Descendant elements in document order."""
        stack = list(reversed([c for c in self.children if isinstance(c, Node)]))
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed([c for c in node.children if isinstance(c, Node)]))

    def text(self, separator: str = ' ') -> str:
        parts: list[str] = []

        def walk(node: Node) -> None:
            for child in node.children:
                if isinstance(child, str):
                    parts.append(child)
                elif child.tag not in SKIP:
                    if child.tag in BLOCK:
                        parts.append(separator)
                    walk(child)
                    if child.tag in BLOCK:
                        parts.append(separator)
        walk(self)
        if '\n' in separator:
            lines = (clean(line) for line in ''.join(parts).split('\n'))
            return '\n'.join(line for line in lines if line)
        return clean(''.join(parts))

    def select(self, selector: str) -> list[Node]:
        return select(self, selector)

    def select_one(self, selector: str) -> Node | None:
        found = select(self, selector)
        return found[0] if found else None

    def next_element(self) -> Node | None:
        if self.parent is None:
            return None
        siblings = [c for c in self.parent.children if isinstance(c, Node)]
        index = siblings.index(self)
        return siblings[index + 1] if index + 1 < len(siblings) else None


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node('#document', {}, None)
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        parent = self.stack[-1]
        node = Node(tag, {k.lower(): (v if v is not None else '') for k, v in attrs}, parent)
        parent.children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        parent = self.stack[-1]
        parent.children.append(Node(tag, {k.lower(): (v if v is not None else '') for k, v in attrs}, parent))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse_html(markup: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(markup or '')
    builder.close()
    return builder.root


_ATTR = re.compile(r'\[\s*([\w:-]+)\s*(?:([~^$*|]?=)\s*(?:"([^"]*)"|\'([^\']*)\'|([^\]\s]+))\s*(i)?\s*)?\]')
_TOKEN = re.compile(r'([#.]?)([\w-]+)|(\[[^\]]*\])|(\*)')


def _split_top(selector: str, sep: str) -> list[str]:
    out, depth, quote, current = [], 0, '', ''
    for ch in selector:
        if quote:
            current += ch
            if ch == quote:
                quote = ''
            continue
        if ch in '"\'':
            quote = ch
        elif ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
        if ch == sep and depth == 0:
            out.append(current)
            current = ''
            continue
        current += ch
    out.append(current)
    return [part.strip() for part in out if part.strip()]


def _compound(text: str) -> Callable[[Node], bool]:
    checks: list[Callable[[Node], bool]] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match:
            raise ValueError('Unsupported selector: %r' % text)
        prefix, name, attr, star = match.groups()
        if star:
            pass
        elif attr:
            amatch = _ATTR.fullmatch(attr)
            if not amatch:
                raise ValueError('Unsupported attribute selector: %r' % attr)
            checks.append(_attr_check(amatch))
        elif prefix == '#':
            checks.append(lambda n, v=name: n.attrs.get('id') == v)
        elif prefix == '.':
            checks.append(lambda n, v=name: v in n.classes)
        else:
            checks.append(lambda n, v=name.lower(): n.tag == v)
        pos = match.end()
    return lambda node: all(check(node) for check in checks)


def _attr_check(match: re.Match[str]) -> Callable[[Node], bool]:
    name, op = match.group(1).lower(), match.group(2)
    raw = next((g for g in (match.group(3), match.group(4), match.group(5)) if g is not None), None)
    fold = bool(match.group(6))

    def check(node: Node) -> bool:
        if name not in node.attrs:
            return False
        if op is None:
            return True
        have = node.attrs[name]
        want = raw or ''
        if fold:
            have, want = have.lower(), want.lower()
        if op == '=':
            return have == want
        if op == '~=':
            return want in have.split()
        if op == '^=':
            return bool(want) and have.startswith(want)
        if op == '$=':
            return bool(want) and have.endswith(want)
        if op == '*=':
            return bool(want) and want in have
        if op == '|=':
            return have == want or have.startswith(want + '-')
        return False
    return check


def _parse_complex(selector: str) -> list[tuple[str, Callable[[Node], bool]]]:
    """[(combinator, matcher)] left to right; the first combinator is ''."""
    parts = re.sub(r'\s*>\s*', ' > ', selector.strip())
    tokens = _split_top(parts, ' ')
    out: list[tuple[str, Callable[[Node], bool]]] = []
    combinator = ''
    for token in tokens:
        if token == '>':
            combinator = '>'
            continue
        out.append((combinator or (' ' if out else ''), _compound(token)))
        combinator = ''
    return out


def _matches(node: Node, chain: list[tuple[str, Callable[[Node], bool]]], scope: Node) -> bool:
    if not chain[-1][1](node):
        return False
    current = node
    for index in range(len(chain) - 1, 0, -1):
        combinator = chain[index][0]
        matcher = chain[index - 1][1]
        if combinator == '>':
            current = current.parent  # type: ignore[assignment]
            if current is None or current is scope or not matcher(current):
                return False
        else:
            current = current.parent  # type: ignore[assignment]
            while current is not None and current is not scope and not matcher(current):
                current = current.parent  # type: ignore[assignment]
            if current is None or current is scope:
                return False
    return True


def select(scope: Node, selector: str) -> list[Node]:
    """Elements under ``scope`` matching a CSS selector group, in document order."""
    chains = [_parse_complex(group) for group in _split_top(selector, ',')]
    return [node for node in scope.elements() if any(_matches(node, chain, scope) for chain in chains)]


# ====================================================================== DOM parsers
def ld_json(root: Node) -> list[Any]:
    blocks = []
    for node in root.select('script[type="application/ld+json"]'):
        raw = ''.join(c for c in node.children if isinstance(c, str))
        try:
            blocks.append(json.loads(raw))
        except ValueError:
            continue
    return blocks


def _field_text(node: Node, selector: str | None, attr: str | None = None) -> str | None:
    if not selector:
        return None
    target = node.select_one(selector)
    if target is None:
        return None
    value = target.get(attr) if attr else target.text()
    return clean(value) or None


def jobs_from_dom(markup: str, site: Any, feature: str = 'search', today: date | None = None) -> dict[str, Any]:
    """Job tuples from rendered HTML using the ``dom`` selectors of ``feature``."""
    spec = site.section(feature).get('dom') or site.section('search')['dom']
    root = parse_html(markup)
    jobs, failed, seen = [], 0, set()
    for tuple_node in root.select(spec['item']):
        job_id = clean(tuple_node.get(spec['id_attr']) or '')
        link = tuple_node.select_one(spec['title_link'])
        href = clean(link.get('href')) if link is not None else ''
        if not re.fullmatch(r'\d{6,20}', job_id):
            match = re.search(r'-(\d{6,20})(?:[?#/]|$)', href)
            job_id = match.group(1) if match else ''
        if not job_id:
            failed += 1
            continue
        if job_id in seen:
            continue
        seen.add(job_id)
        places, mode = split_locations(_field_text(tuple_node, spec.get('location')))
        tags = [clean(t.text()) for t in tuple_node.select(spec['skills'])] if spec.get('skills') else []
        jobs.append({
            'job_id': job_id,
            'url': site.canonical_job_url(job_id, href or None),
            'title': clean(link.text() if link is not None else '', 300) or None,
            'company': _field_text(tuple_node, spec.get('company')),
            'locations': places,
            'experience': experience(_field_text(tuple_node, spec.get('experience'))),
            'salary': salary(_field_text(tuple_node, spec.get('salary')) or 'Not disclosed'),
            'skills': [t for t in tags if t],
            'posted': posted(_field_text(tuple_node, spec.get('posted')), None, today),
            'work_mode': mode,
            'apply_type': 'unknown',
            'already_applied': None,
            'snippet': _field_text(tuple_node, spec.get('snippet')),
        })
    total = None
    count_text = _field_text(root, spec.get('count'))
    if count_text:
        match = re.search(r'of\s+([\d,]+)', count_text)
        total = int(match.group(1).replace(',', '')) if match else None
    return {'jobs': jobs, 'failed': failed, 'total': total}


def labelled_values(root: Node) -> dict[str, str]:
    """``<label>Role: </label><span>…</span>`` pairs as {'role': '…'} (lower-case label without colon)."""
    out: dict[str, str] = {}
    for label in root.select('label'):
        key = clean(label.text()).rstrip(':').strip().lower()
        sibling = label.next_element()
        if key and sibling is not None and key not in out:
            out[key] = clean(sibling.text(separator=' ')).strip(' ,')
    return out


def detail_from_dom(markup: str, site: Any, job_id: str | None = None, today: date | None = None) -> dict[str, Any]:
    """A best-effort ``JobDetail`` from rendered HTML (JSON-LD JobPosting plus labelled values)."""
    spec = site.section('job')['dom']
    root = parse_html(markup)
    posting: Mapping[str, Any] = {}
    for block in ld_json(root):
        for item in block if isinstance(block, list) else [block]:
            if isinstance(item, Mapping) and item.get('@type') == 'JobPosting':
                posting = item
                break
    labels = labelled_values(root)
    ident = _get(posting, 'identifier.value')
    job_id = clean(ident) if ident and re.fullmatch(r'\d{6,20}', clean(ident)) else job_id
    if not job_id:
        raise ValueError('no job id on the page')
    title = clean(posting.get('title')) or _field_text(root, spec['title'])
    if not title:
        raise ValueError('no job title on the page')
    description_html = posting.get('description')
    if not description_html:
        node = root.select_one(spec['description'])
        description_html = ''.join(_outer(c) for c in node.children) if node is not None else ''
    description, truncated = limited(html_to_text(description_html), DESCRIPTION_LIMIT)
    place_values = _get(posting, 'jobLocation.address.addressLocality')
    places = [clean(p) for p in (place_values if isinstance(place_values, list) else [place_values] if place_values else [])]
    if not places:
        places, _ = split_locations(_field_text(root, spec.get('location')))
    exp_text = _field_text(root, spec.get('experience'))
    sal_text = _get(posting, 'baseSalary.value.value') or _field_text(root, spec.get('salary'))
    mode_text = _field_text(root, spec.get('work_mode'))
    education = [('%s: %s' % (k.upper(), v)) for k, v in labels.items() if k in ('ug', 'pg', 'doctorate') and v]
    skills = posting.get('skills') if isinstance(posting.get('skills'), list) else \
        [clean(n.text()) for n in root.select(spec['skills'])] if spec.get('skills') else []
    return {
        'job_id': job_id,
        'url': site.job_url(job_id),
        'title': clean(title, 300),
        'company': clean(_get(posting, 'hiringOrganization.name'), 200) or _field_text(root, spec.get('company')),
        'locations': [p for p in places if p and p != '-'],
        'experience': experience(exp_text),
        'salary': salary(clean(sal_text) or None),
        'skills': list(dict.fromkeys(clean(s) for s in skills if clean(s)))[:60],
        'posted': posted(labels.get('posted'), posting.get('datePosted'), today),
        'work_mode': work_mode_from(mode_text, {}),
        'apply_type': 'unknown',
        'already_applied': None,
        'snippet': description[:SNIPPET_LIMIT] or None,
        'description_text': description,
        'truncated': truncated,
        'role': labels.get('role') or None,
        'industry': labels.get('industry type') or clean(posting.get('industry')) or None,
        'department': labels.get('department') or None,
        'employment_type': labels.get('employment type') or clean(posting.get('employmentType')) or None,
        'education': education,
        'openings': _count(re.sub(r'\D', '', labels.get('openings', '')) or None),
        'applicants': _count(re.sub(r'\D', '', labels.get('applicants', '')) or None),
        'recruiter': None,
        'external_apply_url': None,
    }


def merge_dom_into_detail(detail: dict[str, Any], markup: str) -> dict[str, Any]:
    """Fill gaps of a JSON-parsed detail with labelled values from the page (posted text)."""
    labels = labelled_values(parse_html(markup)) if markup else {}
    if labels.get('posted') and not detail['posted'].get('text'):
        detail['posted']['text'] = labels['posted']
    return detail


def _outer(node: Node | str) -> str:
    if isinstance(node, str):
        return html_lib.escape(node, quote=False)
    attrs = ''.join(' %s="%s"' % (k, html_lib.escape(v)) for k, v in node.attrs.items())
    if node.tag in VOID:
        return '<%s%s>' % (node.tag, attrs)
    return '<%s%s>%s</%s>' % (node.tag, attrs, ''.join(_outer(c) for c in node.children), node.tag)


# ====================================================================== profile
def profile_summary(jsons: Iterable[Any], markup: str, site: Any, today: date | None = None) -> dict[str, Any]:
    """Allow-listed profile facts only (never contact fields), from JSON paths and visible labels."""
    spec = site.section('profile')
    out: dict[str, Any] = {'resume': {'present': False, 'file_name': None, 'uploaded_on': None},
                           'profile_updated_on': None, 'completeness_pct': None, 'notice_period_text': None,
                           'preferred_locations': []}
    found_any = False
    for body in jsons:
        for field, paths in (spec.get('json_paths') or {}).items():
            for path in paths:
                value = _get(body, path)
                if value in (None, '', []):
                    continue
                found_any = True
                _set_profile(out, field, value, today)
                break
    if markup:
        root = parse_html(markup)
        text = root.text(separator='\n')
        for field, pattern in (spec.get('text_patterns') or {}).items():
            match = re.search(pattern, text, re.I)
            if match and _profile_missing(out, field):
                found_any = True
                _set_profile(out, field, match.group('value'), today)
    out['found'] = found_any
    # never let anything contact-like through free text
    for key in ('notice_period_text',):
        if out[key]:
            out[key] = scrub_contacts(clean(out[key], 120))
    if out['resume']['file_name']:
        # Resume file names can embed a phone number between underscores; drop any long digit run.
        name = re.sub(r'\d{7,}', '[removed]', scrub_contacts(clean(out['resume']['file_name'], 160)))
        out['resume']['file_name'] = name
    out['preferred_locations'] = [scrub_contacts(clean(p, 80)) for p in out['preferred_locations'] if clean(p)][:20]
    return out


def _profile_missing(out: Mapping[str, Any], field: str) -> bool:
    if field.startswith('resume_'):
        key = field.split('_', 1)[1]
        return not out['resume'].get(key)
    return not out.get(field)


def _set_profile(out: dict[str, Any], field: str, value: Any, today: date | None) -> None:
    if field == 'resume_file_name':
        out['resume']['file_name'] = clean(value, 160) or None
        out['resume']['present'] = bool(out['resume']['file_name']) or out['resume']['present']
    elif field == 'resume_uploaded_on':
        out['resume']['uploaded_on'] = _date_text(value, today)
        out['resume']['present'] = True
    elif field == 'resume_present':
        out['resume']['present'] = bool(value)
    elif field == 'profile_updated_on':
        out['profile_updated_on'] = _date_text(value, today)
    elif field == 'completeness_pct':
        number = _num(re.sub(r'[^\d.]', '', str(value)) or None)
        out['completeness_pct'] = int(number) if number is not None and 0 <= number <= 100 else None
    elif field == 'notice_period_text':
        out['notice_period_text'] = clean(re.split(r'[,;|]', str(value))[0], 120) or None
    elif field == 'preferred_locations':
        if isinstance(value, list):
            items = [v.get('label') or v.get('name') if isinstance(v, Mapping) else v for v in value]
        else:
            items = re.split(r',|;', str(value))
        out['preferred_locations'] = [clean(i) for i in items if clean(i)]


def _date_text(value: Any, today: date | None) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 10**11:
        return datetime.fromtimestamp(value / 1000, IST).date().isoformat()
    text = clean(value, 60)
    if not text:
        return None
    if re.match(r'\d{4}-\d{2}-\d{2}', text):
        return text[:10]
    text = re.sub(r'\s+,', ',', re.sub(r'(?<=\d)(?=[A-Za-z])', ' ', text))  # "18Oct , 2026" -> "18 Oct, 2026"
    low = text.lower()
    if low in ('today', 'just now'):
        return (today or datetime.now(IST).date()).isoformat()
    if low == 'yesterday':
        return ((today or datetime.now(IST).date()) - timedelta(days=1)).isoformat()
    for fmt in ('%d %b, %Y', '%d %b %Y', '%b %d, %Y', '%d%b, %Y', '%d %B %Y', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


# ====================================================================== questionnaires
QUESTION_TYPES = ('single_select', 'multi_select', 'text', 'textarea', 'number', 'date', 'boolean')


def qid_for(*parts: str) -> str:
    """A stable question id from the field's own name/id and label."""
    digest = hashlib.sha256('\x1f'.join(clean(p) for p in parts).encode('utf-8')).hexdigest()[:12]
    return 'q_' + digest


def questions_from_dom(markup: str, site: Any) -> list[dict[str, Any]]:
    """Visible questionnaire fields inside the apply dialog (rendered HTML of that container).

    Each question carries a private ``_locator`` (how to find the control again) that the server
    keeps and never returns to the host.
    """
    spec = site.section('apply')['questionnaire']
    root = parse_html(markup)
    questions: list[dict[str, Any]] = []
    labels_for = {lab.get('for'): clean(lab.text()) for lab in root.select('label[for]')}
    groups: dict[str, list[Node]] = {}
    order: list[tuple[str, Any]] = []
    prompt = None
    for node in root.elements():
        if node.tag in ('input', 'select', 'textarea') or node.get('contenteditable') in ('true', ''):
            pass
        else:
            if any(node.select_one(sel) is None and _matches_simple(node, sel) for sel in spec['prompt']):
                prompt = clean(node.text()) or prompt
            continue
        kind = (node.get('type') or '').lower()
        if node.tag == 'input' and kind in ('hidden', 'submit', 'button', 'file', 'image', 'reset', 'search'):
            continue
        if node.tag == 'input' and kind in ('radio', 'checkbox'):
            name = node.get('name') or node.get('id') or ''
            key = '%s:%s' % (kind, name)
            if key not in groups:
                groups[key] = []
                order.append(('group', (key, kind, name, prompt)))
            groups[key].append(node)
            continue
        order.append(('field', (node, prompt)))
    for entry_kind, payload in order:
        if entry_kind == 'group':
            key, kind, name, group_prompt = payload
            nodes = groups[key]
            options = []
            for node in nodes:
                label = labels_for.get(node.get('id')) or clean(node.get('aria-label')) or clean(node.get('value'))
                if label:
                    options.append({'value': clean(node.get('value')) or label, 'label': label})
            qtype = 'single_select' if kind == 'radio' else 'multi_select'
            if kind == 'checkbox' and len(options) == 1:
                qtype = 'boolean'
                legend = _legend(nodes[0]) or options[0]['label']
            else:
                legend = _legend(nodes[0]) or group_prompt or name
            questions.append({'qid': qid_for(kind, name, legend), 'label': clean(legend, 500) or 'Question',
                              'type': qtype, 'required': any(n.get('required') is not None or
                                                             n.get('aria-required') == 'true' for n in nodes),
                              'options': options, 'max_length': None, 'min': None, 'max': None,
                              '_locator': {'kind': kind, 'name': name}})
            continue
        node, field_prompt = payload
        label = (labels_for.get(node.get('id')) or clean(node.get('aria-label')) or clean(node.get('placeholder'))
                 or field_prompt or clean(node.get('name')) or 'Question')
        tag = node.tag
        kind = (node.get('type') or 'text').lower()
        options = None
        if tag == 'select':
            qtype = 'single_select' if node.get('multiple') is None else 'multi_select'
            options = [{'value': clean(o.get('value')) or clean(o.text()), 'label': clean(o.text())}
                       for o in node.select('option') if clean(o.text()) and o.get('disabled') is None
                       and clean(o.get('value')) != '']
        elif tag == 'textarea' or node.get('contenteditable') is not None:
            qtype = 'textarea' if tag == 'textarea' else 'text'
        elif kind == 'number':
            qtype = 'number'
        elif kind == 'date':
            qtype = 'date'
        else:
            qtype = 'text'
        ident = node.get('id') or node.get('name') or ''
        questions.append({
            'qid': qid_for(tag, ident, label), 'label': clean(label, 500), 'type': qtype,
            'required': node.get('required') is not None or node.get('aria-required') == 'true',
            'options': options, 'max_length': _count(node.get('maxlength')),
            'min': _num(node.get('min')), 'max': _num(node.get('max')),
            '_locator': {'kind': 'contenteditable' if node.get('contenteditable') is not None else tag,
                         'id': node.get('id'), 'name': node.get('name')},
        })
    return questions


def _matches_simple(node: Node, selector: str) -> bool:
    chains = [_parse_complex(group) for group in _split_top(selector, ',')]
    return any(_matches(node, chain, None) for chain in chains)  # type: ignore[arg-type]


def _legend(node: Node) -> str | None:
    current = node.parent
    while current is not None:
        if current.tag == 'fieldset':
            legend = current.select_one('legend')
            if legend is not None:
                return clean(legend.text())
        label = current.get('aria-label')
        if label and current.get('role') in ('radiogroup', 'group'):
            return clean(label)
        current = current.parent
    return None


def public_questions(questions: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in q.items() if not k.startswith('_')} for q in questions]


def check_answers(questions: Sequence[Mapping[str, Any]], answers: Sequence[Mapping[str, Any]]) -> list[str]:
    """Problems with answers (empty list = usable). Select values must equal an option label exactly."""
    problems = []
    by_id = {q['qid']: q for q in questions}
    given = {}
    for answer in answers:
        qid = answer.get('qid')
        if qid not in by_id:
            problems.append('unknown question id %s' % qid)
            continue
        given[qid] = answer.get('value')
    for qid, question in by_id.items():
        value = given.get(qid)
        if value in (None, '', []):
            if question.get('required'):
                problems.append('%s is required' % qid)
            continue
        labels = [o['label'] for o in question.get('options') or ()]
        if question['type'] == 'single_select':
            if not isinstance(value, str) or value not in labels:
                problems.append('%s must be exactly one of the option labels' % qid)
        elif question['type'] == 'multi_select':
            values = value if isinstance(value, list) else [value]
            if not values or any(not isinstance(v, str) or v not in labels for v in values):
                problems.append('%s must be a list of option labels' % qid)
        elif question['type'] == 'boolean':
            if not isinstance(value, bool):
                problems.append('%s must be true or false' % qid)
        elif question['type'] == 'number':
            number = _num(value)
            if number is None or (question.get('min') is not None and number < question['min']) or \
                    (question.get('max') is not None and number > question['max']):
                problems.append('%s must be a number in range' % qid)
        else:
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                problems.append('%s must be text' % qid)
            elif question.get('max_length') and len(str(value)) > question['max_length']:
                problems.append('%s is longer than %d characters' % (qid, question['max_length']))
    return problems
