#!/usr/bin/env python3
"""Release helper. A commit cannot contain its own SHA, so a release is two commits:

    python3 dev/release.py prepare 0.2.0   # bump versions, run every check, commit A
    python3 dev/release.py pin             # pin MCP sources to A's SHA, commit B, tag v0.2.0 on B
    python3 dev/release.py verify          # the pins point at a commit with the right version
    git push --atomic origin main v0.2.0   # manual, after review

Nothing here pushes.
"""
import argparse
from datetime import date
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / 'plugins' / 'the-jobfather'
MCP_FILES = (PLUGIN / '.mcp.json', PLUGIN / '.codex-mcp.json', PLUGIN / 'mcp_config.json')
MANIFESTS = (PLUGIN / '.claude-plugin' / 'plugin.json', PLUGIN / '.codex-plugin' / 'plugin.json',
             PLUGIN / '.cursor-plugin' / 'plugin.json', PLUGIN / 'plugin.json')
CONNECT = REPO / 'plugins' / 'the-jobfather' / 'skills' / 'the-jobfather' / 'scripts' / 'connect.py'
PIN_FILES = MCP_FILES + (CONNECT,)  # connect.py's fallback serves skills-folder installs
PYPROJECT = REPO / 'packages' / 'jobfather-mcp' / 'pyproject.toml'
INIT = REPO / 'packages' / 'jobfather-mcp' / 'src' / 'jobfather_mcp' / '__init__.py'
RELEASE_FILES = MANIFESTS + (PYPROJECT, INIT, REPO / 'CHANGELOG.md') + PIN_FILES
SOURCE = re.compile(r'(git\+https://github\.com/tusharsapra/the-jobfather@)([0-9a-f]{40}|v\d+\.\d+\.\d+)(#subdirectory=packages/jobfather-mcp)')


def git(*args, capture=True):
    result = subprocess.run(['git', '-C', str(REPO)] + list(args), capture_output=capture, text=True)
    if result.returncode:
        raise SystemExit('git %s failed: %s' % (' '.join(args), (result.stderr or '').strip()))
    return (result.stdout or '').strip()


def stage(paths):
    """Stage exactly the files a release step edits, never stray work in the tree."""
    git('add', '--', *[str(Path(p).relative_to(REPO)) for p in paths])


def run(*cmd):
    print('$ ' + ' '.join(cmd))
    if subprocess.run(cmd, cwd=str(REPO)).returncode:
        raise SystemExit('check failed: ' + ' '.join(cmd))


def replace(path, pattern, repl, count=1):
    text = path.read_text(encoding='utf-8')
    new, n = re.subn(pattern, repl, text, count=count)
    if n == 0:
        raise SystemExit('could not update %s' % path.relative_to(REPO))
    path.write_text(new, encoding='utf-8')


def prepare(version):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise SystemExit('version must be X.Y.Z')
    if git('rev-parse', '--abbrev-ref', 'HEAD') != 'main':
        raise SystemExit('release from main')
    if git('status', '--porcelain', '--untracked-files=no'):
        raise SystemExit('commit or stash your changes first (untracked files are left alone)')
    for path in MANIFESTS:
        data = json.loads(path.read_text(encoding='utf-8'))
        data['version'] = version
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    replace(PYPROJECT, r'(?m)^version\s*=\s*"[^"]+"', 'version = "%s"' % version)
    replace(INIT, r'__version__\s*=\s*"[^"]+"', '__version__ = "%s"' % version)
    replace(REPO / 'CHANGELOG.md', r'(?m)^## (?:\d+\.\d+\.\d+ \(unreleased\)|Unreleased)$', '## %s (%s)' % (version, date.today().isoformat()))
    for path in PIN_FILES:
        replace(path, SOURCE.pattern, lambda m: m.group(1) + 'v' + version + m.group(3), count=0)
    run(sys.executable, '-m', 'unittest', 'discover', '-s', 'tests')
    run(sys.executable, 'dev/validate_package.py')
    run(sys.executable, 'dev/privacy_scan.py', '--all-history', '--require-denylist')
    stage(RELEASE_FILES)
    git('commit', '-q', '-m', 'Release v%s' % version)
    print('Prepared release commit %s. Next: python3 dev/release.py pin' % git('rev-parse', 'HEAD'))


def pin():
    sha = git('rev-parse', 'HEAD')
    subject = git('log', '-1', '--format=%s')
    m = re.fullmatch(r'Release v(\d+\.\d+\.\d+)', subject)
    if not m:
        raise SystemExit('HEAD must be the "Release vX.Y.Z" commit from prepare')
    for path in PIN_FILES:
        replace(path, SOURCE.pattern, lambda mm: mm.group(1) + sha + mm.group(3), count=0)
    run(sys.executable, '-m', 'unittest', 'discover', '-s', 'tests')  # pins feed plan fingerprints
    run(sys.executable, 'dev/validate_package.py', '--release')
    stage(PIN_FILES)
    git('commit', '-q', '-m', 'Pin MCP sources to %s' % sha[:12])
    git('tag', '-a', 'v' + m.group(1), '-m', 'The Jobfather v' + m.group(1))
    print('Pinned to %s and tagged v%s. Review, then: git push --atomic origin main v%s' % (sha, m.group(1), m.group(1)))


def verify():
    pins = set()
    for path in PIN_FILES:
        pins |= {m.group(2) for m in SOURCE.finditer(path.read_text(encoding='utf-8'))}
    if len(pins) != 1:
        raise SystemExit('MCP files disagree: %s' % sorted(pins))
    ref = pins.pop()
    if not re.fullmatch(r'[0-9a-f]{40}', ref):
        raise SystemExit('not SHA-pinned yet: ' + ref)
    git('cat-file', '-e', ref + '^{commit}')
    text = git('show', '%s:packages/jobfather-mcp/pyproject.toml' % ref)
    print('pinned commit %s carries %s' % (ref[:12], re.search(r'(?m)^version\s*=\s*"([^"]+)"', text).group(0)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='command', required=True)
    sub.add_parser('prepare').add_argument('version')
    sub.add_parser('pin')
    sub.add_parser('verify')
    args = ap.parse_args()
    {'prepare': lambda: prepare(args.version), 'pin': pin, 'verify': verify}[args.command]()


if __name__ == '__main__':
    main()
