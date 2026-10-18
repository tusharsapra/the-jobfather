"""Guided logins: detect the host, plan MCP registrations, run each step with consent, record states.

    connect.py detect [--host H] [--json]
    connect.py workspace [--init PATH] [--show]
    connect.py enable --platform naukri|linkedin [--ack "<user words>"] [--mode browser|mcp]
    connect.py disable --platform naukri|linkedin
    connect.py plan --host H --services browser,naukri,linkedin,gmail [--linkedin-mode browser|mcp]
                    [--install plugin|skills|dev] [--out PLAN.json]
    connect.py prewarm --services browser,naukri,linkedin
    connect.py run --plan PLAN.json --step ID --consent "<user words>"
    connect.py record --host H --service S --check installed|visible|authenticated|verified
                      --result yes|no|inconclusive [--evidence-file F] [--error TEXT]
    connect.py status [--brief|--json]
    connect.py resume
    connect.py undo --step ID

Host configuration changes only through `run`, one step at a time, with the user's own words.
Every changed file is backed up as <file>.bak-jobfather-<timestamp> and every action is logged in
<workspace>/.state/connect-log.jsonl. connections.json keeps states and dates only: no accounts,
tokens or secrets.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import platform as host_os
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone

if __name__ == '__main__':
    sys.modules['connect'] = sys.modules[__name__]

import layout
from local import (CODE_ROOT, WorkspaceError, add_workspace_arg, atomic_bytes, atomic_json, now, pointer_file,
                   read_json, require_workspace, workspace, workspace_candidates)

HOSTS = ('claude-code', 'codex', 'antigravity', 'cursor', 'other')
SERVICES = ('browser', 'naukri', 'linkedin', 'gmail')
CHECKS = ('installed', 'visible', 'authenticated', 'verified')
RESULTS = ('yes', 'no', 'inconclusive')
STEP_KINDS = ('command', 'toml_table_keys', 'json_merge', 'manual', 'agent_probe')
PLAN_SCHEMA = 'the-jobfather/connect-plan@1'
CONNECTIONS_SCHEMA = 'the-jobfather/connections@1'
PLUGIN_ROOT = CODE_ROOT.parents[1]
PLUGIN_NAME = 'the-jobfather'
FALLBACK_SOURCE = 'git+https://github.com/tusharsapra/the-jobfather@4e1fb31154e6017e7118598ee22f7b888d03d118#subdirectory=packages/jobfather-mcp'
NAUKRI_SHIPPED = False  # the clean-room Naukri server is not in this release yet
NAUKRI_LATER = ('Naukri support arrives in The Jobfather v0.2.0. Until then, paste Naukri listings or links and '
                'job intake reads them like any other job.')
PLAYWRIGHT_MCP = '@playwright/mcp@0.0.83'
LINKEDIN_MCP = 'mcp-server-linkedin@4.27.0'
LINKEDIN_ALLOW = ('search_jobs', 'get_job_details', 'get_saved_jobs', 'get_job_apply_url', 'get_company_profile',
                  'search_posts', 'search_people', 'get_person_profile', 'get_my_profile', 'close_session')
LINKEDIN_DENY = ('send_message', 'get_inbox', 'get_conversation', 'search_conversations', 'connect_with_person',
                 'get_feed')
NAUKRI_TOOLS = ('naukri_status', 'naukri_login', 'naukri_search_jobs', 'naukri_recommended_jobs', 'naukri_get_job',
                'naukri_similar_jobs', 'naukri_saved_jobs', 'naukri_my_profile', 'naukri_prepare_apply',
                'naukri_apply', 'naukri_close')
GMAIL_SERVER = 'claude_ai_Gmail'
GMAIL_DENY = ('trash_message', 'trash_thread', 'mark_message_spam', 'mark_thread_spam', 'delete_label')
GMAIL_ASK = ('send_message', 'reply', 'forward')
CODEX_GMAIL = 'gmail@openai-curated'
UV_ENV = {'UV_HTTP_TIMEOUT': '300'}
CODEX_FALLBACK = {
    'browser': ({'env_vars': ['JOBFATHER_HOME'], 'startup_timeout_sec': 120, 'tool_timeout_sec': 120}, {}),
    'naukri': ({'env_vars': ['JOBFATHER_HOME'], 'startup_timeout_sec': 120, 'tool_timeout_sec': 300,
                'enabled_tools': list(NAUKRI_TOOLS)}, {'tools.naukri_apply': {'approval_mode': 'prompt'}}),
    'linkedin': ({'env_vars': ['JOBFATHER_HOME'], 'startup_timeout_sec': 180, 'tool_timeout_sec': 300,
                  'enabled_tools': list(LINKEDIN_ALLOW)}, {}),
}
RUNNABLE = ('claude', 'codex', 'agy', 'uvx', 'uv', 'npx')
STALE_DAYS = {'naukri': 7, 'linkedin': 14}
MAX_TOOL_NAME = 64
NOTICE_VERSION = 1
PLATFORM_NAMES = {'naukri': 'Naukri', 'linkedin': 'LinkedIn'}
NOTICE = ('Automated access to {name} may break {name}\'s terms of use and can get your {name} account restricted. '
          'It is opt-in only: The Jobfather connects to {name} only after you agree, keeps to slow, human-paced use, '
          'stops at any checkpoint, never applies or messages without your approval, and you can switch it off at '
          'any time (connect.py disable --platform {platform}).')
QUESTION = 'Which app am I running in right now: Claude Code, Codex, Antigravity, Cursor, or something else?'
SKILL_DIRS = (('claude-code', '.claude/skills/the-jobfather'), ('codex', '.agents/skills/the-jobfather'),
              ('antigravity', '.gemini/config/skills/the-jobfather'), ('cursor', '.cursor/skills/the-jobfather'))
PLUGIN_CACHES = (('claude-code', '.claude/plugins/cache'), ('codex', '.codex/plugins/cache'))
RESUME_SAY = ' Then say "resume Jobfather setup".'
_MACHINE = {}


def _utc():
    return datetime.now(timezone.utc)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def check_tool_names(names):
    too_long = sorted({n for n in names if len(n) > MAX_TOOL_NAME})
    if too_long:
        raise ValueError('Tool names longer than %d characters: %s' % (MAX_TOOL_NAME, ', '.join(too_long)))
    return list(names)


# ------------------------------------------------------------------ host detection
def ancestors(limit=25):
    """[(pid, executable name)] of parent processes, nearest first. Empty on Windows or without ps."""
    if os.name == 'nt':
        return []
    chain, pid, seen = [], os.getppid(), set()
    while pid > 1 and pid not in seen and len(chain) < limit:
        seen.add(pid)
        try:
            text = subprocess.run(['ps', '-o', 'ppid=,comm=', '-p', str(pid)], capture_output=True, text=True,
                                  timeout=3).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            break
        parts = text.split(None, 1)
        if len(parts) < 2 or not parts[0].isdigit():
            break
        chain.append((pid, os.path.basename(parts[1].strip())))
        pid = int(parts[0])
    return chain


def agent_of(process_name):
    """The agent host a process name belongs to, or None (IDEs hosting terminals do not count)."""
    name = process_name.lower()
    if name.endswith('.exe'):
        name = name[:-4]
    if name == 'claude' or name.startswith('claude-code'):
        return 'claude-code'
    if name == 'codex' or name.startswith(('codex-', 'codex.')):
        return 'codex'
    if name == 'agy' or 'antigravity' in name:
        return 'antigravity'
    if name == 'cursor-agent':
        return 'cursor'
    return None


def env_signals(env):
    """(strong, weak) lists of (host, variable name). Values are never reported."""
    strong, weak = [], []
    if env.get('CLAUDECODE') == '1':
        strong.append(('claude-code', 'CLAUDECODE=1'))
    if env.get('CLAUDE_CODE_ENTRYPOINT'):
        strong.append(('claude-code', 'CLAUDE_CODE_ENTRYPOINT'))
    sandbox = sorted(k for k in env if k.startswith('CODEX_SANDBOX'))
    if sandbox:
        strong.append(('codex', sandbox[0]))
    if env.get('CODEX_HOME'):
        weak.append(('codex', 'CODEX_HOME'))
    antigravity = sorted(k for k in env if k.upper().startswith('ANTIGRAVITY'))
    if antigravity:
        strong.append(('antigravity', antigravity[0]))
    if env.get('CURSOR_AGENT'):
        strong.append(('cursor', 'CURSOR_AGENT'))
    if env.get('CURSOR_TRACE_ID'):
        weak.append(('cursor', 'CURSOR_TRACE_ID'))
    return strong, weak


def detect(host=None, env=None, chain=None, skill_dir=None, home=None):
    """The agent's own answer first; otherwise environment and parent-process evidence."""
    env = os.environ if env is None else env
    strong, weak = env_signals(env)
    chain = ancestors() if chain is None else chain
    nearest = next(((agent_of(name), name) for _, name in chain if agent_of(name)), None)
    signals = [{'host': h, 'signal': s, 'strength': 'strong'} for h, s in strong]
    signals += [{'host': h, 'signal': s, 'strength': 'weak'} for h, s in weak]
    if nearest:
        signals.append({'host': nearest[0], 'signal': 'parent process ' + nearest[1], 'strength': 'strong'})
    env_hosts = {h for h, _ in strong}
    evidence = env_hosts | ({nearest[0]} if nearest else set())
    result = {'host': 'unknown', 'source': None, 'confidence': 'none', 'signals': signals, 'conflicts': [],
              'question': None}
    if host:
        if host not in HOSTS:
            raise ValueError('Host must be one of: ' + ', '.join(HOSTS))
        others = sorted(evidence - {host})
        result.update(host=host, source='self', confidence='high' if evidence == {host} else 'medium')
        if others:
            result['conflicts'] = ['evidence also points to %s; trusting your own answer' % ', '.join(others)]
    elif nearest:
        result.update(host=nearest[0], source='evidence', confidence='high' if env_hosts <= {nearest[0]} else 'medium')
        if env_hosts - {nearest[0]}:
            result['conflicts'] = ['environment points to %s, but the nearest agent process is %s'
                                   % (', '.join(sorted(env_hosts - {nearest[0]})), nearest[1])]
    elif len(env_hosts) == 1:
        result.update(host=env_hosts.pop(), source='evidence', confidence='high')
    else:
        result['question'] = QUESTION
        result['hints'] = sorted({h for h, _ in strong + weak})
    result['install'] = install_info(result['host'], skill_dir, home)
    return result


# ------------------------------------------------------------------ install mode
def dev_checkout(skill_dir):
    for parent in Path(skill_dir).resolve().parents:
        if (parent / '.git').exists() and (parent / 'packages' / 'jobfather-mcp').is_dir():
            return parent
    return None


def install_info(host=None, skill_dir=None, home=None):
    """How this copy is installed (plugin, skills folder or developer checkout) and other copies found."""
    skill_dir = Path(skill_dir or CODE_ROOT).resolve()
    home = Path(home or Path.home())
    plugin_root = skill_dir.parents[1] if len(skill_dir.parents) > 1 else skill_dir
    copies = []
    for owner, relative in SKILL_DIRS:
        path = home / relative
        if path.exists():
            copies.append({'kind': 'skills', 'host': owner, 'path': str(path.resolve())})
    for owner, relative in PLUGIN_CACHES:
        cache = home / relative
        if cache.is_dir():
            for marketplace in sorted(cache.iterdir()):
                candidate = marketplace / PLUGIN_NAME
                if candidate.is_dir():
                    copies.append({'kind': 'plugin', 'host': owner, 'path': str(candidate.resolve())})
    info = {'mode': 'unknown', 'skill_dir': str(skill_dir), 'hosts': [], 'copies': copies, 'duplicates': False,
            'warning': None}
    repo = dev_checkout(skill_dir)
    manifests = {'claude-code': plugin_root / '.claude-plugin' / 'plugin.json',
                 'codex': plugin_root / '.codex-plugin' / 'plugin.json',
                 'cursor': plugin_root / '.cursor-plugin' / 'plugin.json',
                 'antigravity': plugin_root / 'plugin.json'}
    if repo is not None:
        info.update(mode='dev', repo=str(repo))
    elif any(p.is_file() for p in manifests.values()) and skill_dir.parent.name == 'skills':
        info.update(mode='plugin', hosts=[h for h, p in manifests.items() if p.is_file()])
    else:
        info['mode'] = 'skills'
        info['hosts'] = [c['host'] for c in copies if c['kind'] == 'skills' and Path(c['path']) == skill_dir]
    others = sorted(c['path'] for c in copies if Path(c['path']) != skill_dir and Path(c['path']) not in skill_dir.parents
                    and (not host or host == 'unknown' or c['host'] == host))
    if others:
        info['duplicates'] = True
        info['warning'] = ('Found another copy of The Jobfather (%s). The host may load a different copy than this one; '
                           'keep one install (plugin or skills folder) and remove the other.' % ', '.join(others))
    return info


# ------------------------------------------------------------------ MCP sources and server specs
def mcp_source():
    """Where MCP servers come from: the plugin's pinned git source, or a developer checkout."""
    dev = os.environ.get('JOBFATHER_DEV_MCP_SOURCE', '').strip()
    if dev:
        path = Path(dev).expanduser().resolve()
        if not (path / 'pyproject.toml').is_file():
            raise ValueError('JOBFATHER_DEV_MCP_SOURCE must point at packages/jobfather-mcp (with pyproject.toml)')
        return {'kind': 'dev', 'path': str(path), 'spec': str(path), 'origin': 'JOBFATHER_DEV_MCP_SOURCE'}
    spec, origin = FALLBACK_SOURCE, 'built-in'
    manifest = PLUGIN_ROOT / '.mcp.json'
    if manifest.is_file():
        try:
            args = read_json(manifest)['mcpServers']['browser']['args']
            spec, origin = args[args.index('--from') + 1], 'plugin .mcp.json'
        except (ValueError, KeyError, IndexError, TypeError):
            pass
    return {'kind': 'git', 'spec': spec, 'pin': spec.split('@')[-1].split('#')[0], 'origin': origin}


def launch(source, entry, extra=()):
    if source['kind'] == 'dev':
        return 'uv', ['run', '--directory', source['path'], entry] + list(extra)
    return 'uvx', ['--from', source['spec'], entry] + list(extra)


def server_spec(service, source):
    if service == 'linkedin':
        command, args = launch(source, 'jobfather-proxy', ['--server-name', 'linkedin', '--allow',
                                                           ','.join(LINKEDIN_ALLOW), '--', 'uvx', LINKEDIN_MCP,
                                                           '--transport', 'stdio', '--no-auto-import'])
    else:
        command, args = launch(source, 'jobfather-' + service)
    return {'command': command, 'args': args, 'env': dict(UV_ENV)}


def fingerprint(spec):
    return _sha(canonical(spec).encode('utf-8'))[:16]


def server_key(host, method, service):
    if method == 'plugin':
        return 'jobfather_' + service if host == 'codex' else service
    return 'jobfather_' + service if host == 'codex' else 'jobfather-' + service


def tool_name(host, method, service, tool):
    """The tool name a host shows for a server's tool (Claude Code naming for plugin and user servers)."""
    key = server_key(host, method, service)
    if host == 'claude-code':
        return ('mcp__plugin_%s_%s__%s' % (PLUGIN_NAME, key, tool)) if method == 'plugin' else 'mcp__%s__%s' % (key, tool)
    return tool


def codex_keys(service):
    """Extra Codex table keys and sub-tables, read from the plugin's .codex-mcp.json when present."""
    keys, tables = copy.deepcopy(CODEX_FALLBACK[service])
    manifest = PLUGIN_ROOT / '.codex-mcp.json'
    if service != 'linkedin' and manifest.is_file():
        try:
            server = read_json(manifest)['mcpServers']['jobfather_' + service]
        except (ValueError, KeyError, TypeError):
            server = None
        if isinstance(server, dict):
            keys = {k: server[k] for k in ('env_vars', 'startup_timeout_sec', 'tool_timeout_sec', 'enabled_tools')
                    if k in server}
            tables = {'tools.' + tool: dict(value) for tool, value in server.get('tools', {}).items()}
    return keys, tables


def codex_config():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'config.toml'


def claude_settings():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude') / 'settings.json'


def cursor_mcp():
    return Path.home() / '.cursor' / 'mcp.json'


def restart_action(host, method):
    if host == 'claude-code':
        if method == 'plugin':
            return 'In Claude Code run /reload-plugins (or quit and start Claude Code again).' + RESUME_SAY
        return 'Quit Claude Code and start it again so it loads the new MCP servers.' + RESUME_SAY
    if host == 'codex':
        return 'Start a new Codex thread (or restart Codex) so it loads the new MCP servers.' + RESUME_SAY
    if host == 'antigravity':
        return 'Restart Antigravity so it loads the new MCP servers.' + RESUME_SAY
    if host == 'cursor':
        return 'Reload Cursor (Command Palette: "Developer: Reload Window") or restart it.' + RESUME_SAY
    return 'Restart your agent app so it loads the new MCP servers.' + RESUME_SAY


# ------------------------------------------------------------------ plan
def _step(step_id, host, kind, title, service=None, argv=None, changes=(), undo=None, consent=True, restart=False,
          **extra):
    if kind not in STEP_KINDS:
        raise ValueError('Unknown step kind ' + kind)
    step = {'id': step_id, 'host': host, 'service': service, 'kind': kind, 'title': title, 'changes': list(changes),
            'undo': undo, 'requires_consent': consent, 'restart': restart}
    if argv is not None:
        step['argv'] = list(argv)
    step.update(extra)
    return step


def _platforms(ws):
    if ws is None or not (Path(ws) / layout.PLATFORMS).is_file():
        return {}
    try:
        data = read_json(Path(ws) / layout.PLATFORMS)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _opted_in(platforms, name):
    entry = platforms.get(name) or {}
    return entry.get('enabled') is True and bool(entry.get('acknowledged'))


def _register(host, method, service, source, title):
    """Registration steps for one server on one host (CLI installs)."""
    spec = server_spec(service, source)
    key = server_key(host, method, service)
    restart = True
    if host == 'claude-code':
        payload = json.dumps(dict({'type': 'stdio'}, **spec), separators=(',', ':'))
        return [_step('claude-%s-add' % service, host, 'command', title, service,
                      argv=['claude', 'mcp', 'add-json', '--scope', 'user', key, payload],
                      changes=['your Claude Code user config: adds the MCP server "%s"' % key],
                      undo={'argv': ['claude', 'mcp', 'remove', '--scope', 'user', key]}, restart=restart,
                      server_key=key, registration='claude-cli', fingerprint=fingerprint(spec))]
    if host == 'codex':
        keys, tables = codex_keys(service)
        config = codex_config()
        argv = ['codex', 'mcp', 'add', key]
        for name, value in sorted(spec['env'].items()):
            argv += ['--env', '%s=%s' % (name, value)]
        argv += ['--', spec['command']] + spec['args']
        steps = [_step('codex-%s-add' % service, host, 'command', title, service, argv=argv,
                       changes=['%s: adds [mcp_servers.%s]' % (config, key)],
                       undo={'argv': ['codex', 'mcp', 'remove', key]}, restart=restart, server_key=key,
                       registration='codex-cli', fingerprint=fingerprint(spec))]
        described = ', '.join(sorted(keys)) + (' and ' + ', '.join('[%s]' % t for t in sorted(tables)) if tables else '')
        steps.append(_step('codex-%s-keys' % service, host, 'toml_table_keys',
                           'Add %s settings to the Codex server entry' % PLATFORM_NAMES.get(service, service.title()),
                           service, file=str(config), table='mcp_servers.' + key, set=keys,
                           tables={'mcp_servers.%s.%s' % (key, t): v for t, v in tables.items()},
                           changes=['%s: adds %s to [mcp_servers.%s] (text insertion, nothing else rewritten)'
                                    % (config, described, key)],
                           undo={'restore_backup': True}, restart=restart, server_key=key))
        steps.append(_step('codex-%s-check' % service, host, 'command', 'Check the entry with Codex\'s own parser',
                           service, argv=['codex', 'mcp', 'get', key, '--json'], consent=False, read_only=True,
                           server_key=key))
        return steps
    if host == 'antigravity':
        return [_step('antigravity-%s-add' % service, host, 'command', title, service,
                      argv=['agy', 'mcp', 'add', key, '--', spec['command']] + spec['args'],
                      changes=['your Antigravity MCP settings: adds the server "%s"' % key],
                      undo={'argv': ['agy', 'mcp', 'remove', key]}, restart=restart, server_key=key,
                      registration='agy-cli', fingerprint=fingerprint(spec), config={'mcpServers': {key: spec}},
                      notes='If agy rejects this syntax, add the server in Antigravity\'s MCP settings using "config".')]
    if host == 'cursor':
        path = cursor_mcp()
        return [_step('cursor-%s-add' % service, host, 'json_merge', title, service, file=str(path),
                      ops=[{'op': 'set_if_absent', 'path': ['mcpServers', key], 'value': spec}],
                      changes=['%s: adds mcpServers.%s' % (path, key)], undo={'restore_backup': True},
                      restart=restart, server_key=key, registration='json', fingerprint=fingerprint(spec))]
    return [_step('%s-%s-add' % (host, service), host, 'manual', title, service, consent=False, restart=restart,
                  server_key=key, registration='manual', fingerprint=fingerprint(spec),
                  instructions='Add this stdio MCP server in your agent\'s MCP settings, then restart it.',
                  config={'mcpServers': {key: spec}})]


def _verify_step(host, method, service, linkedin_mode='browser'):
    names = {'browser': 'browser_navigate', 'naukri': 'naukri_status', 'linkedin': 'get_my_profile'}
    if service == 'linkedin' and linkedin_mode == 'browser':
        return _step('linkedin-verify', host, 'agent_probe', 'Check LinkedIn in the browser: logged in, one real read',
                     service, consent=False, probe_tool=tool_name(host, method, 'browser', 'browser_navigate'),
                     instructions='Open linkedin.com with the browser tools; the user signs in in that window. Then '
                                  'open one job search page. Record each state with connect.py record.')
    guides = {
        'browser': 'Open a public page with the browser tools, then let the user sign in to the sites they want in '
                   'that Chrome window and check one logged-in page per site.',
        'naukri': 'Call naukri_login (the user signs in with Naukri\'s own email/OTP login), then naukri_status with '
                  'check "live", then a 5-result search.',
        'linkedin': 'After the login command, call get_my_profile (store only ok/not ok) and one job search.',
        'gmail': 'With the user\'s OK, read one recent sent message to confirm access. Send nothing.'}
    tool = tool_name(host, method, service, names[service]) if service in names else None
    return _step('%s-verify' % service, host, 'agent_probe', 'Check %s: visible, logged in, one real read'
                 % ('Gmail' if service == 'gmail' else PLATFORM_NAMES.get(service, service)), service, consent=False,
                 probe_tool=tool, instructions=guides[service] + ' Record each state with connect.py record.')


def build_plan(host, services, linkedin_mode='browser', install=None, ws=None):
    if host not in HOSTS:
        raise ValueError('Host must be one of: ' + ', '.join(HOSTS))
    services = list(dict.fromkeys(services))
    unknown = [s for s in services if s not in SERVICES]
    if unknown or not services:
        raise ValueError('Services must be among: ' + ', '.join(SERVICES))
    later = []
    if 'naukri' in services and not NAUKRI_SHIPPED:
        services.remove('naukri')
        later.append(NAUKRI_LATER)
        if not services:
            raise ValueError(NAUKRI_LATER)
    if linkedin_mode not in ('browser', 'mcp'):
        raise ValueError('LinkedIn mode is browser or mcp')
    info = install_info(host)
    mode = install or info['mode']
    if mode not in ('plugin', 'skills', 'dev'):
        mode = 'skills'
    method = 'plugin' if mode == 'plugin' else 'cli'
    source = mcp_source()
    platforms = _platforms(ws)
    steps, warnings = [], later
    if info.get('warning'):
        warnings.append(info['warning'])
    for name in ('naukri', 'linkedin'):
        if name in services and not _opted_in(platforms, name):
            steps.append(_step('%s-opt-in' % name, host, 'manual', 'Show the %s notice and record the user\'s opt-in'
                               % PLATFORM_NAMES[name], name, consent=False,
                               instructions='Run connect.py enable --platform %s to show the notice; if the user '
                                            'agrees, run it again with --ack "<their words>"%s.'
                                            % (name, ' --mode ' + linkedin_mode if name == 'linkedin' else '')))
        elif name == 'linkedin' and name in services and (platforms.get('linkedin') or {}).get('mode') != linkedin_mode:
            steps.append(_step('linkedin-mode', host, 'manual', 'Switch LinkedIn to %s mode' % linkedin_mode, name,
                               consent=False, instructions='Run connect.py enable --platform linkedin --mode %s '
                                                           '--ack "<their words>".' % linkedin_mode))
    for service in [s for s in services if s in ('browser', 'naukri')]:
        title = 'Register the %s server' % ('browser' if service == 'browser' else 'Naukri')
        if method == 'plugin':
            probe = tool_name(host, 'plugin', service, 'browser_tabs' if service == 'browser' else 'naukri_status')
            steps.append(_step('%s-plugin-%s' % (host, service), host, 'agent_probe',
                               'The plugin registers the %s server automatically' % service, service, consent=False,
                               probe_tool=probe, server_key=server_key(host, 'plugin', service), registration='plugin',
                               fingerprint=fingerprint(server_spec(service, source)),
                               instructions='Check that %s is in your tool list. If not: %s'
                                            % (probe, restart_action(host, 'plugin'))))
        else:
            steps += _register(host, method, service, source, title)
    if 'linkedin' in services and linkedin_mode == 'mcp':
        login = ['uvx', LINKEDIN_MCP, '--login', '--no-auto-import']
        steps.append(_step('linkedin-disclosure', host, 'manual', 'Tell the user what the LinkedIn MCP server is',
                           'linkedin', consent=True,
                           instructions='Say before installing: the LinkedIn MCP server (%s) is third-party software '
                                        'under the Apache-2.0 licence. It uses Patchright, a Playwright fork built to '
                                        'avoid bot detection. The Jobfather runs it behind an allowlist proxy that '
                                        'exposes only read tools (%s); messaging, connection requests, inbox and feed '
                                        'stay off. The user logs in themselves with: %s'
                                        % (LINKEDIN_MCP, ', '.join(LINKEDIN_ALLOW), ' '.join(login))))
        steps.append(_step('linkedin-login', host, 'manual', 'The user logs in to LinkedIn in the window this opens',
                           'linkedin', consent=False, user_command=' '.join(login),
                           instructions='Ask the user to run this in a terminal and sign in in the browser window it '
                                        'opens (passwords and codes go only into that window): ' + ' '.join(login)))
        steps += _register(host, 'cli', 'linkedin', source, 'Register the LinkedIn MCP server behind the allowlist proxy')
    elif 'linkedin' in services:
        steps.append(_step('linkedin-browser', host, 'agent_probe', 'LinkedIn through the browser server', 'linkedin',
                           consent=False, instructions='With LinkedIn enabled in browser mode the browser server stops '
                                                       'blocking linkedin.com. Open linkedin.com there and let the user '
                                                       'sign in.'))
    if 'gmail' in services:
        if host == 'claude-code':
            steps.append(_step('gmail-connector', host, 'manual', 'Connect Gmail as a claude.ai connector', 'gmail',
                               consent=False, restart=True, registration='connector', server_key=GMAIL_SERVER,
                               instructions='The user opens claude.ai, Settings, Connectors, Gmail, Connect, and signs '
                                            'in with Google there. Claude Code then lists tools named mcp__%s__*.'
                                            % GMAIL_SERVER))
        elif host == 'codex':
            steps.append(_step('codex-gmail-plugin', host, 'command', 'Install the Gmail plugin for Codex', 'gmail',
                               argv=['codex', 'plugin', 'add', CODEX_GMAIL],
                               changes=['your Codex plugins: installs %s' % CODEX_GMAIL],
                               undo={'argv': ['codex', 'plugin', 'remove', CODEX_GMAIL]}, restart=True,
                               registration='codex-plugin', server_key='gmail'))
        else:
            steps.append(_step('gmail-workspace-mcp', host, 'manual', 'Set up a Gmail MCP server with your own Google '
                               'OAuth client', 'gmail', consent=False, restart=True, registration='manual',
                               server_key='gmail',
                               instructions='1. In Google Cloud Console create a project, enable the Gmail API, set up '
                                            'the OAuth consent screen and create an OAuth client of type Desktop app. '
                                            '2. Add an MCP server to your agent config: command "uvx", args '
                                            '["workspace-mcp", "--tools", "gmail"], env GOOGLE_OAUTH_CLIENT_ID and '
                                            'GOOGLE_OAUTH_CLIENT_SECRET set to your own values. 3. Restart the agent '
                                            'and sign in with Google in the browser window. Keep the client secret '
                                            'only in the agent config: never in chat and never in the workspace.',
                               config={'mcpServers': {'gmail': {'command': 'uvx',
                                                                'args': ['workspace-mcp', '--tools', 'gmail'],
                                                                'env': {'GOOGLE_OAUTH_CLIENT_ID': '<your-client-id>',
                                                                        'GOOGLE_OAUTH_CLIENT_SECRET': '<your-client-secret>'}}}}))
    if host == 'claude-code':
        deny, ask = [], []
        if 'linkedin' in services and linkedin_mode == 'mcp':
            deny += [tool_name(host, 'cli', 'linkedin', t) for t in LINKEDIN_DENY]
        if 'gmail' in services:
            deny += ['mcp__%s__%s' % (GMAIL_SERVER, t) for t in GMAIL_DENY]
            ask += ['mcp__%s__%s' % (GMAIL_SERVER, t) for t in GMAIL_ASK]
        if 'naukri' in services:
            ask = [tool_name(host, 'plugin', 'naukri', 'naukri_apply'), tool_name(host, 'cli', 'naukri', 'naukri_apply')] + ask
        if deny or ask:
            path = claude_settings()
            ops = ([{'op': 'union', 'path': ['permissions', 'deny'], 'values': deny}] if deny else []) + \
                  ([{'op': 'union', 'path': ['permissions', 'ask'], 'values': ask}] if ask else [])
            steps.append(_step('claude-permissions', host, 'json_merge', 'Add permission rules: ask before applying or '
                               'sending, never delete mail or message on LinkedIn', None, file=str(path), ops=ops,
                               changes=['%s: adds %d deny and %d ask rules to "permissions" (nothing removed)'
                                        % (path, len(deny), len(ask))],
                               undo={'restore_backup': True}, restart=False))
    for service in services:
        steps.append(_verify_step(host, method if service != 'linkedin' or linkedin_mode == 'browser' else 'cli',
                                  service, linkedin_mode))
    names = []
    for step in steps:
        for op in step.get('ops', []):
            names += op.get('values', [])
        names += step.get('set', {}).get('enabled_tools', [])
        if step.get('probe_tool'):
            names.append(step['probe_tool'])
    check_tool_names(names + list(LINKEDIN_ALLOW))
    ids = [s['id'] for s in steps]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate plan step ids: %s' % ids)
    clock = _utc()
    plan = {'schema': PLAN_SCHEMA, 'plan_id': 'plan-%s-%s' % (clock.strftime('%Y%m%dT%H%M%SZ'), os.urandom(3).hex()),
            'created_at': clock.isoformat(), 'host': host, 'install_mode': mode, 'method': method,
            'services': services, 'linkedin_mode': linkedin_mode, 'source': source, 'workspace': str(ws) if ws else None,
            'restart_action': restart_action(host, method), 'steps': steps, 'warnings': warnings}
    plan['steps_sha256'] = _sha(canonical(steps).encode('utf-8'))
    return plan


def load_plan(path):
    plan = read_json(path)
    if plan.get('schema') != PLAN_SCHEMA:
        raise ValueError('Not a connect plan: %s' % path)
    if plan.get('steps_sha256') != _sha(canonical(plan.get('steps')).encode('utf-8')):
        raise ValueError('The plan file was edited after it was made; make a new plan')
    return plan


# ------------------------------------------------------------------ TOML (careful text insertion, never a rewrite)
_BARE = re.compile(r'^[A-Za-z0-9_-]+$')


def toml_key(key):
    return key if _BARE.match(key) else toml_value(key)


def toml_value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        out = []
        for ch in value:
            if ch in '"\\':
                out.append('\\' + ch)
            elif ch in '\b\t\n\f\r':
                out.append({'\b': '\\b', '\t': '\\t', '\n': '\\n', '\f': '\\f', '\r': '\\r'}[ch])
            elif ord(ch) < 0x20 or ord(ch) == 0x7f:
                out.append('\\u%04x' % ord(ch))
            else:
                out.append(ch)
        return '"' + ''.join(out) + '"'
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(toml_value(v) for v in value) + ']'
    raise TypeError('Unsupported TOML value: %r' % (value,))


def _split_dotted(text):
    parts, buf, quote, i = [], '', None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == '\\' and quote == '"' and i + 1 < len(text):
                buf += text[i + 1]
                i += 2
                continue
            if ch == quote:
                quote = None
            else:
                buf += ch
        elif ch in '"\'':
            quote = ch
        elif ch == '.':
            parts.append(buf.strip())
            buf = ''
        else:
            buf += ch
        i += 1
    if quote:
        raise ValueError('Unterminated quoted key')
    parts.append(buf.strip())
    return tuple(parts)


def _scan_value(text, state):
    """Advance multi-line string and bracket state over one line's value part; return the new state."""
    i, depth, ml = 0, state['depth'], state['ml']
    while i < len(text):
        if ml:
            end = text.find(ml, i)
            if end < 0:
                return {'depth': depth, 'ml': ml}
            i, ml = end + 3, None
            continue
        ch = text[i]
        if ch == '#':
            break
        if text.startswith('"""', i) or text.startswith("'''", i):
            ml, i = text[i:i + 3], i + 3
            continue
        if ch == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == '\\' else 1
            i += 1
            continue
        if ch == "'":
            end = text.find("'", i + 1)
            i = len(text) if end < 0 else end + 1
            continue
        if ch in '[{':
            depth += 1
        elif ch in ']}':
            depth -= 1
        i += 1
    return {'depth': depth, 'ml': ml}


def toml_scan(text):
    """Classify lines: [{'index', 'kind': header|key|blank|comment|continuation, 'name'}]; checks balance."""
    lines = text.splitlines(True)
    state, out = {'depth': 0, 'ml': None}, []
    for index, raw in enumerate(lines):
        line = raw.rstrip('\r\n')
        stripped = line.strip()
        if state['ml'] or state['depth'] > 0:
            out.append({'index': index, 'kind': 'continuation'})
            state = _scan_value(line, state)
            continue
        if not stripped:
            out.append({'index': index, 'kind': 'blank'})
        elif stripped.startswith('#'):
            out.append({'index': index, 'kind': 'comment'})
        elif stripped.startswith('['):
            array = stripped.startswith('[[')
            end = stripped.find(']]' if array else ']')
            if end < 0:
                raise ValueError('Unreadable table header on line %d' % (index + 1))
            out.append({'index': index, 'kind': 'header', 'array': array,
                        'name': _split_dotted(stripped[2 if array else 1:end])})
        else:
            eq = stripped.find('=')
            if eq <= 0:
                raise ValueError('Unreadable line %d' % (index + 1))
            out.append({'index': index, 'kind': 'key', 'name': _split_dotted(stripped[:eq])})
            state = _scan_value(stripped[eq + 1:], state)
    if state['ml'] or state['depth'] != 0:
        raise ValueError('Unbalanced brackets or an unterminated multi-line string')
    return lines, out


def _table_body(scan, name):
    """(header index, [key lines], index after the last content line) for table `name`, or None."""
    start = next((i for i, item in enumerate(scan) if item['kind'] == 'header' and not item['array']
                  and item['name'] == name), None)
    if start is None:
        return None
    body, last = [], scan[start]['index']
    for item in scan[start + 1:]:
        if item['kind'] == 'header':
            break
        if item['kind'] == 'key':
            body.append(item)
        if item['kind'] in ('key', 'continuation'):
            last = item['index']
    return scan[start]['index'], body, last + 1


def _key_line(key, value, newline):
    return '%s = %s%s' % (toml_key(key), toml_value(value), newline)


def toml_insert(text, table, keys, tables=None):
    """Insert missing keys into [table] and missing sub-tables; never change or reorder existing lines.

    Returns (new text, changes) where changes['lines'] maps each inserted "table.key" to its exact line.
    """
    newline = '\r\n' if '\r\n' in text else '\n'
    lines, scan = toml_scan(text)
    name = _split_dotted(table)
    found = _table_body(scan, name)
    if found is None:
        raise ValueError('[%s] is not in the file; run the step that registers the server first' % table)
    existing = {item['name'][0] for item in found[1]}
    changes = {'added': [], 'kept': [], 'tables_appended': [], 'lines': {}}
    block = []
    for key, value in keys.items():
        if key in existing:
            changes['kept'].append(table + '.' + key)
        else:
            block.append(_key_line(key, value, newline))
            changes['added'].append(table + '.' + key)
            changes['lines'][table + '.' + key] = block[-1]
    lines[found[2]:found[2]] = block
    for sub, values in (tables or {}).items():
        sub_name = _split_dotted(sub)
        relative = sub_name[len(name):]
        if sub_name[:len(name)] != name or not relative:
            raise ValueError('[%s] is not inside [%s]' % (sub, table))
        if relative[0] in existing:
            raise ValueError('[%s] already sets "%s" inline; edit that by hand instead' % (table, relative[0]))
        lines, scan_now = toml_scan(''.join(lines))
        here = _table_body(scan_now, sub_name)
        if here is None:
            if lines and not lines[-1].endswith('\n'):
                lines[-1] += newline
            lines += [newline, '[%s]%s' % ('.'.join(toml_key(p) for p in sub_name), newline)]
            for key, value in values.items():
                lines.append(_key_line(key, value, newline))
                changes['lines'][sub + '.' + key] = lines[-1]
                changes['added'].append(sub + '.' + key)
            changes['tables_appended'].append(sub)
        else:
            have = {item['name'][0] for item in here[1]}
            block = []
            for key, value in values.items():
                if key in have:
                    changes['kept'].append(sub + '.' + key)
                else:
                    block.append(_key_line(key, value, newline))
                    changes['lines'][sub + '.' + key] = block[-1]
                    changes['added'].append(sub + '.' + key)
            lines[here[2]:here[2]] = block
    result = ''.join(lines)
    toml_scan(result)
    return result, changes


def _line_value(line):
    """The value text of a simple one-line `key = value` (comments dropped), normalised for comparison."""
    value = line.split('=', 1)[1] if '=' in line else ''
    state, cut = {'depth': 0, 'ml': None}, len(value)
    for index in range(len(value)):  # find a trailing comment outside strings
        if value[index] == '#' and _scan_value(value[:index], state)['depth'] == 0 and \
                value[:index].count('"') % 2 == 0 and value[:index].count("'") % 2 == 0:
            cut = index
            break
    value = value[:cut].strip()
    try:
        return float(value)
    except ValueError:
        return re.sub(r'\s+', '', value)


def toml_remove(text, lines_by_key, appended=()):
    """Undo toml_insert on a file edited since: drop lines whose key and value are still ours (a host
    rewriting 120 as 120.0 still counts), then sub-tables we appended that are left empty."""
    src, scan = toml_scan(text)
    drop = set()
    wanted = {}
    for qualified, line in lines_by_key.items():
        table, _, key = qualified.rpartition('.')
        wanted.setdefault(table, {})[key] = _line_value(line)
    for table, keys in wanted.items():
        body = _table_body(scan, _split_dotted(table))
        for item in (body[1] if body else []):
            name = item['name'][0]
            if len(item['name']) == 1 and name in keys and _line_value(src[item['index']]) == keys[name]:
                drop.add(item['index'])
    for sub in appended:
        body = _table_body(scan, _split_dotted(sub))
        if body and all(item['index'] in drop for item in body[1]):
            drop.add(body[0])
            if body[0] > 0 and not src[body[0] - 1].strip():
                drop.add(body[0] - 1)
    result = ''.join(line for index, line in enumerate(src) if index not in drop)
    toml_scan(result)
    return result, len(drop)


def toml_verify(text, table, keys, tables=None):
    """Problems with `text` after an insertion (empty list when fine)."""
    problems = []
    try:
        _, scan = toml_scan(text)
    except ValueError as error:
        return [str(error)]
    for name, wanted in [(table, keys)] + list((tables or {}).items()):
        body = _table_body(scan, _split_dotted(name))
        if body is None:
            problems.append('[%s] missing' % name)
            continue
        have = {item['name'][0] for item in body[1]}
        problems += ['%s.%s missing' % (name, k) for k in wanted if k not in have]
    headers = [tuple(i['name']) for i in scan if i['kind'] == 'header' and not i['array']]
    if len(headers) != len(set(headers)):
        problems.append('a table is defined twice')
    try:
        import tomllib  # Python 3.11+: a full parse as a second opinion
    except ImportError:
        return problems
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        problems.append('TOML parse error: %s' % error)
    return problems


# ------------------------------------------------------------------ file steps
def _backup(path, data):
    stamp = _utc().strftime('%Y%m%dT%H%M%SZ')
    target = Path('%s.bak-jobfather-%s' % (path, stamp))
    counter = 1
    while target.exists():
        counter += 1
        target = Path('%s.bak-jobfather-%s-%d' % (path, stamp, counter))
    atomic_bytes(target, data)
    try:
        os.chmod(target, path.stat().st_mode & 0o777)
    except OSError:
        pass
    return target


def _write_keeping_mode(path, data, default_mode=0o600):
    mode = (path.stat().st_mode & 0o777) if path.exists() else default_mode
    atomic_bytes(path, data)
    if os.name != 'nt':
        os.chmod(path, mode)


def apply_toml(step):
    path = Path(step['file'])
    if not path.is_file():
        raise ValueError('%s does not exist yet; run the step that registers the server first' % path)
    original = path.read_bytes()
    text, changes = toml_insert(original.decode('utf-8'), step['table'], step.get('set', {}), step.get('tables', {}))
    result = {'file': str(path), 'table': step['table'], 'added': changes['added'], 'kept': changes['kept'],
              'lines': changes['lines'], 'tables_appended': changes['tables_appended'],
              'changed': bool(changes['added']), 'before_sha256': _sha(original)}
    if not changes['added']:
        result['after_sha256'] = result['before_sha256']
        return result
    backup = _backup(path, original)
    data = text.encode('utf-8')
    _write_keeping_mode(path, data)
    problems = toml_verify(path.read_bytes().decode('utf-8'), step['table'], step.get('set', {}), step.get('tables', {}))
    if problems:
        _write_keeping_mode(path, original)
        raise ValueError('Verification failed (%s); %s was restored from the backup' % ('; '.join(problems), path))
    result.update(backup=str(backup), after_sha256=_sha(data))
    return result


def _indent_of(text):
    m = re.search(r'\n([ \t]+)"', text)
    return m.group(1) if m else 2


def _walk(data, keys, create=True):
    target = data
    for key in keys:
        if key not in target:
            if not create:
                return None
            target[key] = {}
        target = target[key]
        if not isinstance(target, dict):
            raise ValueError('"%s" is not an object; not changing it' % '.'.join(keys))
    return target


def apply_json(step):
    path = Path(step['file'])
    existed = path.is_file()
    original = path.read_bytes() if existed else b''
    try:
        data = json.loads(original.decode('utf-8')) if original.strip() else {}
    except ValueError as error:
        raise ValueError('%s is not plain JSON (%s); not changing it' % (path, error))
    if not isinstance(data, dict):
        raise ValueError('%s does not hold a JSON object; not changing it' % path)
    added, kept = [], []
    for op in step['ops']:
        parent = _walk(data, op['path'][:-1])
        key = op['path'][-1]
        if op['op'] == 'union':
            current = parent.setdefault(key, [])
            if not isinstance(current, list):
                raise ValueError('"%s" is not a list; not changing it' % '.'.join(op['path']))
            for value in op['values']:
                if value in current:
                    kept.append({'path': op['path'], 'value': value})
                else:
                    current.append(value)
                    added.append({'path': op['path'], 'value': value})
        elif op['op'] == 'set_if_absent':
            if key in parent:
                kept.append({'path': op['path'], 'value': parent[key], 'differs': parent[key] != op['value']})
            else:
                parent[key] = op['value']
                added.append({'path': op['path'], 'value': op['value'], 'set': True})
        else:
            raise ValueError('Unknown JSON operation ' + str(op['op']))
    result = {'file': str(path), 'added': added, 'kept': kept, 'changed': bool(added), 'existed': existed,
              'before_sha256': _sha(original) if existed else None}
    if not added:
        result['after_sha256'] = result['before_sha256']
        return result
    text = json.dumps(data, indent=_indent_of(original.decode('utf-8')) if existed else 2, ensure_ascii=False) + '\n'
    backup = _backup(path, original) if existed else None
    _write_keeping_mode(path, text.encode('utf-8'))
    check = json.loads(path.read_bytes().decode('utf-8'))
    for item in added:
        node = _walk(check, item['path'][:-1], create=False)
        value = None if node is None else node.get(item['path'][-1])
        if (item.get('set') and value != item['value']) or (not item.get('set') and item['value'] not in (value or [])):
            if existed:
                _write_keeping_mode(path, original)
            else:
                path.unlink()
            raise ValueError('Verification failed for %s; the file was restored' % path)
    result.update(backup=str(backup) if backup else None, after_sha256=_sha(text.encode('utf-8')))
    return result


def _reverse_json(path, added):
    data = json.loads(path.read_bytes().decode('utf-8'))
    removed = []
    for item in added:
        parent = _walk(data, item['path'][:-1], create=False)
        if parent is None:
            continue
        key = item['path'][-1]
        if item.get('set'):
            if parent.get(key) == item['value']:
                del parent[key]
                removed.append(item)
        elif isinstance(parent.get(key), list) and item['value'] in parent[key]:
            parent[key].remove(item['value'])
            removed.append(item)
    return data, removed


# ------------------------------------------------------------------ workspace state
def machine_id():
    if 'id' in _MACHINE:
        return _MACHINE['id']
    value = None
    try:
        if sys.platform == 'darwin':
            out = subprocess.run(['ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'], capture_output=True, text=True,
                                 timeout=5).stdout
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            value = m.group(1) if m else None
        elif os.name == 'nt':
            out = subprocess.run(['reg', 'query', r'HKLM\SOFTWARE\Microsoft\Cryptography', '/v', 'MachineGuid'],
                                 capture_output=True, text=True, timeout=5).stdout
            m = re.search(r'MachineGuid\s+REG_SZ\s+(\S+)', out)
            value = m.group(1) if m else None
        else:
            for candidate in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
                if os.path.isfile(candidate):
                    value = Path(candidate).read_text(encoding='utf-8').strip() or None
                    if value:
                        break
    except (OSError, subprocess.SubprocessError):
        value = None
    _MACHINE['id'] = value or '%s|%s' % (socket.gethostname(), host_os.machine())
    return _MACHINE['id']


def machine_hash():
    return _sha(('the-jobfather/machine:' + machine_id()).encode('utf-8'))[:16]


def empty_connections():
    return {'schema': CONNECTIONS_SCHEMA, 'machine': machine_hash(), 'updated_at': None, 'hosts': {}, 'resume': None}


def load_connections(ws):
    path = Path(ws) / layout.CONNECTIONS
    if not path.is_file():
        return empty_connections()
    data = read_json(path)
    if not isinstance(data, dict) or data.get('schema') != CONNECTIONS_SCHEMA:
        raise ValueError('%s is not a %s file' % (layout.CONNECTIONS, CONNECTIONS_SCHEMA))
    data.setdefault('hosts', {})
    data.setdefault('resume', None)
    return data


def save_connections(ws, data):
    data['updated_at'] = now()
    data['machine'] = machine_hash()
    atomic_json(Path(ws) / layout.CONNECTIONS, data)
    return data


def service_entry(data, host, service):
    hosts = data['hosts'].setdefault(host, {'services': {}})
    entry = hosts.setdefault('services', {}).setdefault(service, {})
    entry.setdefault('registration', None)
    entry.setdefault('restrictions', {})
    entry.setdefault('status', {})
    for check in CHECKS:
        entry['status'].setdefault(check, 'unknown')
    entry.setdefault('checked_at', {})
    entry.setdefault('last_error', None)
    return entry


_EMAIL = re.compile(r'[\w.+-]+@[\w-]+(\.[\w-]+)+')


def scrub(text):
    """No account identities in connections.json: mask emails and long digit runs, cap the length."""
    text = _EMAIL.sub('<email>', str(text))
    return re.sub(r'\d{6,}', '<number>', text)[:500]


def log_event(ws, entry):
    path = Path(ws) / layout.CONNECT_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(entry, at=now()), ensure_ascii=False, sort_keys=True) + '\n'
    with path.open('ab') as stream:
        stream.write(line.encode('utf-8'))
        stream.flush()
        os.fsync(stream.fileno())


def log_entries(ws):
    path = Path(ws) / layout.CONNECT_LOG
    if not path.is_file():
        return []
    entries = []
    for line in path.read_bytes().decode('utf-8').splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def restrictions_for(step):
    service = step.get('service')
    if step['kind'] == 'toml_table_keys':
        out = {k: v for k, v in step.get('set', {}).items() if k == 'enabled_tools'}
        approvals = {t.rsplit('.', 1)[-1]: v.get('approval_mode') for t, v in step.get('tables', {}).items()
                     if 'approval_mode' in v}
        if approvals:
            out['approval_mode'] = approvals
        return out
    if service == 'linkedin' and step.get('registration'):
        return {'allow': list(LINKEDIN_ALLOW), 'proxy': 'jobfather-proxy'}
    return {}


def set_resume(data, plan_path, plan, step):
    registration = step.get('registration')
    method = plan.get('method') if registration in (None, 'connector') else ('plugin' if registration == 'plugin' else 'cli')
    data['resume'] = {'host': plan['host'], 'plan': str(plan_path), 'after_step': step['id'],
                      'action': restart_action(plan['host'], method), 'created_at': now(), 'machine': machine_hash()}


# ------------------------------------------------------------------ commands
def _resolve_exe(name):
    if os.path.basename(name) != name or name not in RUNNABLE:
        raise ValueError('Refusing to run %r: only %s are run by connect.py' % (name, ', '.join(RUNNABLE)))
    path = shutil.which(name)
    if not path:
        raise ValueError('%s is not installed or not on PATH; run doctor.py for install steps' % name)
    return path


def _tail(text, size=1500):
    text = (text or '').strip()
    return text[-size:]


def run_command(argv, timeout=180):
    exe = _resolve_exe(argv[0])
    try:
        done = subprocess.run([exe] + list(argv[1:]), capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'exit_code': None, 'stdout': '', 'stderr': 'timed out after %d s' % timeout}
    ok = done.returncode == 0
    output = done.stdout + done.stderr
    if not ok and re.search(r'already exists', output, re.I):
        ok = True
    return {'ok': ok, 'exit_code': done.returncode, 'stdout': _tail(done.stdout), 'stderr': _tail(done.stderr),
            'already_present': bool(done.returncode and ok)}


def run_step(ws, plan_path, step_id, consent=None):
    plan = load_plan(plan_path)
    step = next((s for s in plan['steps'] if s['id'] == step_id), None)
    if step is None:
        raise ValueError('No step %s in this plan; steps: %s' % (step_id, ', '.join(s['id'] for s in plan['steps'])))
    words = (consent or '').strip()
    if step['requires_consent'] and len(words) < 2:
        raise ValueError('Step %s changes your setup (%s). Show it to the user and pass their own words with '
                         '--consent "<their words>".' % (step_id, '; '.join(step['changes']) or step['title']))
    kind = step['kind']
    entry = {'event': 'run', 'plan': str(plan_path), 'plan_id': plan['plan_id'], 'step': step_id, 'host': step['host'],
             'service': step.get('service'), 'kind': kind, 'consent': words[:2000] or None, 'undo': step.get('undo'),
             'registration': step.get('registration'), 'restart': step.get('restart')}
    try:
        if kind == 'command':
            outcome = run_command(step['argv'], step.get('timeout_s', 180))
            result = dict(outcome, step=step_id)
            if not outcome['ok']:
                raise ValueError('%s exited with %s: %s' % (step['argv'][0], outcome['exit_code'],
                                                            outcome['stderr'] or outcome['stdout']))
        elif kind == 'toml_table_keys':
            result = dict(apply_toml(step), ok=True, step=step_id)
        elif kind == 'json_merge':
            result = dict(apply_json(step), ok=True, step=step_id)
        else:
            result = {'ok': True, 'step': step_id, 'shown': True, 'instructions': step.get('instructions'),
                      'user_command': step.get('user_command'), 'config': step.get('config')}
    except (ValueError, OSError) as error:
        log_event(ws, dict(entry, ok=False, error=str(error)[:1500]))
        data = load_connections(ws)
        if step.get('service') and step.get('registration'):
            service_entry(data, step['host'], step['service'])['last_error'] = scrub(error)
            save_connections(ws, data)
        raise
    log_event(ws, dict(entry, ok=True, **{k: result.get(k) for k in ('file', 'table', 'backup', 'before_sha256',
                                                                       'after_sha256', 'added', 'kept', 'lines',
                                                                       'tables_appended', 'existed', 'exit_code',
                                                                       'stdout', 'stderr')
                                         if k in result}))
    data = load_connections(ws)
    service = step.get('service')
    if service and (step.get('registration') or kind == 'toml_table_keys') and not step.get('read_only'):
        record = service_entry(data, step['host'], service)
        if step.get('registration'):
            record['registration'] = {'method': step['registration'], 'server_key': step.get('server_key'),
                                      'fingerprint': step.get('fingerprint')}
            if kind != 'agent_probe' and kind != 'manual':
                record['status']['installed'] = 'yes'
                record['checked_at']['installed'] = now()
        record['restrictions'].update(restrictions_for(step))
        record['last_error'] = None
        data['hosts'][step['host']]['install_mode'] = plan['install_mode']
    if step['id'] == 'claude-permissions':
        for item in result.get('added', []) + result.get('kept', []):
            rule, bucket = item['value'], item['path'][-1]
            owner = next((s for s, marker in (('naukri', 'naukri'), ('gmail', GMAIL_SERVER),
                                               ('linkedin', 'jobfather-linkedin')) if marker in rule), None)
            if owner and owner in plan['services']:
                rules = service_entry(data, step['host'], owner)['restrictions'].setdefault('permissions', {})
                if rule not in rules.setdefault(bucket, []):
                    rules[bucket].append(rule)
    if step.get('restart') and kind in ('command', 'toml_table_keys', 'json_merge', 'manual') and \
            (result.get('changed', True) or result.get('already_present')):
        set_resume(data, plan_path, plan, step)
        result['restart'] = data['resume']['action']
    save_connections(ws, data)
    return result


def record(ws, host, service, check, value, evidence_file=None, error=None):
    if host not in HOSTS:
        raise ValueError('Host must be one of: ' + ', '.join(HOSTS))
    if service not in SERVICES or check not in CHECKS or value not in RESULTS:
        raise ValueError('Use --service %s, --check %s and --result %s' % ('|'.join(SERVICES), '|'.join(CHECKS),
                                                                            '|'.join(RESULTS)))
    data = load_connections(ws)
    entry = service_entry(data, host, service)
    entry['status'][check] = value
    entry['checked_at'][check] = now()
    if evidence_file:
        blob = Path(evidence_file).read_bytes()
        entry.setdefault('evidence', {})[check] = {'sha256': _sha(blob), 'bytes': len(blob)}
    if value == 'yes' and check == 'verified':
        entry['last_error'] = None
    elif value != 'yes' and error:
        entry['last_error'] = scrub(error)
    resume = data.get('resume')
    cleared = False
    if resume and resume.get('host') == host and check != 'installed':
        data['resume'] = None
        cleared = True
    save_connections(ws, data)
    log_event(ws, {'event': 'record', 'host': host, 'service': service, 'check': check, 'result': value})
    return {'host': host, 'service': service, 'status': entry['status'], 'resume_cleared': cleared}


def _parse_time(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def flags(entry, service, resume_host=None, host=None, clock=None):
    """reauth_needed | stale | restart_pending | not_connected for one service entry."""
    clock = clock or _utc()
    status = entry.get('status', {})
    out = []
    if status.get('installed') != 'yes':
        out.append('not_connected')
    if status.get('authenticated') == 'no':
        out.append('reauth_needed')
    elif status.get('authenticated') == 'yes' and service in STALE_DAYS:
        at = _parse_time(entry.get('checked_at', {}).get('authenticated'))
        if at is None or clock - at > timedelta(days=STALE_DAYS[service]):
            out.append('stale')
    if resume_host and resume_host == host:
        out.append('restart_pending')
    return out


def level(entry):
    reached = 'none'
    for check in CHECKS:
        if entry.get('status', {}).get(check) != 'yes':
            break
        reached = check
    return reached


def status(ws):
    data = load_connections(ws)
    resume_host = (data.get('resume') or {}).get('host')
    hosts = {}
    for host, item in sorted(data['hosts'].items()):
        services = {}
        for service, entry in sorted(item.get('services', {}).items()):
            services[service] = {'level': level(entry), 'status': entry.get('status'),
                                 'flags': flags(entry, service, resume_host, host),
                                 'checked_at': entry.get('checked_at'), 'last_error': entry.get('last_error'),
                                 'registration': entry.get('registration')}
        hosts[host] = {'install_mode': item.get('install_mode'), 'services': services}
    return {'workspace': str(ws), 'this_machine': data.get('machine') in (None, machine_hash()), 'hosts': hosts,
            'resume': data.get('resume')}


def brief(report):
    lines = []
    for host, item in report['hosts'].items():
        parts = ['%s=%s%s' % (s, v['level'], '(' + ','.join(v['flags']) + ')' if v['flags'] else '')
                 for s, v in item['services'].items()]
        lines.append('connect %s: %s' % (host, ' '.join(parts) or 'nothing recorded'))
    if not lines:
        lines.append('connect: nothing connected yet')
    if report['resume']:
        lines.append('connect_resume=yes: %s' % report['resume']['action'])
    if not report['this_machine']:
        lines.append('note: these states were recorded on another machine')
    return '\n'.join(lines)


def resume(ws):
    data = load_connections(ws)
    rec = data.get('resume')
    if not rec:
        return {'resume': None, 'message': 'Nothing is waiting for a restart.'}
    done = {e['step'] for e in log_entries(ws) if e.get('event') == 'run' and e.get('ok')}
    pending = []
    if rec.get('plan') and Path(rec['plan']).is_file():
        try:
            plan = load_plan(rec['plan'])
        except ValueError:
            plan = None
        if plan:
            pending = [{'id': s['id'], 'title': s['title'], 'kind': s['kind'], 'requires_consent': s['requires_consent']}
                       for s in plan['steps'] if s['id'] not in done]
    return {'resume': rec, 'next_steps': pending,
            'message': 'If the user has done this: "%s", check that the new tools are visible and record it with '
                       'connect.py record (that clears this reminder); then continue with the next steps.'
                       % rec['action']}


def undo(ws, step_id, consent=None):
    runs = [e for e in log_entries(ws) if e.get('event') == 'run' and e.get('step') == step_id and e.get('ok')]
    changed = [e for e in runs if e.get('kind') not in ('toml_table_keys', 'json_merge')
               or e.get('after_sha256') != e.get('before_sha256')]
    if not runs:
        raise ValueError('No successful run of %s in the connect log' % step_id)
    last = changed[-1] if changed else runs[-1]  # a later no-op re-run must not hide the real change
    if any(e.get('event') == 'undo' and e.get('step') == step_id and e.get('ok') and e['at'] > last['at']
           for e in log_entries(ws)):
        raise ValueError('%s was already undone' % step_id)
    kind, result = last['kind'], {'step': step_id}
    if kind == 'command':
        argv = (last.get('undo') or {}).get('argv')
        if not argv:
            raise ValueError('%s has no automatic undo' % step_id)
        outcome = run_command(argv)
        result.update(outcome)
        if not outcome['ok']:
            log_event(ws, {'event': 'undo', 'step': step_id, 'ok': False, 'error': outcome['stderr'][:1500]})
            raise ValueError('Undo command failed: %s' % (outcome['stderr'] or outcome['stdout']))
    elif kind in ('toml_table_keys', 'json_merge'):
        path = Path(last['file'])
        if not last.get('after_sha256') or last.get('after_sha256') == last.get('before_sha256'):
            result['message'] = 'That step changed nothing, so there is nothing to undo'
        elif path.is_file() and _sha(path.read_bytes()) == last['after_sha256']:
            if last.get('backup'):
                _write_keeping_mode(path, Path(last['backup']).read_bytes())
                result['restored_from'] = last['backup']
            else:
                path.unlink()
                result['removed_file'] = str(path)
        elif path.is_file() and kind == 'json_merge':
            data, removed = _reverse_json(path, last.get('added') or [])
            _backup(path, path.read_bytes())
            _write_keeping_mode(path, (json.dumps(data, indent=_indent_of(path.read_text(encoding='utf-8')),
                                                  ensure_ascii=False) + '\n').encode('utf-8'))
            result['removed'] = removed
        elif path.is_file():
            current = path.read_bytes()
            text, removed = toml_remove(current.decode('utf-8'), last.get('lines') or {}, last.get('tables_appended') or ())
            if not removed:
                raise ValueError('%s changed since and none of the inserted lines are left; nothing to undo' % path)
            _backup(path, current)
            _write_keeping_mode(path, text.encode('utf-8'))
            result['removed_lines'] = removed
        else:
            raise ValueError('%s no longer exists' % path)
    else:
        result['message'] = 'Nothing to undo for a %s step' % kind
    log_event(ws, {'event': 'undo', 'step': step_id, 'ok': True, 'consent': (consent or '')[:2000] or None})
    data = load_connections(ws)
    host, service = last.get('host'), last.get('service')
    if host in data['hosts'] and service in data['hosts'][host].get('services', {}) and kind != 'agent_probe':
        entry = data['hosts'][host]['services'][service]
        if last.get('registration') and kind in ('command', 'json_merge'):
            entry['registration'] = None
            entry['status']['installed'] = 'no'
            entry['checked_at']['installed'] = now()
        elif kind == 'toml_table_keys':
            entry['restrictions'] = {}
        if last.get('restart') and kind != 'manual':
            data['resume'] = {'host': host, 'plan': last.get('plan'), 'after_step': step_id,
                              'action': restart_action(host, 'cli'), 'created_at': now(), 'machine': machine_hash()}
            result['restart'] = data['resume']['action']
    save_connections(ws, data)
    return result


def enable(ws, name, ack=None, mode=None):
    if name not in PLATFORM_NAMES:
        raise ValueError('Platform must be naukri or linkedin')
    if name == 'naukri' and not NAUKRI_SHIPPED:
        raise ValueError(NAUKRI_LATER)
    if mode and name != 'linkedin':
        raise ValueError('--mode applies to LinkedIn only')
    if mode not in (None, 'browser', 'mcp'):
        raise ValueError('LinkedIn mode is browser or mcp')
    notice = NOTICE.format(name=PLATFORM_NAMES[name], platform=name)
    if not ack:
        return {'platform': name, 'enabled': False, 'notice': notice, 'notice_version': NOTICE_VERSION,
                'next': 'Show this notice to the user. If they agree, run again with --ack "<their own words>".'}
    if len(ack.strip()) < 2:
        raise ValueError('Record the user\'s own words agreeing to the notice')
    path = Path(ws) / layout.PLATFORMS
    data = read_json(path) if path.is_file() else {'schema': 'the-jobfather/platforms@1'}
    entry = data.setdefault(name, {})
    entry['enabled'] = True
    entry['acknowledged'] = {'at': now(), 'notice_version': NOTICE_VERSION, 'statement': ack.strip()[:2000]}
    if name == 'linkedin':
        entry['mode'] = mode or entry.get('mode') or 'browser'
    if name == 'naukri':
        entry.setdefault('limits', {})
    atomic_json(path, data)
    log_event(ws, {'event': 'enable', 'platform': name, 'mode': entry.get('mode'), 'notice_version': NOTICE_VERSION,
                   'consent': ack.strip()[:2000]})
    return {'platform': name, 'enabled': True, 'mode': entry.get('mode'), 'notice': notice,
            'notice_version': NOTICE_VERSION}


def disable(ws, name):
    if name not in PLATFORM_NAMES:
        raise ValueError('Platform must be naukri or linkedin')
    path = Path(ws) / layout.PLATFORMS
    data = read_json(path) if path.is_file() else {'schema': 'the-jobfather/platforms@1'}
    data.setdefault(name, {})['enabled'] = False
    atomic_json(path, data)
    log_event(ws, {'event': 'disable', 'platform': name})
    return {'platform': name, 'enabled': False,
            'next': 'Remove its MCP server from the host too if one was registered (connect.py undo --step ID).'}


def prewarm(services):
    source = mcp_source()
    jobs = []
    if not NAUKRI_SHIPPED:
        services = [s for s in services if s != 'naukri']
    if 'browser' in services:
        jobs.append(('playwright-mcp', ['npx', '-y', PLAYWRIGHT_MCP, '--version']))
    if any(s in services for s in ('browser', 'naukri', 'linkedin')):
        command, args = launch(source, 'jobfather-browser', ['--version'])
        jobs.append(('jobfather-mcp', [command] + args))
    if 'linkedin' in services:
        jobs.append(('linkedin-mcp', ['uvx', LINKEDIN_MCP, '--help']))
    results = []
    for name, argv in jobs:
        try:
            outcome = run_command(argv, timeout=900)
        except ValueError as error:
            results.append({'name': name, 'ok': False, 'skipped': True, 'reason': str(error)})
            continue
        results.append({'name': name, 'ok': outcome['ok'], 'exit_code': outcome['exit_code'],
                        'output': _tail(outcome['stdout'] + '\n' + outcome['stderr'], 400)})
    return {'source': source, 'results': results}


def show_workspace(explicit=None):
    try:
        source, _ = next(iter(workspace_candidates(explicit)))
        ws = workspace(explicit)
    except WorkspaceError as error:
        return {'workspace': None, 'error': str(error)}
    return {'workspace': str(ws), 'source': source, 'exists': ws.is_dir(), 'initialized': (ws / layout.CONFIG).is_file(),
            'pointer_file': str(pointer_file())}


def human_detect(result):
    install = result['install']
    lines = ['Host: %s (%s; confidence %s)' % (result['host'], result['source'] or 'no evidence', result['confidence'])]
    lines += ['  note: ' + c for c in result['conflicts']]
    if result.get('question'):
        lines.append('Ask the user: ' + result['question'])
    lines.append('Install: %s%s' % (install['mode'], ' (%s)' % ', '.join(install['hosts']) if install['hosts'] else ''))
    if install.get('warning'):
        lines.append('Warning: ' + install['warning'])
    return '\n'.join(lines)


def human_status(report):
    lines = ['Workspace: ' + report['workspace']]
    for host, item in report['hosts'].items():
        lines.append('%s (%s)' % (host, item.get('install_mode') or 'install unknown'))
        for service, value in item['services'].items():
            states = ', '.join('%s=%s' % (c, value['status'].get(c, 'unknown')) for c in CHECKS)
            lines.append('  %-9s %s%s' % (service, states, '  [' + ', '.join(value['flags']) + ']' if value['flags'] else ''))
    if not report['hosts']:
        lines.append('Nothing connected yet.')
    if report['resume']:
        lines.append('Waiting for a restart: ' + report['resume']['action'])
    return '\n'.join(lines)


def main(argv=None):
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    sub = parser.add_subparsers(dest='command', required=True)
    child = sub.add_parser('detect')
    child.add_argument('--host', help='what the agent knows itself to be: ' + '|'.join(HOSTS))
    child.add_argument('--json', action='store_true')
    child = sub.add_parser('workspace')
    child.add_argument('--init', metavar='PATH')
    child.add_argument('--show', action='store_true')
    child = sub.add_parser('enable')
    child.add_argument('--platform', choices=sorted(PLATFORM_NAMES), required=True)
    child.add_argument('--ack', help="the user's own words agreeing to the notice")
    child.add_argument('--mode', choices=('browser', 'mcp'))
    sub.add_parser('disable').add_argument('--platform', choices=sorted(PLATFORM_NAMES), required=True)
    child = sub.add_parser('plan')
    child.add_argument('--host', choices=HOSTS, required=True)
    child.add_argument('--services', required=True, help='comma-separated: ' + ','.join(SERVICES))
    child.add_argument('--linkedin-mode', choices=('browser', 'mcp'), default='browser')
    child.add_argument('--install', choices=('plugin', 'skills', 'dev'))
    child.add_argument('--out', type=Path)
    sub.add_parser('prewarm').add_argument('--services', default='browser,naukri')
    child = sub.add_parser('run')
    child.add_argument('--plan', type=Path, required=True)
    child.add_argument('--step', required=True)
    child.add_argument('--consent', help="the user's own words approving this step")
    child = sub.add_parser('record')
    child.add_argument('--host', choices=HOSTS, required=True)
    child.add_argument('--service', choices=SERVICES, required=True)
    child.add_argument('--check', choices=CHECKS, required=True)
    child.add_argument('--result', choices=RESULTS, required=True)
    child.add_argument('--evidence-file', type=Path)
    child.add_argument('--error', help='short reason when the result is no or inconclusive (no account details)')
    child = sub.add_parser('status')
    child.add_argument('--brief', action='store_true')
    child.add_argument('--json', action='store_true')
    sub.add_parser('resume')
    child = sub.add_parser('undo')
    child.add_argument('--step', required=True)
    child.add_argument('--consent')
    args = parser.parse_args(argv)
    text = None
    if args.command == 'detect':
        result = detect(args.host)
        text = None if args.json else human_detect(result)
    elif args.command == 'workspace':
        if args.init:
            import onboard
            result = onboard.init(args.init)
        else:
            result = show_workspace(args.workspace)
    elif args.command == 'plan':
        try:
            ws = require_workspace(args.workspace)
        except WorkspaceError:
            ws = None
        plan = build_plan(args.host, [s.strip() for s in args.services.split(',') if s.strip()], args.linkedin_mode,
                          args.install, ws)
        out = args.out or (ws / layout.STATE / 'connect-plan.json' if ws else None)
        if out is None:
            raise WorkspaceError('No workspace yet: pass --out PLAN.json or create the workspace first')
        atomic_json(out, plan)
        result = {'plan': str(out), 'plan_id': plan['plan_id'], 'host': plan['host'], 'install_mode': plan['install_mode'],
                  'source': plan['source'].get('pin') or plan['source']['kind'],
                  'steps': [{'id': s['id'], 'kind': s['kind'], 'title': s['title'], 'changes': s['changes'],
                             'undo': s['undo'], 'requires_consent': s['requires_consent'], 'restart': s['restart']}
                            for s in plan['steps']],
                  'restart_action': plan['restart_action'], 'warnings': plan['warnings']}
    elif args.command == 'prewarm':
        result = prewarm([s.strip() for s in args.services.split(',') if s.strip()])
    else:
        ws = require_workspace(args.workspace)
        if args.command == 'enable':
            result = enable(ws, args.platform, args.ack, args.mode)
        elif args.command == 'disable':
            result = disable(ws, args.platform)
        elif args.command == 'run':
            result = run_step(ws, args.plan, args.step, args.consent)
        elif args.command == 'record':
            result = record(ws, args.host, args.service, args.check, args.result, args.evidence_file, args.error)
        elif args.command == 'status':
            report = status(ws)
            result = report
            text = brief(report) if args.brief else (None if args.json else human_status(report))
        elif args.command == 'resume':
            result = resume(ws)
        else:
            result = undo(ws, args.step, args.consent)
    print(text if text is not None else json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit('Connect failed: ' + str(error))
