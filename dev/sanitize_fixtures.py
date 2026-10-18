#!/usr/bin/env python3
"""Turn raw Naukri captures into synthetic test fixtures (keys and shapes kept, content replaced).

    python dev/sanitize_fixtures.py --capture ~/.cache/jobfather-dev/captures/<run> --scenario search
            [--step 01-search] [--name search] [--max-jobs 8] [--seed TEXT] [--dry-run]

Reads a capture written by ``dev/naukri_recorder.py`` and writes
``packages/jobfather-mcp/tests/fixtures/naukri/<name>/`` with ``responses.json`` (only the
responses the site profile matches for the scenario), ``page.html`` (when the scenario uses the
DOM) and ``meta.json``, plus ``MANIFEST.json`` (sha256 per file, sanitizer version, capture date,
site-profile version).

Rules: JSON keys and value types are kept. Free text (titles, companies, descriptions, skills) is
replaced by seeded synthetic text of the same shape; short format-bearing strings survive only
when they match an allowlisted pattern (experience ranges, salary formats, relative and ISO dates,
counts, an allowlist of city names, UI labels and enum values). Identifiers become synthetic
digits of the same length (consistent within one run, so a job id in a URL, a JSON body and the
HTML still agree). URLs are rebuilt from synthetic slugs; query values are dropped unless the
parameter name is allowlisted. Anything from a profile or user endpoint becomes
``REDACTED_<key>``. HTML loses scripts (JSON-LD is sanitized as JSON), styles and every attribute
except ``class``, ``id``, ``role``, ``aria-*`` and non-identifying ``data-*``; text nodes become
synthetic text unless allowlisted. The result is checked for leftover source text and with
``dev/privacy_scan.py``; nothing is written if either finds anything.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import html as html_lib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / 'packages' / 'jobfather-mcp' / 'tests' / 'fixtures' / 'naukri'
PROFILE = REPO / 'packages' / 'jobfather-mcp' / 'src' / 'jobfather_mcp' / 'naukri' / 'site_profile.json'
VERSION = '1'

# Which responses each scenario keeps (site-profile sections) and whether the page HTML is kept.
SCENARIOS = {
    'search': (('search',), True),
    'job': (('job', 'similar'), True),
    'similar': (('similar',), False),
    'recommended': (('recommended',), True),
    'saved': (('saved',), True),
    'profile': (('profile',), False),  # the profile page is personal: JSON keys only, every value redacted
    'login-markers': ((), True),
}
USER_ENDPOINT = re.compile(r'/mnjuser|/users?/|profile|resman|/self\b|dashboard|/me\b', re.I)
CONTACT_KEY = re.compile(r'e-?mail|mobile|phone|contact|address|dob|birth|gender|pan\b|aadhaar|password|token|'
                         r'cookie|session|username|user_?name|fullname|first_?name|last_?name', re.I)
ID_KEY = re.compile(r'(?:^|[a-z_])(?:id|ids|gid|gids|sid|uuid|guid)$|Id$|Ids$|Gid$|Gids$', re.I)
KEEP_QUERY = {'noOfResults', 'urlType', 'searchType', 'pageNo', 'sort', 'experience', 'jobAge', 'wfhType',
              'ctcFilter', 'microsite', 'brandedConsultantJd', 'amboxConsultantData', 'expJD', 'src'}
KEEP_SLUG = {'job', 'listings', 'jobs', 'in', 'to', 'years', 'year', 'careers', 'overview', 'mnjuser', 'jobapi',
             'search', 'simjobs', 'logo', 'images', 'logo_images', 'groups', 'v0', 'v1', 'v2', 'v3', 'v4', 'v5',
             'recommendedjobs', 'savedjobs', 'profile', 'homepage', 'nlogin', 'login', 'all', 'areas',
             'reviews', 'gif', 'png', 'jpg', 'html'}
# Real city names are replaced too: a maintainer's private denylist may name any city, so fixtures
# carry synthetic place names. Only these structural words survive inside replaced text.
CITIES = {'remote'}
KEEP_WORDS = {'hybrid', 'remote', 'yrs', 'years', 'lacs', 'lakhs', 'pa', 'not', 'disclosed', 'days', 'day', 'ago',
              'all', 'areas', 'few', 'hours', 'work', 'from', 'office', 'including', 'variable'}
ENUMS = {'inr', 'jp', 'crawled', 'airex', 'fulltime', 'parttime', 'grey', 'template1', 'hybrid', 'remote',
         'work from office', 'full time, permanent', 'full time', 'part time', 'permanent', 'temporary/contractual',
         'p.a.', 'any graduate', 'any postgraduate', 'true', 'false', 'y', 'n', 'jd', 'experience', 'salary',
         'location', 'default', 'control', 'sim', 'adv', 'search_by_key_loc', 'search_by_keyword',
         'search_by_location', 'company jobs', 'consultant jobs', 'mandatoryskills', 'otherskills', 'nerskills',
         'preferred', 'other', 'job details', 'about company', 'not disclosed', 'unpaid', 'ug', 'pg', 'doctorate'}
UI_LABELS = {'apply', 'posted:', 'openings:', 'applicants:', 'role:', 'industry type:', 'department:',
             'employment type:', 'role category:', 'education', 'ug:', 'pg:', 'doctorate:', 'key skills',
             'job description', 'no results found', 'sort by', 'relevance', 'date', 'login', 'register', 'save',
             'reviews', 'previous', 'next', 'continue with google', 'similar jobs', 'work mode', 'salary',
             'experience', 'freshness', 'location', 'last 1 day', 'last 3 days', 'last 7 days', 'last 15 days',
             'last 30 days', ':', ',', '|', '.', 'apply on company site', 'applied', 'register to apply',
             'login to apply', 'all filters', 'jobs', 'companies', 'services', 'for employers', 'about company',
             'job highlights', 'department', 'role', 'industry type', 'employment type', 'posted', 'openings',
             'applicants', 'yrs', 'years', 'not disclosed', 'hybrid', 'remote', 'work from office', 'save job',
             'share', 'beware of imposters!', 'report this job', 'saved jobs', 'jobs saved by you', 'no saved jobs!',
             'tap on save icon against a job to save it', 'recommended jobs', 'profile last updated',
             'preferred work location', 'uploaded on', 'resume'}
PATTERNS = [re.compile(p, re.I) for p in (
    r'\d{1,2}(?:\.\d)?\s*-\s*\d{1,2}(?:\.\d)?\s*(?:yrs?|years?)',
    r'\d{1,2}\+?\s*(?:yrs?|years?)',
    r'fresher',
    r'not disclosed',
    r'[\d.,]+\s*-\s*[\d.,]+\s*(?:lacs?|lakhs?|cr)(?:\s*(?:pa|p\.a\.?))?(?:\s*\(including variable:\s*[\d.]+%\))?',
    r'[\d.,]+\s*-\s*[\d.,]+\s*lakhs',
    r'(?:upto |up to |above |below |over )?[\d.,]+\s*(?:lakhs?|lacs?|cr|crores?)\+?(?:\s*(?:pa|p\.a\.?))?',
    r'\d{1,2}\+?\s*(?:days?|weeks?|months?)\s+ago',
    r'few hours ago|just now|today|\d{1,2}\s*hours?\s+ago|\d{1,2}d ago',
    r'\d{4}-\d{2}-\d{2}(?:[ t]\d{2}:\d{2}(?::\d{2})?)?',
    r'\d{1,2}:\d{2}\s*[ap]m',
    r'\d{1,5}\+?',
    r'\d{1,3}\s*-\s*\d{1,3}\s+of\s+\d{1,7}',
    r'\(\d{1,7}\)',
    r'\d{1,7}\s+reviews',
    r'[\d.]+',
    r'\d{1,3}\s*%',
)]
VOCAB = ('amber', 'basil', 'cedar', 'delta', 'ember', 'fable', 'garnet', 'harbor', 'indigo', 'juniper', 'kestrel',
         'lumen', 'meadow', 'nimbus', 'orchid', 'pebble', 'quartz', 'raven', 'sierra', 'tundra', 'umber', 'velvet',
         'willow', 'xenon', 'yarrow', 'zephyr', 'atlas', 'beacon', 'canyon', 'dune', 'fjord', 'grove', 'heron',
         'island', 'jasper', 'lagoon', 'maple', 'nectar', 'onyx', 'prairie', 'ripple', 'summit', 'thistle', 'valley',
         'wren', 'alder', 'bramble', 'coral', 'drift', 'flint', 'glacier', 'hazel', 'iris', 'kelp', 'lichen', 'moss')
DROP_TAGS = {'script', 'style', 'svg', 'noscript', 'template', 'iframe', 'object', 'embed', 'link', 'meta', 'head',
             'title', 'base', 'canvas', 'video', 'audio', 'picture', 'source'}
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
DESKTOP = re.compile(r'(?i)\bdesktop-(?=[a-z0-9]{7}\b)')  # avoids the scanner's PC-hostname pattern
SAFE_DATA = re.compile(r'^(?:[a-z0-9_-]{1,40}|\d{1,20})$', re.I)


def norm(text):
    return re.sub(r'\s+', ' ', html_lib.unescape(str(text))).strip()


def allowlisted(text):
    value = norm(text)
    if not value:
        return True
    low = value.lower()
    if low in ENUMS or low in UI_LABELS or low.rstrip(':') in UI_LABELS or low in CITIES:
        return True
    if len(value) > 120:
        return False
    if any(p.fullmatch(value) for p in PATTERNS):
        return True
    places = re.sub(r'^(?:hybrid|remote)\s*-\s*', '', low)
    parts = [p.strip() for p in re.split(r',(?![^()]*\))', places) if p.strip()]
    return bool(parts) and all(p in CITIES for p in parts)


class Sanitizer:
    def __init__(self, seed):
        self.key = hashlib.sha256(seed.encode('utf-8')).digest()
        self.digit_map = {}
        self.replaced = set()

    def _rng(self, *parts):
        digest = hmac.new(self.key, '\x1f'.join(str(p) for p in parts).encode('utf-8'), hashlib.sha256).digest()
        return random.Random(int.from_bytes(digest, 'big'))

    # ------------------------------------------------------------------ atoms
    def digits(self, original):
        original = str(original)
        if original not in self.digit_map:
            rng = self._rng('digits', original)
            body = ''.join(rng.choice('0123456789') for _ in range(len(original)))
            # a leading 0 (or 1 for one digit) keeps synthetic ids clear of phone-number patterns
            self.digit_map[original] = ('0' + body[1:]) if len(original) > 1 else str(rng.choice('12345'))
            self.replaced.add(original)
        return self.digit_map[original]

    def words(self, original):
        """Synthetic text with the same shape (word lengths ignored, punctuation and spacing kept)."""
        text = str(original)
        if allowlisted(text):
            return text
        if re.fullmatch(r'https?://\S+', text.strip()):
            return self.url(text.strip())
        rng = self._rng('text', norm(text))
        self.replaced.add(norm(text))
        text = re.sub(r'https?://\S+', 'https://example.com/link', text)
        text = re.sub(r'\S+@\S+', 'someone at example dot com', text)

        def word(match):
            token = match.group(0)
            if token.isdigit():
                return ''.join(rng.choice('0123456789') for _ in token) if len(token) < 5 else '0' * len(token)
            if token.lower() in KEEP_WORDS:
                return token
            choice = rng.choice(VOCAB)
            if token.isupper() and len(token) > 1:
                return choice.upper()
            if token[0].isupper():
                return choice.capitalize()
            return choice
        return re.sub(r'[A-Za-zÀ-￿]+|\d+', word, text)

    def ident(self, value):
        """An id / for attribute: identifier-like parts stay, text-like parts become synthetic."""
        if allowlisted(value):
            return value
        out = []
        for part in value.split('-'):
            if not part or allowlisted(part) or re.fullmatch(r'[a-z]{1,24}|[a-z]+(?:[A-Z][a-z0-9]*)+', part):
                out.append(part)
            elif part.isdigit():
                out.append(self.digits(part) if len(part) >= 5 else part)
            else:
                out.append(self.words(part))
        return '-'.join(out)

    def slug_token(self, token):
        low = token.lower()
        if not token or low in KEEP_SLUG:
            return token
        if token.isdigit():
            return self.digits(token) if len(token) >= 4 else token
        return self._rng('slug', low).choice(VOCAB)

    def url(self, value):
        try:
            parts = urllib.parse.urlsplit(value)
        except ValueError:
            return 'https://example.com/'
        host = parts.hostname or ''
        keep_host = host.endswith(('naukri.com', 'naukimg.com'))
        netloc = parts.netloc if keep_host else ('example.com' if parts.netloc else '')
        if parts.username or parts.password:
            netloc = host if keep_host else 'example.com'
        api = re.match(r'/(?:jobapi|cloudgateway-[\w-]+|servicegateway-[\w-]+)/', parts.path) is not None
        segments = []
        for segment in parts.path.split('/'):
            pieces = re.split(r'([-_.])', segment)
            # API paths name endpoints, not people: keep their words, still remap long numbers
            segments.append(''.join(p if p in '-_.' else
                                    (self.digits(p) if p.isdigit() and len(p) >= 4 else p) if api else self.slug_token(p)
                                    for p in pieces))
        query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k in KEEP_QUERY]
        query = [(k, v if allowlisted(v) and len(v) < 40 else '') for k, v in query]
        return urllib.parse.urlunsplit((parts.scheme if netloc else '', netloc, '/'.join(segments),
                                        urllib.parse.urlencode(query), ''))

    # ------------------------------------------------------------------ JSON
    def json(self, value, key='', user=False):
        if isinstance(value, dict):
            return {k: self.json(v, k, user) for k, v in value.items()}
        if isinstance(value, list):
            return [self.json(v, key, user) for v in value]
        if isinstance(value, bool) or value is None:
            return value
        if user or CONTACT_KEY.search(key or ''):
            if isinstance(value, str):
                self.replaced.add(norm(value))
                return 'REDACTED_%s' % (re.sub(r'[^A-Za-z0-9_]', '_', key) or 'value')
            return 0
        if isinstance(value, (int, float)):
            if ID_KEY.search(key or '') and float(value).is_integer() and value > 9:
                return int(self.digits(int(value)).lstrip('0') or '1')
            return value
        text = str(value)
        if key.startswith('@') and re.fullmatch(r'[A-Za-z]{1,40}', text):
            return text  # schema.org types such as JobPosting
        if re.fullmatch(r'\d+', text):
            return self.digits(text) if (len(text) >= 6 or (ID_KEY.search(key or '') and len(text) >= 3)) else text
        if re.match(r'^(?:https?:)?//|^/[\w-]', text) and ' ' not in text:
            return self.url(text if not text.startswith('//') else 'https:' + text)
        if ID_KEY.search(key or '') and re.fullmatch(r'[A-Za-z0-9_-]{8,}', text):
            return self._rng('token', text).choice(VOCAB) + '-' + self.digits(re.sub(r'\D', '', text) or '1' * 8)
        if '<' in text and '>' in text:
            return self.html(text)
        return self.words(text)

    # ------------------------------------------------------------------ HTML
    def html(self, markup, keep_ldjson=True):
        out = _HtmlRewriter(self, keep_ldjson)
        out.feed(markup)
        out.close()
        return ''.join(out.parts)


class _HtmlRewriter(HTMLParser):
    def __init__(self, sanitizer, keep_ldjson):
        super().__init__(convert_charrefs=True)
        self.s = sanitizer
        self.keep_ldjson = keep_ldjson
        self.parts = []
        self.drop = 0
        self.ldjson = None

    def _attrs(self, tag, attrs):
        kept = []
        for name, value in attrs:
            name = name.lower()
            value = value or ''
            if name in ('class', 'role'):
                kept.append((name, DESKTOP.sub('desktop_', value)))
            elif name == 'id' or (tag == 'label' and name == 'for'):
                kept.append((name, DESKTOP.sub('desktop_', self.s.ident(value))))
            elif name.startswith('aria-'):
                kept.append((name, value if allowlisted(value) or value in ('true', 'false') else self.s.words(value)))
            elif name.startswith('data-') and not CONTACT_KEY.search(name):
                if re.fullmatch(r'\d{5,}', value):
                    kept.append((name, self.s.digits(value)))
                elif SAFE_DATA.fullmatch(value) and re.fullmatch(r'[a-z0-9_-]+|[a-z]+(?:[A-Z][a-z0-9]*)+', value):
                    kept.append((name, value))  # identifier-like (lower case or camelCase)
                else:
                    kept.append((name, self.s.words(value)))  # data-title and similar carry visible text
            elif tag == 'input' and name in ('type', 'name', 'required', 'disabled', 'checked', 'multiple',
                                             'maxlength', 'min', 'max', 'autocomplete'):
                kept.append((name, value if re.fullmatch(r'[\w-]{0,40}', value) else ''))
            elif tag in ('select', 'textarea') and name in ('name', 'required', 'multiple', 'maxlength'):
                kept.append((name, value if re.fullmatch(r'[\w-]{0,40}', value) else ''))
            elif tag == 'script' and name == 'type':
                kept.append((name, value))
            elif name == 'contenteditable':
                kept.append((name, value))
        return ''.join(' %s="%s"' % (k, html_lib.escape(v, quote=True)) for k, v in kept)

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and self.keep_ldjson and ('type', 'application/ld+json') in [(k, (v or '').lower())
                                                                                       for k, v in attrs]:
            self.ldjson = []
            return
        if self.drop or tag in DROP_TAGS:
            if tag not in VOID:
                self.drop += 1
            return
        self.parts.append('<%s%s>' % (tag, self._attrs(tag, attrs)))

    def handle_startendtag(self, tag, attrs):
        if self.drop or tag in DROP_TAGS:
            return
        self.parts.append('<%s%s>' % (tag, self._attrs(tag, attrs)))

    def handle_endtag(self, tag):
        if self.ldjson is not None and tag == 'script':
            raw = ''.join(self.ldjson)
            self.ldjson = None
            try:
                data = self.s.json(json.loads(raw))
            except ValueError:
                return
            self.parts.append('<script type="application/ld+json">%s</script>'
                              % json.dumps(data, ensure_ascii=False).replace('</', '<\\/'))
            return
        if self.drop:
            if tag in DROP_TAGS or tag not in VOID:
                self.drop = max(0, self.drop - 1)
            return
        if tag not in VOID:
            self.parts.append('</%s>' % tag)

    def handle_data(self, data):
        if self.ldjson is not None:
            self.ldjson.append(data)
            return
        if self.drop:
            return
        if not data.strip():
            self.parts.append(data)
            return
        self.parts.append(html_lib.escape(self.s.words(data), quote=False))


# ---------------------------------------------------------------------- capture handling
def load_step(capture, step):
    folder = capture / step if step else next(p for p in sorted(capture.iterdir()) if p.is_dir())
    responses = json.loads((folder / 'responses.json').read_text(encoding='utf-8'))
    meta = json.loads((folder / 'meta.json').read_text(encoding='utf-8'))
    html_path = folder / 'page.html'
    markup = html_path.read_text(encoding='utf-8') if html_path.is_file() else None
    return folder, responses, meta, markup


def select_responses(responses, sections, profile):
    patterns = [re.compile(profile[s]['response']['url_regex']) for s in sections
                if profile.get(s, {}).get('response')]
    return [r for r in responses if 'body' in r and any(p.search(r.get('url', '')) for p in patterns)]


def trim_jobs(body, max_jobs):
    """Keep the fixture small: at most max_jobs items in every job list."""
    if isinstance(body, dict):
        return {k: (v[:max_jobs] if isinstance(v, list) and v and isinstance(v[0], dict) and 'jobId' in v[0]
                    else trim_jobs(v, max_jobs)) for k, v in body.items()}
    return body


def json_keys(value, out=None):
    out = set() if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(k)
            json_keys(v, out)
    elif isinstance(value, list):
        for v in value:
            json_keys(v, out)
    return out


def leaked(sanitizer, text, keys=()):
    """Any replaced source text (12+ characters) that still appears verbatim."""
    for original in sanitizer.replaced:
        if (not isinstance(original, str) or original in keys or allowlisted(original)
                or re.fullmatch(r'[\w-]+', original)):
            continue  # identifier-like tokens (class names, ids) are kept by design
        if (len(original) >= 16 or (len(original) >= 8 and ' ' in original)) and original in text:
            return original[:30]
    return None


def privacy_scan(paths):
    done = subprocess.run([sys.executable, str(REPO / 'dev' / 'privacy_scan.py'), '--json', '--paths'] + [str(p) for p in paths],
                          capture_output=True, text=True)
    try:
        findings = json.loads(done.stdout).get('findings', [])
    except ValueError:
        findings = [{'rule': 'scanner-failed', 'where': done.stderr[-300:]}]
    return [f for f in findings if f.get('level', 'error') == 'error']


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--capture', required=True, help='a capture folder written by naukri_recorder.py')
    ap.add_argument('--scenario', required=True, choices=sorted(SCENARIOS))
    ap.add_argument('--step', help='step folder inside the capture (default: the first)')
    ap.add_argument('--name', help='fixture folder name (default: the scenario)')
    ap.add_argument('--max-jobs', type=int, default=8)
    ap.add_argument('--seed', default=None, help='seed for synthetic text (default: random)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args(argv)

    capture = Path(args.capture).expanduser().resolve()
    profile = json.loads(PROFILE.read_text(encoding='utf-8'))
    folder, responses, meta, markup = load_step(capture, args.step)
    attempts = 1 if args.seed is not None else 4
    for attempt in range(attempts):
        outputs = build(args, profile, responses, meta, markup,
                        args.seed if args.seed is not None else os.urandom(16).hex())
        if args.dry_run:
            for name, text in outputs.items():
                print('%s: %d bytes' % (name, len(text.encode('utf-8'))))
            return 0
        findings = write(args, profile, meta, outputs)
        if not findings:
            return 0
        # A sha256 digest can contain a phone-like digit run by chance: try another seed.
        only_digests = all(f.get('rule', '').startswith('phone') and str(f.get('where', '')).endswith('MANIFEST.json')
                           for f in findings)
        if not only_digests or attempt == attempts - 1:
            for f in findings:
                print('privacy: %s %s:%s' % (f.get('rule'), Path(str(f.get('where'))).name, f.get('line')),
                      file=sys.stderr)
            raise SystemExit('sanitize_fixtures: privacy_scan found %d problem(s); nothing written' % len(findings))
    return 1


def build(args, profile, responses, meta, markup, seed):
    """Sanitized file contents {name: text}; raises SystemExit if source text survives."""
    sections, keep_html = SCENARIOS[args.scenario]
    s = Sanitizer(seed)
    kept = []
    for record in select_responses(responses, sections, profile) if sections else []:
        user = bool(USER_ENDPOINT.search(urllib.parse.urlsplit(record['url']).path)) or args.scenario == 'profile'
        kept.append({'method': record.get('method'), 'url': s.url(record['url']), 'status': record.get('status'),
                     'body': s.json(trim_jobs(record['body'], args.max_jobs), user=user)})
    outputs = {'responses.json': json.dumps(kept, indent=1, ensure_ascii=False) + '\n'}
    if keep_html and markup:
        outputs['page.html'] = s.html(markup)
    outputs['meta.json'] = json.dumps({'url': s.url(meta.get('url', '')), 'status': meta.get('status'),
                                       'checkpoint': meta.get('checkpoint')}, indent=1) + '\n'
    keys = json_keys([r['body'] for r in kept])
    for name, text in outputs.items():
        leak = leaked(s, text, keys)
        if leak:
            raise SystemExit('sanitize_fixtures: source text survived in %s (starts %r); refusing to write'
                             % (name, leak))
    return outputs


def write(args, profile, meta, outputs):
    """Stage, scan and move into place; returns privacy findings (nothing is written when there are any)."""
    target = FIXTURES / (args.name or args.scenario)
    staging = FIXTURES / ('.staging-%s' % os.getpid())
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        hashes = {}
        for name, text in outputs.items():
            data = text.encode('utf-8')
            (staging / name).write_bytes(data)
            hashes[name] = hashlib.sha256(data).hexdigest()
        captured = meta.get('captured_at', '')[:10] or datetime.now(timezone.utc).date().isoformat()
        manifest = {'schema': 'the-jobfather/naukri-fixture@1', 'scenario': args.scenario,
                    'sanitizer_version': VERSION, 'capture_date': captured, 'site_profile': profile['version'],
                    'files': hashes}
        (staging / 'MANIFEST.json').write_text(json.dumps(manifest, indent=1) + '\n', encoding='utf-8')
        findings = privacy_scan(sorted(staging.iterdir()))
        if findings:
            return findings
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print('wrote %s (%s)' % (target.relative_to(REPO), ', '.join(sorted(hashes) + ['MANIFEST.json'])))
    return []


if __name__ == '__main__':
    sys.exit(main())
