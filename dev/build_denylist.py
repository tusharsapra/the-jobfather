#!/usr/bin/env python3
"""Build the maintainer's PRIVATE denylist for dev/privacy_scan.py.

The denylist lists phrases that must never appear in the repository: your names,
contacts, employers, clients, colleagues, projects, locations, machine identity.
It is generated from files OUTSIDE the repository that you point it at (your own
profile, old CVs, notes) and written OUTSIDE any git work tree with mode 0600.

It prints counts per kind only, never values.

    python3 dev/build_denylist.py --source ~/Jobfather/profile --source ~/old-cv.tex \
        [--extra ~/.config/the-jobfather-dev/denylist-extra.txt] [--allow your-handle]

Format written (one entry per line):  kind<TAB>match<TAB>value
match is one of: phrase, word, exact, digits, domain, prefix.  kind 'allow' exempts a token.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import re
import socket
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

VERSION = '1'
DEFAULT_OUT = Path('~/.config/the-jobfather-dev/denylist.txt').expanduser()
TEXT_EXT = {'.md', '.json', '.jsonl', '.tex', '.txt', '.mjs', '.js', '.py', '.csv', '.yml', '.yaml', '.toml', '.html'}
MAX_FILE = 5_000_000

# Words that are generic in job-hunting text; an extracted phrase made only of these is dropped.
GENERIC = set('''
a an and the of for to in on at by with from as or not no yes is are be this that these those my our your their
read first education tools platforms platform internships internship freelancing freelance automation projects project
portfolio corrections resolved missing detail details summary skills skill claims claim experience evidence profile
context index job jobs search working preferences preference identity contact story bank voice status rules rule
practical desired tone medium research basis learning feedback editorial pass illustrative rewrites hr application
applications email emails preferences direct human current previous historical approved established preferred account
responsibilities depth scope additional location locations what is established cv core manager senior junior lead
associate assistant executive head director specialist analyst consultant coordinator intern trainee officer engineer
developer designer marketing digital performance media paid campaign campaigns planning planner operations ads ad
advertising brand growth social content strategy strategist client clients servicing management team teams business
account accounts sales product data analytics report reporting google meta facebook instagram linkedin youtube excel
csv sheets naukri gmail india remote hybrid onsite office relocation notice period salary compensation ctc lakh lpa
inr usd year years month months monday tuesday wednesday thursday friday saturday sunday january february march april
may june july august september october november december present today now new old full time part contract permanent
university college school institute degree bachelor master bachelors masters mba bba bcom btech mtech graduate
graduation postgraduate diploma certificate certification course courses
'''.split())
LABELS = ('name', 'full name', 'phone', 'mobile', 'email', 'e-mail', 'portfolio', 'website', 'linkedin', 'github',
          'base', 'location', 'address', 'city', 'current employer/title', 'current employer', 'employer', 'company')
JS_KEYS = ('firstname', 'lastname', 'fullname', 'name', 'email', 'phone', 'mobile', 'linkedin', 'website',
           'portfolio', 'location', 'city', 'address', 'current_company', 'currentcompany', 'company', 'school',
           'university', 'college', 'current_title', 'title')
COMMON_DOMAINS = {
    'google.com', 'linkedin.com', 'github.com', 'naukri.com', 'gmail.com', 'youtube.com', 'facebook.com',
    'instagram.com', 'twitter.com', 'x.com', 'medium.com', 'wikipedia.org', 'greenhouse.io', 'lever.co',
    'ashbyhq.com', 'workable.com', 'myworkdayjobs.com', 'icims.com', 'smartrecruiters.com', 'indeed.com',
    'glassdoor.com', 'microsoft.com', 'apple.com', 'amazon.com', 'openai.com', 'anthropic.com', 'claude.ai',
    'claude.com', 'npmjs.com', 'pypi.org', 'python.org', 'nodejs.org', 'mozilla.org', 'w3.org', 'ctan.org',
    'tug.org', 'schema.org', 'json-schema.org', 'example.com', 'example.org', 'notion.so', 'figma.com',
    'canva.com', 'adobe.com', 'hubspot.com', 'semrush.com', 'similarweb.com', 'ahrefs.com', 'forms.gle',
    'docs.google.com', 'mail.google.com', 'gstatic.com', 'googleapis.com', 'bit.ly', 'lnkd.in',
}
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')
PHONE = re.compile(r'(?<!\d)(?:\+?\d{1,3}[\s.-]?)?\(?\d{2,5}\)?(?:[\s.-]?\d{2,5}){1,3}(?!\d)')
URL = re.compile(r'https?://([A-Za-z0-9.-]+)(/[^\s)\]}>"\']*)?')
LINKEDIN = re.compile(r'linkedin\.com/in/([A-Za-z0-9_%-]+)', re.I)
GITHUB = re.compile(r'github\.com/([A-Za-z0-9-]+)', re.I)


def is_generic(phrase: str) -> bool:
    tokens = re.findall(r'[a-z0-9]+', phrase.casefold())
    return not tokens or all(t in GENERIC or t.isdigit() or len(t) <= 2 for t in tokens)


class Collector:
    def __init__(self, allow: set[str]):
        self.entries: dict[tuple[str, str], str] = {}
        self.allow = {a.casefold() for a in allow}

    def add(self, kind: str, match: str, value: str) -> None:
        value = re.sub(r'\s+', ' ', value).strip().strip('.,;:()[]{}"\'`*_')
        if not value or value.casefold() in self.allow:
            return
        folded = value.casefold()
        if any(re.search(r'github\.com/%s(?:/|$)' % re.escape(a), folded) for a in self.allow):
            return  # the public credit link is intentional
        if kind == 'location' and (len(value) > 40 or len(value.split()) > 4 or re.search(r'\.\s', value)):
            return  # sentences, not places
        if match == 'digits':
            digits = re.sub(r'\D', '', value)
            if len(digits) < 8:
                return
            value = digits[-10:]
        elif match in ('phrase', 'word'):
            if len(value) < 4 or is_generic(value):
                return
        key = (match, value.casefold())
        self.entries.setdefault(key, kind)

    def lines(self):
        for (match, folded), kind in sorted(self.entries.items(), key=lambda kv: (kv[1], kv[0])):
            yield '%s\t%s\t%s' % (kind, match, folded)


def iter_files(sources):
    for src in sources:
        path = Path(src).expanduser()
        if path.is_dir():
            for f in sorted(path.rglob('*')):
                if f.is_file() and f.suffix.lower() in TEXT_EXT and f.stat().st_size <= MAX_FILE:
                    yield f
        elif path.is_file():
            yield path
        else:
            print('build_denylist: skipping missing source (index %d)' % sources.index(src), file=sys.stderr)


def extract(text: str, c: Collector) -> None:
    for m in EMAIL.finditer(text):
        email = m.group(0)
        c.add('email', 'exact', email)
        local = email.split('@', 1)[0]
        if len(local) >= 5 and not local.lower().startswith(('noreply', 'no-reply', 'info', 'careers', 'jobs', 'hr', 'hello')):
            c.add('email', 'exact', local + '@')
    for m in PHONE.finditer(text):
        digits = re.sub(r'\D', '', m.group(0))
        if 10 <= len(digits) <= 13 and not re.fullmatch(r'(19|20)\d{6,}', digits):
            c.add('phone', 'digits', digits)
    for m in LINKEDIN.finditer(text):
        c.add('profile', 'exact', 'linkedin.com/in/' + m.group(1))
    for m in GITHUB.finditer(text):
        c.add('profile', 'exact', 'github.com/' + m.group(1) + '/')
    for m in URL.finditer(text):
        host = m.group(1).lower().strip('.')
        base = '.'.join(host.split('.')[-2:])
        if base not in COMMON_DOMAINS and host not in COMMON_DOMAINS and not base.endswith(('.example', '.test')):
            c.add('domain', 'domain', host[4:] if host.startswith('www.') else host)
    for m in re.finditer(r'(?im)^\s*[-*]\s*(%s)\s*:\s*(.+?)\s*$' % '|'.join(re.escape(l) for l in LABELS), text):
        label, value = m.group(1).lower(), m.group(2)
        value = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', value)
        for part in re.split(r'[,;|]| — | - ', value):
            if label in ('name', 'full name'):
                c.add('name', 'phrase', part)
                for token in part.split():
                    c.add('name', 'word', token)
            elif label in ('phone', 'mobile'):
                c.add('phone', 'digits', part)
            elif label in ('base', 'location', 'address', 'city'):
                c.add('location', 'word', part)
            else:
                c.add('identity', 'phrase', part)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r'^##\s+(.+?)\s*$', line)
        if not m:
            continue
        heading = re.sub(r'[`*_]', '', m.group(1))
        convention = re.match(r'^\[[A-Z][A-Z0-9]{1,7}\]\s*(.+)$', heading)
        following = [l for l in lines[i + 1:i + 6] if l.strip()][:3]
        role_line = any(re.match(r'^\*\*[^*]+\|[^*]+\*\*\s*$', l) or l.startswith('```jobfather-entry') for l in following)
        if convention:
            c.add('org', 'phrase', convention.group(1))
        elif role_line and not re.search(r'\d{4}', heading):
            c.add('org', 'phrase', heading)
    for m in re.finditer(r'(?m)^\*\*([^*\n]+\|[^*\n]+)\*\*\s*$', text):
        parts = [p.strip() for p in m.group(1).split('|')]
        if len(parts) >= 3:
            c.add('location', 'word', parts[-1])
            for part in re.split(r',\s*', parts[-1]):
                c.add('location', 'word', part)
    for m in re.finditer(r'\\resumeEntry\s*\{([^}]*)\}\s*\{[^}]*\}\s*\{[^}]*\}\s*\{([^}]*)\}', text):
        c.add('org', 'phrase', m.group(1))
        for part in re.split(r',\s*', m.group(2)):
            c.add('location', 'word', part)
    for m in re.finditer(r'\\href\{([^}]*)\}\{([^}]*)\}', text):
        c.add('identity', 'phrase', m.group(2))
    for m in re.finditer(r'(?im)["\']?(%s)["\']?\s*:\s*["\']([^"\'\n]{2,120})["\']' % '|'.join(JS_KEYS), text):
        key, value = m.group(1).lower(), m.group(2)
        if key in ('phone', 'mobile'):
            c.add('phone', 'digits', value)
        elif key in ('location', 'city', 'address'):
            c.add('location', 'word', value)
        elif key in ('firstname', 'lastname'):
            c.add('name', 'word', value)
        elif key not in ('title', 'current_title'):
            c.add('identity', 'phrase', value)


def extract_json_values(obj, c: Collector, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            extract_json_values(v, c, path + (str(k).lower(),))
    elif isinstance(obj, list):
        for v in obj:
            extract_json_values(v, c, path)
    elif isinstance(obj, str) and path:
        joined = '/'.join(path)
        if any(k in joined for k in ('location', 'city', 'address')):
            for part in re.split(r'[,;/]', obj):
                c.add('location', 'word', part)
        elif any(k in joined for k in ('name', 'owner')):
            c.add('name', 'phrase', obj)


def machine_entries(c: Collector) -> None:
    names = {socket.gethostname(), platform.node()}
    if sys.platform == 'darwin':
        for key in ('ComputerName', 'LocalHostName', 'HostName'):
            try:
                out = subprocess.run(['scutil', '--get', key], capture_output=True, text=True, timeout=5).stdout.strip()
                if out:
                    names.add(out)
            except (OSError, subprocess.SubprocessError):
                pass
    for name in names:
        name = name.split('.')[0]
        if len(name) >= 4:
            c.entries.setdefault(('exact', name.casefold()), 'host')
            c.entries.setdefault(('phrase', re.sub(r'[-_]+', ' ', name).casefold()), 'host')
    user = getpass.getuser()
    home = str(Path.home())
    c.entries.setdefault(('prefix', home.casefold()), 'path')
    for prefix in ('/users/%s' % user, '/home/%s' % user, 'c:\\users\\%s' % user):
        c.entries.setdefault(('prefix', prefix.casefold()), 'path')
    for key in ('user.name', 'user.email'):
        try:
            out = subprocess.run(['git', 'config', '--global', key], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            out = ''
        if out:
            c.add('git', 'exact' if '@' in out else 'phrase', out)


def inside_git_tree(path: Path) -> bool:
    probe = path if path.is_dir() else path.parent
    while not probe.exists():
        probe = probe.parent
    try:
        out = subprocess.run(['git', '-C', str(probe), 'rev-parse', '--is-inside-work-tree'],
                             capture_output=True, text=True, timeout=5)
    except OSError:
        return False
    return out.returncode == 0 and out.stdout.strip() == 'true'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Build the private denylist (prints counts only).')
    ap.add_argument('--source', action='append', default=[], help='file or directory outside the repo (repeatable)')
    ap.add_argument('--extra', type=Path, help='hand-written entries in denylist format (kind<TAB>match<TAB>value)')
    ap.add_argument('--allow', action='append', default=['tusharsapra', '66330793+tusharsapra@users.noreply.github.com'],
                    help='tokens that are allowed even if extracted (repeatable)')
    ap.add_argument('--out', type=Path, default=DEFAULT_OUT)
    ap.add_argument('--no-machine', action='store_true', help='skip hostname/username/home/git identity entries')
    args = ap.parse_args(argv)

    out = args.out.expanduser().resolve()
    if inside_git_tree(out):
        print('build_denylist: refusing to write inside a git work tree', file=sys.stderr)
        return 2
    c = Collector(set(args.allow))
    files = 0
    for f in iter_files(args.source):
        try:
            text = f.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        files += 1
        extract(text, c)
        if f.suffix.lower() == '.json':
            try:
                extract_json_values(json.loads(text), c)
            except ValueError:
                pass
    if not args.no_machine:
        machine_entries(c)
    lines = list(c.lines())
    if args.extra and args.extra.expanduser().exists():
        for raw in args.extra.expanduser().read_text(encoding='utf-8').splitlines():
            if raw.strip() and not raw.startswith('#') and raw.count('\t') == 2:
                lines.append(raw.strip())
    lines += ['allow\texact\t%s' % a for a in args.allow]

    out.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(out.parent, 0o700)
    header = '# the-jobfather-dev denylist v%s generated %s by build_denylist.py\n# kind\tmatch\tvalue\n' % (
        VERSION, datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
    tmp = out.with_suffix('.tmp')
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(header + '\n'.join(sorted(set(lines))) + '\n')
    os.replace(tmp, out)
    os.chmod(out, 0o600)
    counts = Counter(line.split('\t', 1)[0] for line in set(lines))
    print('build_denylist: %d source files; entries by kind: %s' % (
        files, ', '.join('%s=%d' % kv for kv in sorted(counts.items()))))
    print('build_denylist: written (mode 0600) outside the repository')
    return 0


if __name__ == '__main__':
    sys.exit(main())
