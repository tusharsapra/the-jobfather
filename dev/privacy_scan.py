#!/usr/bin/env python3
"""Scan The Jobfather repository for personal data before it can be committed or pushed.

Stdlib only. Two layers:
  * generic detectors (emails, phones, home paths, profile URLs, hostnames, secrets,
    binaries and their metadata, git identity) that run everywhere, including CI;
  * an optional private denylist kept OUTSIDE the repository
    (default ~/.config/the-jobfather-dev/denylist.txt) with the maintainer's own
    names, contacts, employers and other identifying phrases.

Exit status: 0 = clean, 1 = findings, 2 = usage or environment error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
import zlib
from pathlib import Path

VERSION = '1'
REPO = Path(__file__).resolve().parent.parent
DEFAULT_DENYLIST = Path(os.environ.get('JOBFATHER_DENYLIST', '~/.config/the-jobfather-dev/denylist.txt')).expanduser()
ALLOW_FILE = REPO / 'dev' / 'privacy-allow.json'
TAINT_FILE = REPO / 'dev' / 'taint-hashes.txt'
AUTHOR_NAME = 'tusharsapra'
AUTHOR_EMAIL = '66330793+tusharsapra@users.noreply.github.com'
ALLOWED_TRAILER_EMAILS = {AUTHOR_EMAIL, 'noreply@anthropic.com'}

BINARY_EXT = {
    '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.heic', '.zip', '.xlsx', '.xls', '.docx', '.doc',
    '.pptx', '.key', '.numbers', '.pages', '.sqlite', '.db', '.har', '.eml', '.msg', '.mbox', '.tar',
    '.gz', '.tgz', '.7z', '.rar', '.ico', '.woff', '.woff2', '.ttf', '.otf', '.pyc',
}
WARN_BYTES = 500_000
FAIL_BYTES = 2_000_000

# ---------------------------------------------------------------- generic detectors
ALLOWED_EMAIL_DOMAINS = (
    'example.com', 'example.org', 'example.net', 'example.test', 'users.noreply.github.com',
    'noreply.github.com',
)
EMAIL = re.compile(r'(?<![\w.%+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})(?![\w-])')
PHONES = (
    ('phone-e164', re.compile(r'(?<![\w+])\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,5}){2,4}(?!\d)')),
    ('phone-in', re.compile(r'(?<![\d+])(?:91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)')),
    ('phone-nanp', re.compile(r'(?<!\d)\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)')),
)
FICTIONAL_PHONE = re.compile(r'555[\s.-]?01\d\d(?!\d)')
HOME_PATHS = (
    re.compile(r'(?<![\w.~-])/Users/(?!Shared\b|runner\b|you\b|username\b|name\b)[A-Za-z][A-Za-z0-9._-]*'),
    re.compile(r'(?<![\w.~-])/home/(?!runner\b|user\b|you\b|username\b|name\b)[A-Za-z][A-Za-z0-9._-]*'),
    re.compile(r'(?<![\w.~-])[A-Za-z]:\\Users\\(?!Public\b|you\b|username\b|name\b|runneradmin\b)[A-Za-z][A-Za-z0-9._-]*'),
)
PROFILE_URLS = re.compile(r'(?:linkedin\.com/in/(?!example\b|your-profile\b|you\b|username\b)[A-Za-z0-9_%-]+|lnkd\.in/[A-Za-z0-9]+)', re.I)
HOSTNAMES = re.compile(r"\b[A-Za-z][A-Za-z0-9]*[-'’]?s?-(?:MacBook|MBP|iMac|Mac-mini|Mac-Studio)[A-Za-z0-9-]*\b|\bDESKTOP-[A-Z0-9]{7}\b", re.I)
SECRETS = (
    ('secret-github', re.compile(r'\bgh[pousr]_[A-Za-z0-9]{36,}\b')),
    ('secret-google', re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b')),
    ('secret-google-oauth', re.compile(r'\bGOCSPX-[A-Za-z0-9_-]{20,}')),
    ('secret-private-key', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ('secret-aws', re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ('secret-slack', re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}')),
    ('secret-jwt', re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}')),
    ('secret-assignment', re.compile(r'(?i)\b(?:api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*[\'"][^\'"\s]{12,}[\'"]')),
)
QUERY_PII = re.compile(r'[?&](?:email|phone|mobile)=(?!example|you|<)[^&\s"\']+', re.I)
TRAILER = re.compile(r'(?im)^co-authored-by:\s*.*<([^>]+)>\s*$')
ZERO_WIDTH = dict.fromkeys(map(ord, '​‌‍⁠﻿'), None)


def normalize(text: str) -> str:
    text = unicodedata.normalize('NFKC', text).translate(ZERO_WIDTH).casefold()
    text = re.sub(r'\s*[\[(]\s*at\s*[\])]\s*', '@', text)
    text = re.sub(r'\s*[\[(]\s*dot\s*[\])]\s*', '.', text)
    return text


# ---------------------------------------------------------------- denylist
class Denylist:
    def __init__(self, path: Path | None):
        self.path = path
        self.patterns: list[tuple[str, re.Pattern]] = []
        self.digits: list[str] = []
        self.allow: set[str] = set()
        self.loaded = False
        if path and path.exists():
            self._load(path)

    def _load(self, path: Path) -> None:
        for raw in path.read_text(encoding='utf-8').splitlines():
            if not raw.strip() or raw.startswith('#'):
                continue
            parts = raw.split('\t')
            if len(parts) != 3:
                continue
            kind, match, value = (p.strip() for p in parts)
            if not value:
                continue
            if kind == 'allow':
                self.allow.add(normalize(value))
                continue
            if match == 'digits':
                digits = re.sub(r'\D', '', value)
                if len(digits) >= 7:
                    self.digits.append(digits[-10:])
                continue
            norm = normalize(value)
            if match in ('phrase', 'word'):
                tokens = re.findall(r'[^\W_]+', norm)
                if not tokens:
                    continue
                body = r'[\W_]*'.join(re.escape(t) for t in tokens) if match == 'phrase' else re.escape(norm)
                pattern = re.compile(r'(?<![^\W_])' + body + r'(?![^\W_])')
            else:  # exact, domain, prefix: case-insensitive substring
                pattern = re.compile(re.escape(norm))
            self.patterns.append((kind, pattern))
        self.loaded = True

    def scan(self, text: str):
        norm = normalize(text)
        for kind, pattern in self.patterns:
            for m in pattern.finditer(norm):
                window = norm[max(0, m.start() - 40):m.end() + 40]
                if any(a in window and m.group(0) in a for a in self.allow):
                    continue
                yield 'denylist-' + kind, m.start()
        if self.digits:
            for m in re.finditer(r'\+?\d[\d\s().-]{5,}\d', norm):
                digits = re.sub(r'\D', '', m.group(0))
                if any(len(digits) >= len(d) and digits[-len(d):] == d for d in self.digits):
                    yield 'denylist-phone', m.start()


# ---------------------------------------------------------------- findings
class Finding:
    __slots__ = ('rule', 'where', 'line', 'excerpt', 'level')

    def __init__(self, rule, where, line, excerpt, level='error'):
        self.rule, self.where, self.line, self.excerpt, self.level = rule, where, line, excerpt, level

    def render(self, redact: bool) -> str:
        loc = '%s:%s' % (self.where, self.line) if self.line else self.where
        text = '' if redact or not self.excerpt else '  %s' % self.excerpt
        return '%s %s %s%s' % (self.level.upper(), self.rule, loc, text)

    def key(self):
        return (self.rule, self.where, self.line)


def load_allow():
    if not ALLOW_FILE.exists():
        return {'findings': [], 'binaries': []}
    data = json.loads(ALLOW_FILE.read_text(encoding='utf-8'))
    data.setdefault('findings', [])
    data.setdefault('binaries', [])
    return data


def load_taint():
    if not TAINT_FILE.exists():
        return set()
    return {line.split()[0] for line in TAINT_FILE.read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.startswith('#')}


def line_of(text: str, index: int) -> int:
    return text.count('\n', 0, index) + 1


def excerpt(text: str, index: int, length: int = 0) -> str:
    start = max(0, index - 20)
    snippet = text[start:index + max(length, 1) + 20].replace('\n', ' ')
    return snippet.strip()[:90]


class Scanner:
    def __init__(self, denylist: Denylist, allow: dict, taint: set[str]):
        self.denylist = denylist
        self.allow = allow
        self.taint = taint
        self.findings: list[Finding] = []

    def allowed(self, rule: str, where: str, matched: str) -> bool:
        digest = hashlib.sha256(matched.encode('utf-8')).hexdigest()
        for item in self.allow['findings']:
            if item.get('rule') in (rule, '*') and where.endswith(item.get('path', '\0')) and item.get('match_sha256') in (digest, '*'):
                return True
        return False

    def add(self, rule, where, text, index, length=0, level='error'):
        matched = text[index:index + length] if length else ''
        if self.allowed(rule, where, matched):
            return
        self.findings.append(Finding(rule, where, line_of(text, index) if text else 0, excerpt(text, index, length) if text else '', level))

    # ---- text
    def scan_text(self, where: str, text: str) -> None:
        for m in EMAIL.finditer(text):
            local, domain = m.group(1).lower(), m.group(2).lower()
            if domain.endswith(ALLOWED_EMAIL_DOMAINS) or domain.endswith('.example') or local in ('noreply', 'no-reply'):
                continue
            if local == 'git' and domain == 'github.com':
                continue
            if local == 'noreply' or m.group(0).lower() == 'noreply@anthropic.com':
                continue
            self.add('email', where, text, m.start(), len(m.group(0)))
        for rule, pattern in PHONES:
            for m in pattern.finditer(text):
                value = m.group(0)
                if FICTIONAL_PHONE.search(value):
                    continue
                if rule != 'phone-in' and re.fullmatch(r'[\d.]+', value.strip()):
                    continue  # bare dotted numbers are versions, not phones
                self.add(rule, where, text, m.start(), len(value))
        for pattern in HOME_PATHS:
            for m in pattern.finditer(text):
                self.add('home-path', where, text, m.start(), len(m.group(0)))
        for m in PROFILE_URLS.finditer(text):
            self.add('profile-url', where, text, m.start(), len(m.group(0)))
        for m in HOSTNAMES.finditer(text):
            self.add('hostname', where, text, m.start(), len(m.group(0)))
        for rule, pattern in SECRETS:
            for m in pattern.finditer(text):
                self.add(rule, where, text, m.start(), len(m.group(0)))
        for m in QUERY_PII.finditer(text):
            self.add('query-pii', where, text, m.start(), len(m.group(0)))
        if self.taint:
            for m in re.finditer(r'[A-Za-z_][A-Za-z0-9_]{3,}', text):
                if hashlib.sha256(m.group(0).casefold().encode()).hexdigest() in self.taint:
                    self.add('taint', where, text, m.start(), len(m.group(0)))
        for rule, index in self.denylist.scan(text):
            self.add(rule, where, text, index, 0)

    # ---- names
    def scan_name(self, where: str) -> None:
        for rule, _ in self.denylist.scan(where.replace('/', ' / ')):
            self.findings.append(Finding(rule + '-filename', where, 0, ''))

    # ---- blobs
    def scan_blob(self, where: str, data: bytes) -> None:
        self.scan_name(where)
        size = len(data)
        if size > FAIL_BYTES:
            self.findings.append(Finding('size', where, 0, '%d bytes' % size))
            return
        if size > WARN_BYTES:
            self.findings.append(Finding('size', where, 0, '%d bytes' % size, level='warn'))
        ext = os.path.splitext(where)[1].lower()
        binary = ext in BINARY_EXT or b'\0' in data[:8192]
        if binary:
            digest = hashlib.sha256(data).hexdigest()
            pinned = any(b.get('path') and where.endswith(b['path']) and b.get('sha256') == digest for b in self.allow['binaries'])
            if not pinned:
                self.findings.append(Finding('binary', where, 0, '%s, %d bytes' % (ext or 'no extension', size)))
            for label, text in binary_texts(where, data):
                self.scan_text('%s[%s]' % (where, label), text)
            return
        self.scan_text(where, data.decode('utf-8', errors='replace'))


# ---------------------------------------------------------------- binary text extraction
def binary_texts(where: str, data: bytes):
    ext = os.path.splitext(where)[1].lower()
    if ext == '.pdf':
        yield from pdf_texts(data)
    elif ext in ('.docx', '.xlsx', '.pptx', '.zip'):
        yield from zip_texts(data)
    elif ext == '.png':
        yield from png_texts(data)
    elif ext in ('.jpg', '.jpeg'):
        yield 'exif', data[:65536].decode('latin-1', errors='replace')
    else:
        yield 'raw', data.decode('latin-1', errors='replace')


def pdf_texts(data: bytes):
    yield 'raw', data.decode('latin-1', errors='replace')
    for m in re.finditer(rb'stream\r?\n(.*?)\r?\nendstream', data, re.S):
        try:
            yield 'stream', zlib.decompress(m.group(1)).decode('latin-1', errors='replace')
        except zlib.error:
            continue


def zip_texts(data: bytes):
    import io
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                if name.endswith(('.xml', '.rels', '.txt', '.json', '.md', '.csv')):
                    yield name, z.read(name).decode('utf-8', errors='replace')
    except zipfile.BadZipFile:
        yield 'raw', data.decode('latin-1', errors='replace')


def png_texts(data: bytes):
    pos = 8
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], 'big')
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if kind in (b'tEXt', b'iTXt'):
            yield kind.decode(), body.decode('latin-1', errors='replace')
        elif kind == b'zTXt':
            try:
                yield 'zTXt', zlib.decompress(body.split(b'\0', 1)[1][1:]).decode('latin-1', errors='replace')
            except (zlib.error, IndexError):
                pass
        pos += 12 + length


# ---------------------------------------------------------------- git helpers
def git(*args, input_bytes=None) -> bytes:
    return subprocess.run(['git', '-C', str(REPO)] + list(args), input=input_bytes,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout


def working_tree_files():
    out = git('ls-files', '-co', '--exclude-standard', '-z')
    return [p for p in out.decode('utf-8').split('\0') if p]


def staged_files():
    out = git('diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z')
    return [p for p in out.decode('utf-8').split('\0') if p]


def read_blobs(shas):
    """Yield (sha, bytes) for each blob sha using one cat-file process."""
    if not shas:
        return
    proc = subprocess.Popen(['git', '-C', str(REPO), 'cat-file', '--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for sha in shas:
            proc.stdin.write(sha.encode() + b'\n')
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            if len(header) < 3 or header[1] != b'blob':
                if len(header) >= 3:
                    proc.stdout.read(int(header[2]) + 1)
                continue
            size = int(header[2])
            body = proc.stdout.read(size)
            proc.stdout.read(1)
            yield sha, body
    finally:
        proc.stdin.close()
        proc.wait()


def history_blobs(rev_args):
    out = git('rev-list', '--objects', *rev_args).decode('utf-8', errors='replace')
    names = {}
    for line in out.splitlines():
        parts = line.split(' ', 1)
        if len(parts) == 2:
            names.setdefault(parts[0], parts[1])
    if not names:
        return []
    check = git('cat-file', '--batch-check', input_bytes='\n'.join(names).encode() + b'\n').decode()
    blobs = [line.split()[0] for line in check.splitlines() if len(line.split()) >= 2 and line.split()[1] == 'blob']
    return [(sha, names[sha]) for sha in blobs]


def commit_metadata(rev_args):
    fmt = '%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B%x1e'
    out = git('log', '--format=' + fmt, *rev_args).decode('utf-8', errors='replace')
    for record in out.split('\x1e'):
        record = record.strip('\n')
        if not record:
            continue
        sha, an, ae, cn, ce, body = (record.split('\x00') + [''] * 6)[:6]
        yield sha, an, ae, cn, ce, body


def scan_commits(scanner: Scanner, rev_args) -> None:
    for sha, an, ae, cn, ce, body in commit_metadata(rev_args):
        where = 'commit %s' % sha[:12]
        for who, name, email in (('author', an, ae), ('committer', cn, ce)):
            if (name, email) != (AUTHOR_NAME, AUTHOR_EMAIL) and not os.environ.get('JOBFATHER_CONTRIBUTOR'):
                scanner.findings.append(Finding('git-identity', where, 0, '%s is not the project identity' % who))
        for email in TRAILER.findall(body):
            if email not in ALLOWED_TRAILER_EMAILS:
                scanner.findings.append(Finding('git-trailer', where, 0, 'co-author email not allowed'))
        scanner.scan_text(where + ' message', body + '\n' + an + ' ' + cn)


def push_revs(stdin_text: str):
    revs = []
    zero = '0' * 40
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) != 4 or parts[1] == zero:
            continue
        local_sha, remote_sha = parts[1], parts[3]
        revs.append([local_sha, '--not', '--remotes'] if remote_sha == zero else ['%s..%s' % (remote_sha, local_sha)])
    return revs


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--working-tree', action='store_true', help='tracked and untracked, not ignored (default)')
    mode.add_argument('--staged', action='store_true', help='the index (pre-commit)')
    mode.add_argument('--all-history', action='store_true', help='every blob and commit in history')
    mode.add_argument('--push-range', action='store_true', help='commits being pushed (pre-push stdin)')
    mode.add_argument('--paths', nargs='+', help='explicit files')
    mode.add_argument('--commit-msg', metavar='FILE', help='a commit message file')
    ap.add_argument('--denylist', type=Path, default=None, help='private denylist (default: %s)' % DEFAULT_DENYLIST)
    ap.add_argument('--require-denylist', action='store_true', help='fail if the denylist is missing')
    ap.add_argument('--no-denylist', action='store_true', help='generic detectors only')
    ap.add_argument('--ci', action='store_true', help='CI mode: generic detectors, redacted output')
    ap.add_argument('--redact-output', action='store_true', help='never print matched text')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)

    denylist_path = None if (args.no_denylist or args.ci) else (args.denylist or DEFAULT_DENYLIST)
    if args.require_denylist and (denylist_path is None or not denylist_path.exists()):
        print('privacy_scan: denylist required but missing: %s' % denylist_path, file=sys.stderr)
        return 2
    denylist = Denylist(denylist_path)
    scanner = Scanner(denylist, load_allow(), load_taint())
    redact = args.redact_output or args.ci

    try:
        if args.paths:
            for p in args.paths:
                path = Path(p)
                files = [f for f in path.rglob('*') if f.is_file()] if path.is_dir() else [path]
                for f in files:
                    resolved = f.resolve()
                    # Inside the repo, judge the repo-relative name (the parent folders are not shipped).
                    name = resolved.relative_to(REPO).as_posix() if REPO in resolved.parents else str(f)
                    scanner.scan_blob(name, f.read_bytes())
        elif args.commit_msg:
            scanner.scan_text('commit message', Path(args.commit_msg).read_text(encoding='utf-8', errors='replace'))
        elif args.staged:
            for p in staged_files():
                scanner.scan_blob(p, git('show', ':' + p))
        elif args.all_history:
            blobs = history_blobs(['--all'])
            names = dict(blobs)
            for sha, data in read_blobs([s for s, _ in blobs]):
                scanner.scan_blob('%s@%s' % (names[sha], sha[:10]), data)
            scan_commits(scanner, ['--all'])
        elif args.push_range:
            for rev in push_revs(sys.stdin.read()):
                blobs = history_blobs(rev)
                names = dict(blobs)
                for sha, data in read_blobs([s for s, _ in blobs]):
                    scanner.scan_blob('%s@%s' % (names[sha], sha[:10]), data)
                scan_commits(scanner, rev)
        else:
            for p in working_tree_files():
                f = REPO / p
                if f.is_file() and not f.is_symlink():
                    scanner.scan_blob(p, f.read_bytes())
    except subprocess.CalledProcessError as exc:
        print('privacy_scan: git failed: %s' % exc.stderr.decode(errors='replace').strip(), file=sys.stderr)
        return 2

    seen = set()
    findings = []
    for f in scanner.findings:
        if f.key() not in seen:
            seen.add(f.key())
            findings.append(f)
    errors = [f for f in findings if f.level == 'error']
    if args.json:
        print(json.dumps({'version': VERSION, 'denylist_loaded': denylist.loaded,
                          'findings': [{'rule': f.rule, 'where': f.where, 'line': f.line, 'level': f.level}
                                       for f in findings]}, indent=2))
    else:
        for f in findings:
            print(f.render(redact))
        state = 'loaded' if denylist.loaded else 'not loaded'
        print('privacy_scan: %d error(s), %d warning(s); denylist %s' % (len(errors), len(findings) - len(errors), state),
              file=sys.stderr)
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
