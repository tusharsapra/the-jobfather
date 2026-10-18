#!/usr/bin/env python3
"""Check The Jobfather's package files without external dependencies.

    python3 dev/validate_package.py            # full check
    python3 dev/validate_package.py --quick    # fast subset (pre-commit hook)
    python3 dev/validate_package.py --release  # also require SHA-pinned MCP sources
"""
import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / 'plugins' / 'the-jobfather'
SKILL = PLUGIN / 'skills' / 'the-jobfather'
PACKAGE = REPO / 'packages' / 'jobfather-mcp'
MCP_FILES = (PLUGIN / '.mcp.json', PLUGIN / '.codex-mcp.json', PLUGIN / 'mcp_config.json')
MANIFESTS = (PLUGIN / '.claude-plugin' / 'plugin.json', PLUGIN / '.codex-plugin' / 'plugin.json',
             PLUGIN / '.cursor-plugin' / 'plugin.json', PLUGIN / 'plugin.json')
MARKETPLACES = (REPO / '.claude-plugin' / 'marketplace.json', REPO / '.agents' / 'plugins' / 'marketplace.json',
                REPO / '.cursor-plugin' / 'marketplace.json')
NAUKRI_TOOLS = ('naukri_status', 'naukri_login', 'naukri_search_jobs', 'naukri_recommended_jobs', 'naukri_get_job',
                'naukri_similar_jobs', 'naukri_saved_jobs', 'naukri_my_profile', 'naukri_prepare_apply',
                'naukri_apply', 'naukri_close')
PLAYWRIGHT_TOOLS = ('browser_close', 'browser_resize', 'browser_console_messages', 'browser_handle_dialog',
                    'browser_evaluate', 'browser_file_upload', 'browser_fill_form', 'browser_install',
                    'browser_press_key', 'browser_type', 'browser_navigate', 'browser_navigate_back',
                    'browser_network_requests', 'browser_take_screenshot', 'browser_snapshot', 'browser_click',
                    'browser_drag', 'browser_hover', 'browser_select_option', 'browser_tabs', 'browser_wait_for')
RESERVED_PREFIXES = ('claude-', 'anthropic-', 'anthropics-', 'cc-plugin-')
ALLOWED_BINARIES = {'.svg'}
errors, warnings = [], []


def fail(message):
    errors.append(message)


def strict_json(path):
    def hook(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            fail('%s: duplicate keys' % path.relative_to(REPO))
        return dict(pairs)
    try:
        return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=hook)
    except (OSError, ValueError) as error:
        fail('%s: %s' % (path.relative_to(REPO), error))
        return {}


def frontmatter(text):
    m = re.match(r'\A---\n(.*?)\n---\n', text, re.S)
    if not m:
        return {}
    data, key = {}, None
    for line in m.group(1).splitlines():
        kv = re.match(r'^([a-z_]+):\s*(.*)$', line)
        if kv:
            key = kv.group(1)
            data[key] = kv.group(2).strip()
        elif key and line.startswith('  '):
            data[key] += ' ' + line.strip()
    return data


def package_version():
    text = (PACKAGE / 'pyproject.toml').read_text(encoding='utf-8') if (PACKAGE / 'pyproject.toml').exists() else ''
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else None


def check_skill():
    skill_files = [p for p in REPO.rglob('SKILL.md') if '.git' not in p.parts]
    if skill_files != [SKILL / 'SKILL.md']:
        fail('exactly one SKILL.md is allowed, at %s; found %s' % (SKILL.relative_to(REPO), [str(p.relative_to(REPO)) for p in skill_files]))
    meta = frontmatter((SKILL / 'SKILL.md').read_text(encoding='utf-8'))
    if meta.get('name') != 'the-jobfather':
        fail('SKILL.md name must be the-jobfather')
    if not meta.get('description') or len(meta['description']) > 1024:
        fail('SKILL.md description must be 1-1024 characters')
    for forbidden in (PLUGIN / 'bin', PLUGIN / 'CLAUDE.md'):
        if forbidden.exists():
            fail('%s must not exist (it blocks some installs)' % forbidden.relative_to(REPO))


def check_versions():
    versions = {}
    for path in MANIFESTS:
        versions[str(path.relative_to(REPO))] = strict_json(path).get('version')
    pkg = package_version()
    if pkg:
        versions['packages/jobfather-mcp/pyproject.toml'] = pkg
        init = (PACKAGE / 'src' / 'jobfather_mcp' / '__init__.py')
        if init.exists():
            m = re.search(r'__version__\s*=\s*"([^"]+)"', init.read_text(encoding='utf-8'))
            versions['jobfather_mcp/__init__.py'] = m.group(1) if m else None
    head = re.search(r'(?m)^## (\d+\.\d+\.\d+)', (REPO / 'CHANGELOG.md').read_text(encoding='utf-8'))
    versions['CHANGELOG.md'] = head.group(1) if head else None
    if len(set(versions.values())) != 1:
        fail('versions differ: %s' % versions)
    return next(iter(versions.values()))


def check_mcp(version, release=False):
    sources = set()
    for path in MCP_FILES:
        data = strict_json(path)
        text = path.read_text(encoding='utf-8')
        for bad in ('/Users/', '/home/', 'C:\\', '${', '~/', '@latest'):
            if bad in text:
                fail('%s contains %r (MCP files must be path-free and pinned)' % (path.name, bad))
        for name, server in data.get('mcpServers', {}).items():
            if server.get('command') not in ('uvx', 'npx'):
                fail('%s: server %s must run with uvx or npx' % (path.name, name))
            args = server.get('args', [])
            if '--from' in args:
                sources.add(args[args.index('--from') + 1])
    connect = SKILL / 'scripts' / 'connect.py'
    if connect.exists():
        fallback = re.search(r"FALLBACK_SOURCE = '([^']+)'", connect.read_text(encoding='utf-8'))
        if fallback:
            sources.add(fallback.group(1))
    if len(sources) != 1:
        fail('all MCP files and connect.py FALLBACK_SOURCE must use one identical source: %s' % sorted(sources))
    for source in sources:
        m = re.match(r'^git\+https://github\.com/tusharsapra/the-jobfather@([0-9a-f]{40}|v\d+\.\d+\.\d+)#subdirectory=packages/jobfather-mcp$', source)
        if not m:
            fail('MCP source must be pinned to a 40-hex commit or vX.Y.Z tag: %s' % source)
        elif release and not re.fullmatch(r'[0-9a-f]{40}', m.group(1)):
            fail('release builds must pin MCP sources to a commit SHA')
        elif m.group(1).startswith('v') and m.group(1)[1:] != version:
            fail('MCP tag pin %s does not match version %s' % (m.group(1), version))
    shipped = {path.name: any('jobfather-naukri' in server.get('args', [])
                              for server in strict_json(path).get('mcpServers', {}).values()) for path in MCP_FILES}
    shipped['pyproject.toml'] = 'jobfather-naukri' in (PACKAGE / 'pyproject.toml').read_text(encoding='utf-8')
    if len(set(shipped.values())) != 1:
        fail('the Naukri server must be in every MCP file and the package entry points, or in none: %s' % shipped)
    if not shipped['pyproject.toml']:
        return  # this release does not ship Naukri
    codex = strict_json(PLUGIN / '.codex-mcp.json').get('mcpServers', {}).get('jobfather_naukri', {})
    if tuple(sorted(codex.get('enabled_tools', []))) != tuple(sorted(NAUKRI_TOOLS)):
        fail('.codex-mcp.json enabled_tools must equal the Naukri tool list')
    if codex.get('tools', {}).get('naukri_apply', {}).get('approval_mode') != 'prompt':
        fail('.codex-mcp.json must set naukri_apply approval_mode "prompt"')
    try:
        sys.path.insert(0, str(PACKAGE / 'src'))
        from jobfather_mcp.naukri import TOOL_NAMES  # noqa: E402
        if tuple(sorted(TOOL_NAMES)) != tuple(sorted(NAUKRI_TOOLS)):
            fail('Naukri server TOOL_NAMES differ from the validated tool list')
    except Exception:  # package not built yet or deps missing; the constant list still applies
        pass


def check_tool_names():
    names = []
    for server, tools in (('browser', PLAYWRIGHT_TOOLS), ('naukri', NAUKRI_TOOLS)):
        names += ['mcp__plugin_the-jobfather_%s__%s' % (server, t) for t in tools]
        names += ['mcp__jobfather-%s__%s' % (server, t) for t in tools]
        names += ['mcp__jobfather_%s__%s' % (server, t) for t in tools]
        names += ['the-jobfather_%s_%s' % (server, t) for t in tools]
    long = [n for n in names if len(n) > 64]
    if long:
        fail('tool names over 64 characters: %s' % long[:5])


def check_manifests():
    for path in MANIFESTS:
        data = strict_json(path)
        name = data.get('name', '')
        if name != 'the-jobfather' or name.startswith(RESERVED_PREFIXES):
            fail('%s: name must be the-jobfather' % path.relative_to(REPO))
        if data.get('license') != 'MIT':
            fail('%s: license must be MIT' % path.relative_to(REPO))
        author = data.get('author', {})
        if author.get('name') != 'tusharsapra' or set(author) - {'name', 'url'}:
            fail('%s: author must be {name: tusharsapra[, url]} only' % path.relative_to(REPO))
        for key in ('homepage', 'repository'):
            if key in data and not str(data[key]).startswith('https://github.com/tusharsapra/the-jobfather'):
                fail('%s: %s must point at the repository' % (path.relative_to(REPO), key))
    for path in MARKETPLACES:
        data = strict_json(path)
        for plugin in data.get('plugins', []):
            source = plugin.get('source')
            rel = source.get('path') if isinstance(source, dict) else source
            if not rel or not (REPO / rel).exists():  # marketplace sources are relative to the repo root
                fail('%s: plugin source does not resolve: %s' % (path.relative_to(REPO), rel))


def check_third_party():
    text = (REPO / 'THIRD-PARTY.md').read_text(encoding='utf-8')
    for needle in ('blader/humanizer', 'ai-job-search', '@playwright/mcp', 'mcp-server-linkedin'):
        if needle not in text:
            fail('THIRD-PARTY.md must mention %s' % needle)
    if not (SKILL / 'third_party' / 'humanizer' / 'LICENSE').exists():
        fail('vendored humanizer LICENSE missing')


def check_python():
    for path in sorted((SKILL / 'scripts').rglob('*.py')):
        text = path.read_text(encoding='utf-8')
        if re.search(r'requires-python\s*=\s*">=3\.1\d', text):
            continue
        try:
            ast.parse(text, str(path), feature_version=(3, 9))
        except SyntaxError as error:
            fail('%s is not Python 3.9 compatible: %s' % (path.relative_to(REPO), error))


def check_links():
    for md in sorted(REPO.rglob('*.md')):
        if '.git' in md.parts or 'third_party' in md.parts:
            continue
        for target in re.findall(r'\]\(([^)#\s]+)(?:#[^)]*)?\)', md.read_text(encoding='utf-8')):
            if target.startswith(('http://', 'https://', 'mailto:')):
                continue
            if not (md.parent / target).exists():
                fail('%s: broken link %s' % (md.relative_to(REPO), target))


def check_files():
    try:
        out = subprocess.run(['git', '-C', str(REPO), 'ls-files', '-co', '--exclude-standard', '-z'],
                             capture_output=True, check=True).stdout.decode()
    except (OSError, subprocess.CalledProcessError):
        return
    for rel in filter(None, out.split('\0')):
        path = REPO / rel
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size > 1_000_000:
            fail('%s is larger than 1 MB' % rel)
        elif size > 500_000:
            warnings.append('%s is larger than 500 KB' % rel)
        data = path.read_bytes()[:8192]
        if b'\0' in data and path.suffix not in ALLOWED_BINARIES:
            fail('%s looks binary; binaries are not allowed' % rel)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--quick', action='store_true')
    ap.add_argument('--release', action='store_true')
    args = ap.parse_args()
    check_skill()
    version = check_versions()
    check_mcp(version, release=args.release)
    check_manifests()
    if not args.quick:
        check_tool_names()
        check_third_party()
        check_python()
        check_links()
        check_files()
    for message in warnings:
        print('WARN ' + message)
    for message in errors:
        print('ERROR ' + message)
    print('validate_package: %d error(s), %d warning(s)' % (len(errors), len(warnings)), file=sys.stderr)
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
