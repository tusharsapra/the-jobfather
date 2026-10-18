"""Health report: Python, uv, Node, Chrome, TeX and Poppler, the workspace and saved connections.

    doctor.py [--json] [--require-pdf] [--live] [--save-tools] [--workspace PATH]

Read-only apart from a lock probe in <workspace>/.state/locks and, with --save-tools, the
machine settings file (never the workspace). Install hints are suggestions to offer the user;
nothing is installed. Exit status 0 when ready, 1 when something blocks normal use.
"""
import argparse
import glob
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

if __name__ == '__main__':
    sys.modules['doctor'] = sys.modules[__name__]

import layout
import pdf_tools
from local import (CODE_ROOT, WorkspaceError, add_workspace_arg, atomic_json, machine, machine_file, now, read_json,
                   shared_lock, workspace, workspace_candidates)

TEMPLATE = CODE_ROOT / 'templates' / 'cv' / 'classic@1' / 'template.json'
MIN_PYTHON = (3, 9)
MIN_NODE = 18
PLAYWRIGHT_MCP = '@playwright/mcp@0.0.83'
TEX_TOOLS = ('pdflatex', 'kpsewhich')
POPPLER_TOOLS = ('pdfinfo', 'pdftotext', 'pdftoppm', 'pdffonts')
EXTRA_DIRS = ('~/.local/bin', '~/.cargo/bin', '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin',
              '~/AppData/Roaming/npm', 'C:/Program Files/nodejs')
CLOUD_MARKERS = (('library/mobile documents', 'iCloud Drive'), ('icloud drive', 'iCloud Drive'),
                 ('iclouddrive', 'iCloud Drive'), ('library/cloudstorage', 'a cloud-synced folder'),
                 ('dropbox', 'Dropbox'), ('onedrive', 'OneDrive'), ('google drive', 'Google Drive'),
                 ('my drive', 'Google Drive'))
STATUS_ORDER = ('ok', 'info', 'warn', 'fail')


# ------------------------------------------------------------------ lookups (patched in tests)
def os_kind():
    if sys.platform == 'darwin':
        return 'macos'
    return 'windows' if os.name == 'nt' else 'linux'


def which(name):
    found = shutil.which(name)
    if found:
        return found
    for pattern in EXTRA_DIRS:
        for suffix in ('', '.exe', '.cmd'):
            path = Path(os.path.expanduser(pattern)) / (name + suffix)
            if path.is_file() and os.access(str(path), os.X_OK):
                return str(path)
    return None


def run(argv, timeout=20):
    """(exit code, combined output) or (None, reason)."""
    try:
        done = subprocess.run([str(a) for a in argv], capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, 'timed out after %d s' % timeout
    except OSError as error:
        return None, str(error)
    return done.returncode, (done.stdout + done.stderr).strip()


def exists(path):
    return os.path.exists(path)


# ------------------------------------------------------------------ checks
def check(check_id, title, status, detail, fix=None, **extra):
    item = {'id': check_id, 'title': title, 'status': status, 'detail': detail}
    if fix:
        item['fix'] = fix
    item.update(extra)
    return item


def install_hint(tool, kind=None):
    kind = kind or os_kind()
    hints = {
        'uv': {'macos': '`brew install uv`, or `curl -LsSf https://astral.sh/uv/install.sh | sh`',
               'linux': '`curl -LsSf https://astral.sh/uv/install.sh | sh` (or your distribution\'s uv package)',
               'windows': '`winget install --id=astral-sh.uv -e`, or `powershell -ExecutionPolicy ByPass -c "irm '
                          'https://astral.sh/uv/install.ps1 | iex"`'},
        'node': {'macos': '`brew install node`, or the LTS installer from https://nodejs.org',
                 'linux': 'your distribution\'s nodejs package (version %d or newer) or nvm' % MIN_NODE,
                 'windows': '`winget install OpenJS.NodeJS.LTS`'},
        'git': {'macos': '`xcode-select --install` (or `brew install git`)',
                'linux': 'your distribution\'s git package', 'windows': '`winget install --id Git.Git -e`'},
        'chrome': {'macos': 'Google Chrome from https://www.google.com/chrome/',
                   'linux': 'Google Chrome from https://www.google.com/chrome/ (Chromium is not enough)',
                   'windows': 'Google Chrome from https://www.google.com/chrome/ (or `winget install Google.Chrome`)'},
        'tex': {'macos': 'MacTeX (`brew install --cask mactex-no-gui`), or BasicTeX plus `sudo tlmgr install` for the '
                         'missing packages',
                'linux': 'TeX Live (Debian/Ubuntu: `sudo apt install texlive-latex-recommended texlive-latex-extra '
                         'texlive-fonts-extra`)',
                'windows': 'MiKTeX (`winget install MiKTeX.MiKTeX`); it installs missing packages on first use'},
        'poppler': {'macos': '`brew install poppler`', 'linux': '`sudo apt install poppler-utils` (or your '
                                                                'distribution\'s poppler package)',
                    'windows': 'Poppler for Windows (for example `scoop install poppler` or `choco install poppler`), '
                               'then `doctor.py --save-tools`'},
    }
    return 'Ask the user before installing anything. Suggested: %s.' % hints[tool][kind]


def check_python():
    version = '%d.%d.%d' % sys.version_info[:3]
    if sys.version_info[:2] >= MIN_PYTHON:
        return check('python', 'Python', 'ok', 'Python %s (%s)' % (version, sys.executable))
    return check('python', 'Python', 'fail', 'Python %s is too old; the scripts need 3.9 or newer' % version,
                 'Install Python 3.9+ and run the scripts with it.')


def check_uv(live=False):
    uv, uvx = which('uv'), which('uvx')
    if not uv or not uvx:
        return check('uv', 'uv (runs the MCP servers)', 'warn', 'uv/uvx not found; logins and the MCP servers need it',
                     install_hint('uv'), path=uv or uvx)
    detail = 'uvx at %s' % uvx
    if live:
        code, out = run([uv, '--version'])
        detail += '; ' + (out.splitlines()[0] if code == 0 and out else 'uv --version failed: %s' % out)
    return check('uv', 'uv (runs the MCP servers)', 'ok', detail, path=uvx)


def check_git():
    git = which('git')
    if git:
        return check('git', 'git (uv fetches the MCP package from GitHub)', 'ok', 'git at %s' % git, path=git)
    return check('git', 'git (uv fetches the MCP package from GitHub)', 'warn',
                 'git not found; uvx cannot fetch the pinned MCP package without it', install_hint('git'))


def check_node(live=False):
    node, npx = which('node'), which('npx')
    if not node or not npx:
        return check('node', 'Node.js and npx (browser automation)', 'warn', 'node/npx not found',
                     install_hint('node'), path=npx)
    code, out = run([node, '--version'])
    match = re.match(r'v?(\d+)\.', out or '') if code == 0 else None
    if not match:
        return check('node', 'Node.js and npx (browser automation)', 'warn', 'could not read the Node version (%s)'
                     % (out or 'no output'), install_hint('node'), path=npx)
    major = int(match.group(1))
    if major < MIN_NODE:
        return check('node', 'Node.js and npx (browser automation)', 'warn', 'Node %s is older than %d'
                     % (out.strip(), MIN_NODE), install_hint('node'), path=npx)
    detail = 'Node %s, npx at %s' % (out.strip(), npx)
    if live:
        code, version = run([npx, '-y', PLAYWRIGHT_MCP, '--version'], timeout=300)
        detail += '; Playwright MCP: %s' % (version.splitlines()[-1] if code == 0 and version else 'failed (%s)' % version)
        if code != 0:
            return check('node', 'Node.js and npx (browser automation)', 'warn', detail,
                         'Check the network, then run `connect.py prewarm --services browser`.', path=npx)
    return check('node', 'Node.js and npx (browser automation)', 'ok', detail, path=npx)


def browser_candidates(kind=None):
    kind = kind or os_kind()
    if kind == 'macos':
        chrome = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                  os.path.expanduser('~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')]
        edge = ['/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
                os.path.expanduser('~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge')]
    elif kind == 'windows':
        roots = [os.environ.get(k) for k in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA')]
        roots = [r for r in roots if r]
        chrome = [os.path.join(r, 'Google', 'Chrome', 'Application', 'chrome.exe') for r in roots]
        edge = [os.path.join(r, 'Microsoft', 'Edge', 'Application', 'msedge.exe') for r in roots]
    else:
        chrome = [which(n) for n in ('google-chrome', 'google-chrome-stable')] + ['/opt/google/chrome/chrome']
        edge = [which(n) for n in ('microsoft-edge', 'microsoft-edge-stable')] + ['/opt/microsoft/msedge/msedge']
    return [p for p in chrome if p], [p for p in edge if p]


def check_browser(ws=None):
    chrome, edge = browser_candidates()
    chrome = next((p for p in chrome if exists(p)), None)
    edge = next((p for p in edge if exists(p)), None)
    channel = 'chrome'
    if ws is not None and (Path(ws) / layout.BROWSER).is_file():
        try:
            channel = read_json(Path(ws) / layout.BROWSER).get('channel', 'chrome')
        except ValueError:
            pass
    title = 'Google Chrome (or Microsoft Edge) for the browser server'
    if channel == 'msedge':
        if edge:
            return check('browser', title, 'ok', 'Microsoft Edge at %s (config/browser.json channel msedge)' % edge,
                         path=edge)
        return check('browser', title, 'warn', 'config/browser.json asks for Edge, which was not found',
                     install_hint('chrome'))
    if chrome:
        return check('browser', title, 'ok', 'Google Chrome at %s' % chrome, path=chrome)
    if edge:
        return check('browser', title, 'warn', 'Only Microsoft Edge found (%s); the browser server uses Chrome '
                     'unless config/browser.json sets "channel": "msedge"' % edge, install_hint('chrome'), path=edge)
    return check('browser', title, 'warn', 'Neither Google Chrome nor Microsoft Edge was found', install_hint('chrome'))


def required_tex_files():
    try:
        return list(read_json(TEMPLATE).get('required_files', []))
    except (OSError, ValueError):
        return []


def kpsewhich_missing(kpsewhich, files):
    """Files kpsewhich cannot find (matched by name, one batch call, per-file fallback)."""
    if not files:
        return []
    code, out = run([kpsewhich] + list(files), timeout=60)
    if code is None:
        return list(files)
    found = {os.path.basename(line.strip()) for line in (out or '').splitlines() if line.strip()}
    missing = [f for f in files if f not in found]
    confirmed = []
    for name in missing:
        code, out = run([kpsewhich, name], timeout=20)
        if code != 0 or not (out or '').strip():
            confirmed.append(name)
    return confirmed


def check_pdf(require_pdf=False, live=False, tools=None):
    tools = pdf_tools.resolve() if tools is None else tools
    bad = 'fail' if require_pdf else 'warn'
    results = []
    missing_tex = [t for t in TEX_TOOLS if not tools.get(t)]
    if missing_tex:
        results.append(check('tex', 'TeX (pdfLaTeX) for CV PDFs', bad, 'missing: ' + ', '.join(missing_tex),
                             install_hint('tex')))
    else:
        files = required_tex_files()
        missing = kpsewhich_missing(tools['kpsewhich'], files)
        detail = 'pdflatex at %s; %d/%d template files found' % (tools['pdflatex'], len(files) - len(missing), len(files))
        if live:
            code, out = run([tools['pdflatex'], '--version'])
            detail += '; ' + (out.splitlines()[0] if code == 0 and out else 'pdflatex --version failed')
        if missing:
            results.append(check('tex', 'TeX (pdfLaTeX) for CV PDFs', bad, detail + '; missing: ' + ', '.join(missing),
                                 install_hint('tex') + ' Find a file\'s package with `tlmgr search --global --file NAME`.',
                                 missing_files=missing, path=tools['pdflatex']))
        else:
            results.append(check('tex', 'TeX (pdfLaTeX) for CV PDFs', 'ok', detail, path=tools['pdflatex']))
    missing_poppler = [t for t in POPPLER_TOOLS if not tools.get(t)]
    if missing_poppler:
        results.append(check('poppler', 'Poppler (PDF checks)', bad, 'missing: ' + ', '.join(missing_poppler),
                             install_hint('poppler')))
    else:
        results.append(check('poppler', 'Poppler (PDF checks)', 'ok', 'pdfinfo at %s' % tools['pdfinfo'],
                             path=tools['pdfinfo']))
    return results


def cloud_folder(path):
    text = str(path).replace('\\', '/').lower()
    for marker, name in CLOUD_MARKERS:
        if '/' + marker in text or text.startswith(marker):
            return name
    return None


def git_work_tree(path):
    for parent in [Path(path)] + list(Path(path).parents):
        if (parent / '.git').exists():
            return parent
    return None


def check_workspace(explicit=None):
    """(checks, workspace path or None)."""
    title = 'Workspace'
    try:
        source, _ = next(iter(workspace_candidates(explicit)))
        ws = workspace(explicit)
    except WorkspaceError as error:
        return [check('workspace', title, 'fail', str(error), 'Choose a workspace folder outside the skill folder.')], None
    if not ws.is_dir() or not (ws / layout.CONFIG).is_file():
        return [check('workspace', title, 'warn', 'No workspace at %s yet (from %s)' % (ws, source),
                      'Run the onboarding workflow (onboard.py init --path FOLDER).', path=str(ws))], None
    results = []
    try:
        config = read_json(ws / layout.CONFIG)
        if not isinstance(config, dict):
            raise ValueError('not an object')
    except ValueError as error:
        return [check('workspace', title, 'fail', 'config.json is unreadable: %s' % error,
                      'Restore config.json from the change history or a backup.', path=str(ws))], ws
    onboarding = {}
    if (ws / layout.ONBOARDING).is_file():
        try:
            onboarding = read_json(ws / layout.ONBOARDING)
        except ValueError:
            onboarding = {}
    phase = onboarding.get('phase') or 'not started'
    pending = [n for n, s in (onboarding.get('steps') or {}).items() if s.get('state') in ('pending', 'in-progress')]
    changes = ws / layout.CHANGES
    events = len([l for l in changes.read_bytes().splitlines() if l.strip()]) if changes.is_file() else 0
    detail = '%s (from %s); onboarding: %s%s; history: %s' % (
        ws, source, phase, ' (next: %s)' % pending[0] if pending else '',
        '%d event(s)' % events if events else 'none yet')
    results.append(check('workspace', title, 'ok' if events else 'info', detail, None if events else
                         'Finish onboarding so the first history entry is recorded.', path=str(ws), phase=phase,
                         history_events=events, source=source))
    try:
        with shared_lock(ws / layout.LOCKS / 'doctor-probe.lock', timeout=2):
            pass
        results.append(check('locks', 'File locks in the workspace', 'ok', 'lock probe succeeded'))
    except WorkspaceError as error:
        results.append(check('locks', 'File locks in the workspace', 'fail', str(error),
                             'Move the workspace to a local disk folder (not a network or synced drive).'))
    except RuntimeError as error:
        results.append(check('locks', 'File locks in the workspace', 'warn', str(error)))
    cloud = cloud_folder(ws)
    if cloud:
        results.append(check('cloud', 'Workspace location', 'warn', 'The workspace is inside %s; sync clients can '
                             'break file locks and leave conflicted copies' % cloud,
                             'Move it to a local folder such as ~/Jobfather and update the pointer '
                             '(onboard.py init in the new place, then move the files).'))
    repo = git_work_tree(ws)
    if repo:
        results.append(check('git', 'Workspace location', 'warn', 'The workspace is inside a git work tree (%s); '
                             'personal data could be committed or pushed' % repo,
                             'Move it outside any repository, or make sure the repository is private and ignored.'))
    if os.name != 'nt':
        mode = ws.stat().st_mode & 0o777
        if mode & 0o077:
            results.append(check('permissions', 'Workspace permissions', 'warn', 'The workspace folder is readable by '
                                 'other users (mode %o)' % mode, 'chmod 700 "%s"' % ws))
    return results, ws


def check_connections(ws):
    if ws is None:
        return []
    import connect
    title = 'Connections'
    path = ws / layout.CONNECTIONS
    platforms = {}
    if (ws / layout.PLATFORMS).is_file():
        try:
            platforms = read_json(ws / layout.PLATFORMS)
        except ValueError:
            platforms = {}
    try:
        data = connect.load_connections(ws) if path.is_file() else None
    except ValueError as error:
        return [check('connections', title, 'warn', str(error), 'Re-run the connect workflow to rebuild it.')]
    enabled = [p for p in ('naukri', 'linkedin') if (platforms.get(p) or {}).get('enabled') is True]
    if data is None:
        if enabled:
            return [check('connections', title, 'warn', 'Enabled but not connected: ' + ', '.join(enabled),
                          'Run the connect workflow.', flags={p: ['not_connected'] for p in enabled})]
        return [check('connections', title, 'info', 'No connections recorded yet (optional).')]
    resume_host = (data.get('resume') or {}).get('host')
    summary, problems = {}, {}
    for host, item in sorted(data.get('hosts', {}).items()):
        for service, entry in sorted(item.get('services', {}).items()):
            found = connect.flags(entry, service, resume_host, host)
            summary['%s/%s' % (host, service)] = {'level': connect.level(entry), 'flags': found}
            if found:
                problems['%s/%s' % (host, service)] = found
    recorded = {key.split('/', 1)[1] for key in summary}
    for name in enabled:
        if name not in recorded:
            problems[name] = ['not_connected']
            summary[name] = {'level': 'none', 'flags': ['not_connected']}
    if resume_host and not any('restart_pending' in v for v in problems.values()):
        problems[resume_host] = ['restart_pending']
    notes = []
    if data.get('machine') not in (None, connect.machine_hash()):
        notes.append('states were recorded on another machine')
    detail = '; '.join('%s: %s' % (k, v['level'] + (' [%s]' % ', '.join(v['flags']) if v['flags'] else ''))
                       for k, v in summary.items()) or 'nothing recorded'
    if notes:
        detail += ' (' + '; '.join(notes) + ')'
    fixes = []
    flat = {f for v in problems.values() for f in v}
    if 'restart_pending' in flat:
        fixes.append((data.get('resume') or {}).get('action') or 'Restart the agent app, then resume setup.')
    if 'reauth_needed' in flat or 'stale' in flat:
        fixes.append('Log in again for the flagged service and record the result (connect.md); do not reset profiles.')
    if 'not_connected' in flat:
        fixes.append('Run the connect workflow for the flagged service.')
    return [check('connections', title, 'warn' if problems or notes else 'ok', detail, ' '.join(fixes) or None,
                  flags=problems, services=summary)]


def save_tools(checks, tools):
    """Remember tool locations in the machine settings file (never in the workspace)."""
    paths = {name: path for name, path in tools.items() if path}
    for item in checks:
        if item.get('path') and item['id'] in ('uv', 'node', 'git', 'browser'):
            paths[item['id']] = item['path']
    dirs = sorted({str(Path(p).parent) for n, p in paths.items() if n in pdf_tools.REQUIRED})
    data = dict(machine())
    data.update({'tool_bin_paths': dirs, 'tools': paths, 'saved_at': now()})
    atomic_json(machine_file(), data)
    return str(machine_file())


def report(explicit=None, require_pdf=False, live=False, save=False):
    tools = pdf_tools.resolve()
    checks = [check_python(), check_uv(live), check_git(), check_node(live)]
    workspace_checks, ws = check_workspace(explicit)
    checks.append(check_browser(ws))
    checks += check_pdf(require_pdf, live, tools)
    checks += workspace_checks
    checks += check_connections(ws)
    result = {'ready': not any(c['status'] == 'fail' for c in checks), 'generated_at': now(), 'os': os_kind(),
              'machine': platform.machine(), 'require_pdf': require_pdf, 'live': live, 'checks': checks}
    if save:
        result['saved_tools'] = save_tools(checks, tools)
    return result


def human(result):
    marks = {'ok': '[ok]  ', 'info': '[info]', 'warn': '[warn]', 'fail': '[FAIL]'}
    lines = []
    for item in result['checks']:
        lines.append('%s %s: %s' % (marks[item['status']], item['title'], item['detail']))
        if item.get('fix') and item['status'] in ('warn', 'fail', 'info'):
            lines.append('       fix: ' + item['fix'])
    lines.append('Ready: %s' % ('yes' if result['ready'] else 'no (fix the [FAIL] items)'))
    if result.get('saved_tools'):
        lines.append('Saved tool locations to ' + result['saved_tools'])
    return '\n'.join(lines)


def main(argv=None):
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    parser.add_argument('--json', action='store_true', help='print the JSON report only')
    parser.add_argument('--require-pdf', action='store_true', help='treat missing TeX/Poppler as not ready')
    parser.add_argument('--live', action='store_true', help='also run version probes (may use the network)')
    parser.add_argument('--save-tools', action='store_true', help='remember tool locations in machine.json')
    args = parser.parse_args(argv)
    result = report(args.workspace, args.require_pdf, args.live, args.save_tools)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else human(result))
    return 0 if result['ready'] else 1


if __name__ == '__main__':
    sys.exit(main())
