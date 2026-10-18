"""connect.py: host detection, install modes, plans per host and mode, consent, file steps with backup and
undo, command steps, connections.json and the CLI. Host config files are always temp files here."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from support import REPO, SKILL, DemoWorkspace

import connect
import layout

SNAPSHOT = REPO / 'tests' / 'fixtures' / 'connect' / 'plans.json'
SCENARIOS = {
    'claude-code-plugin': ('claude-code', 'plugin', 'browser,naukri,linkedin,gmail', 'mcp'),
    'claude-code-skills': ('claude-code', 'skills', 'browser,naukri,linkedin,gmail', 'mcp'),
    'codex-plugin': ('codex', 'plugin', 'browser,naukri,linkedin,gmail', 'mcp'),
    'codex-skills': ('codex', 'skills', 'browser,naukri,linkedin,gmail', 'mcp'),
    'antigravity-plugin': ('antigravity', 'plugin', 'browser,naukri,gmail', 'browser'),
    'antigravity-skills': ('antigravity', 'skills', 'browser,naukri,linkedin', 'browser'),
    'cursor-skills': ('cursor', 'skills', 'browser,naukri', 'browser'),
    'other-skills': ('other', 'skills', 'browser,gmail', 'browser'),
}
CODEX_CONFIG = '''model = "fictional-model"

[mcp_servers.jobfather_browser]
command = "uvx"
args = ["--from", "SRC", "jobfather-browser"]

[mcp_servers.jobfather_browser.env]
UV_HTTP_TIMEOUT = "300"

[mcp_servers.jobfather_naukri]
command = "uvx"
args = [
  "--from",
  "SRC",  # a comment with [brackets]
  "jobfather-naukri",
]
note = """multi-line
[not.a.table]
"""

[mcp_servers.jobfather_naukri.env]
UV_HTTP_TIMEOUT = "300"

[profiles."quoted name"]
model = "other"
'''


SHIPPED = connect.NAUKRI_SHIPPED


class ConnectCase(unittest.TestCase):
    def setUp(self):
        self.demo = DemoWorkspace(init=False)
        self.ws = self.demo.root
        self.home = Path(os.environ['HOME'])
        self.env = mock.patch.dict(os.environ, {'CODEX_HOME': str(self.home / '.codex'),
                                                'CLAUDE_CONFIG_DIR': str(self.home / '.claude')})
        self.env.start()
        os.environ.pop('JOBFATHER_DEV_MCP_SOURCE', None)
        self.machine = mock.patch.object(connect, 'machine_id', return_value='fictional-machine-id')
        self.machine.start()
        self.naukri = mock.patch.object(connect, 'NAUKRI_SHIPPED', True)  # these tests cover the Naukri wiring too
        self.naukri.start()

    def tearDown(self):
        self.naukri.stop()
        self.machine.stop()
        self.env.stop()
        self.demo.close()

    def plan_file(self, host, services, linkedin_mode='browser', install='skills'):
        plan = connect.build_plan(host, services.split(','), linkedin_mode, install, self.ws)
        path = self.ws / layout.STATE / 'connect-plan.json'
        connect.atomic_json(path, plan)
        return path, plan

    def log(self):
        return connect.log_entries(self.ws)


# ------------------------------------------------------------------ detection
class DetectionTests(ConnectCase):
    def test_environment_and_parent_process_signals(self):
        cases = [
            ({'CLAUDECODE': '1'}, [], 'claude-code'),
            ({'CLAUDE_CODE_ENTRYPOINT': 'cli'}, [], 'claude-code'),
            ({'CODEX_SANDBOX': 'seatbelt'}, [], 'codex'),
            ({}, [(30, 'zsh'), (20, 'codex')], 'codex'),
            ({'ANTIGRAVITY_AGENT': '1'}, [], 'antigravity'),
            ({}, [(30, 'bash'), (20, 'agy')], 'antigravity'),
            ({}, [(30, 'Antigravity Helper (Renderer)')], 'antigravity'),
            ({'CURSOR_AGENT': '1'}, [], 'cursor'),
            ({'CLAUDECODE': '1', 'CURSOR_TRACE_ID': 'x'}, [(30, 'claude'), (20, 'Cursor')], 'claude-code'),
        ]
        for env, chain, host in cases:
            with self.subTest(env=env, chain=chain):
                result = connect.detect(env=env, chain=chain, home=self.home)
                self.assertEqual((result['host'], result['source'], result['confidence']), (host, 'evidence', 'high'))
                self.assertIsNone(result['question'])

    def test_nearest_agent_process_wins_over_inherited_variables(self):
        result = connect.detect(env={'CLAUDECODE': '1'}, chain=[(30, 'codex'), (20, 'claude')], home=self.home)
        self.assertEqual((result['host'], result['confidence']), ('codex', 'medium'))
        self.assertTrue(result['conflicts'])

    def test_self_identification_comes_first(self):
        result = connect.detect('antigravity', env={'CLAUDECODE': '1'}, chain=[], home=self.home)
        self.assertEqual((result['host'], result['source']), ('antigravity', 'self'))
        self.assertIn('claude-code', result['conflicts'][0])
        with self.assertRaises(ValueError):
            connect.detect('emacs', env={}, chain=[], home=self.home)

    def test_unsure_means_unknown_with_one_question(self):
        for env in ({}, {'CURSOR_TRACE_ID': 'x', 'CODEX_HOME': '/tmp/x'}):
            result = connect.detect(env=env, chain=[(30, 'zsh'), (20, 'Cursor')], home=self.home)
            self.assertEqual(result['host'], 'unknown')
            self.assertEqual(result['question'], connect.QUESTION)
        self.assertEqual(result['hints'], ['codex', 'cursor'])
        self.assertTrue(all(s['signal'] in ('CURSOR_TRACE_ID', 'CODEX_HOME') for s in result['signals']))

    @unittest.skipIf(os.name == 'nt', 'the parent-process walk uses ps (POSIX only)')
    def test_ancestor_walk_parses_ps(self):
        outputs = iter(['  42 /bin/zsh\n', '   7 /opt/fake/bin/codex\n', '   1 /sbin/launchd\n'])
        fake = lambda *a, **k: mock.Mock(stdout=next(outputs))
        with mock.patch.object(connect.os, 'getppid', return_value=99), \
                mock.patch.object(connect.subprocess, 'run', side_effect=fake):
            chain = connect.ancestors()
        self.assertEqual(chain, [(99, 'zsh'), (42, 'codex'), (7, 'launchd')])


class InstallModeTests(ConnectCase):
    def make_skill(self, root):
        (root / 'SKILL.md').parent.mkdir(parents=True, exist_ok=True)
        (root / 'SKILL.md').write_text('---\nname: the-jobfather\n---\n', encoding='utf-8')
        return root

    def test_checkout_plugin_and_skills_folder(self):
        self.assertEqual(connect.install_info(skill_dir=SKILL, home=self.home)['mode'], 'dev')
        plugin = self.home / '.claude' / 'plugins' / 'cache' / 'mkt' / 'the-jobfather' / '0.1.0'
        skill = self.make_skill(plugin / 'skills' / 'the-jobfather')
        (plugin / '.claude-plugin').mkdir(parents=True)
        (plugin / '.claude-plugin' / 'plugin.json').write_text('{}', encoding='utf-8')
        info = connect.install_info('claude-code', skill, self.home)
        self.assertEqual((info['mode'], info['hosts'], info['duplicates']), ('plugin', ['claude-code'], False))
        folder = self.make_skill(self.home / '.claude' / 'skills' / 'the-jobfather')
        info = connect.install_info('claude-code', folder, self.home)
        self.assertEqual((info['mode'], info['hosts']), ('skills', ['claude-code']))
        self.assertTrue(info['duplicates'])
        self.assertIn(str(plugin.parent.resolve()), info['warning'])

    def test_copies_for_other_hosts_are_not_duplicates(self):
        folder = self.make_skill(self.home / '.claude' / 'skills' / 'the-jobfather')
        self.make_skill(self.home / '.agents' / 'skills' / 'the-jobfather')
        self.assertFalse(connect.install_info('claude-code', folder, self.home)['duplicates'])
        self.assertTrue(connect.install_info('unknown', folder, self.home)['duplicates'])


# ------------------------------------------------------------------ sources and plans
class NaukriLaterTests(ConnectCase):
    """While the Naukri server is not released, setup leaves it out and says when it arrives."""

    def test_naukri_is_left_out_until_it_ships(self):
        with mock.patch.object(connect, 'NAUKRI_SHIPPED', False):
            plan = connect.build_plan('claude-code', ['browser', 'naukri'], 'browser', 'skills', self.ws)
            self.assertFalse([s for s in plan['steps'] if 'naukri' in s['id']])
            self.assertIn(connect.NAUKRI_LATER, plan['warnings'])
            for call in (lambda: connect.build_plan('codex', ['naukri'], 'browser', 'plugin', self.ws),
                         lambda: connect.enable(self.ws, 'naukri', 'Yes, turn it on.')):
                with self.assertRaises(ValueError) as ctx:
                    call()
                self.assertIn('v0.2.0', str(ctx.exception))


class SourceTests(ConnectCase):
    def test_pin_comes_from_the_plugin_manifest_and_fallbacks_match_it(self):
        manifest = json.loads((connect.PLUGIN_ROOT / '.mcp.json').read_text(encoding='utf-8'))
        args = manifest['mcpServers']['browser']['args']
        source = connect.mcp_source()
        self.assertEqual((source['spec'], source['origin']), (args[args.index('--from') + 1], 'plugin .mcp.json'))
        self.assertEqual(connect.FALLBACK_SOURCE, source['spec'])
        codex = json.loads((connect.PLUGIN_ROOT / '.codex-mcp.json').read_text(encoding='utf-8'))['mcpServers']
        self.assertEqual('jobfather_naukri' in codex, SHIPPED)  # the plugin files and the switch agree
        if SHIPPED:
            self.assertEqual(list(connect.NAUKRI_TOOLS), codex['jobfather_naukri']['enabled_tools'])
        for service in ('browser', 'naukri'):
            keys, tables = connect.codex_keys(service)
            self.assertEqual((keys, tables), connect.CODEX_FALLBACK[service])

    def test_developer_source_uses_uv_run(self):
        package = REPO / 'packages' / 'jobfather-mcp'
        with mock.patch.dict(os.environ, {'JOBFATHER_DEV_MCP_SOURCE': str(package)}):
            spec = connect.server_spec('browser', connect.mcp_source())
        self.assertEqual(spec['command'], 'uv')
        self.assertEqual(spec['args'], ['run', '--directory', str(package.resolve()), 'jobfather-browser'])
        with mock.patch.dict(os.environ, {'JOBFATHER_DEV_MCP_SOURCE': str(self.home)}):
            with self.assertRaises(ValueError):
                connect.mcp_source()

    def test_linkedin_runs_behind_the_allowlist_proxy(self):
        spec = connect.server_spec('linkedin', connect.mcp_source())
        args = spec['args']
        self.assertEqual(args[2:4], ['jobfather-proxy', '--server-name'])
        self.assertEqual(args[args.index('--allow') + 1], ','.join(connect.LINKEDIN_ALLOW))
        self.assertEqual(args[args.index('--') + 1:], ['uvx', 'mcp-server-linkedin@4.27.0', '--transport', 'stdio',
                                                         '--no-auto-import'])
        self.assertFalse(set(connect.LINKEDIN_DENY) & set(connect.LINKEDIN_ALLOW))


def normalise(plan, home, ws):
    text = json.dumps(plan)
    text = text.replace(plan['source']['spec'], '<SRC>').replace(str(ws), '<WS>').replace(str(home), '<HOME>')
    data = json.loads(text)
    for key in ('plan_id', 'created_at', 'steps_sha256', 'workspace'):
        data.pop(key)
    data['source'] = data['source']['kind']
    return data


class PlanTests(ConnectCase):
    def test_plans_match_snapshots(self):
        got = {}
        for name, (host, install, services, mode) in SCENARIOS.items():
            plan = connect.build_plan(host, services.split(','), mode, install, self.ws)
            got[name] = normalise(plan, self.home, self.ws)
        if os.environ.get('JOBFATHER_UPDATE_SNAPSHOTS'):
            SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
            SNAPSHOT.write_text(json.dumps(got, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        want = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
        for name in SCENARIOS:
            with self.subTest(scenario=name):
                self.assertEqual(got[name], want[name])

    def test_registration_matrix(self):
        ids = lambda host, install, services='browser,naukri', mode='browser': [
            s['id'] for s in connect.build_plan(host, services.split(','), mode, install, self.ws)['steps']]
        self.assertIn('claude-code-plugin-browser', ids('claude-code', 'plugin'))
        self.assertIn('claude-browser-add', ids('claude-code', 'skills'))
        self.assertIn('codex-plugin-naukri', ids('codex', 'plugin'))
        self.assertTrue({'codex-naukri-add', 'codex-naukri-keys', 'codex-naukri-check'} <= set(ids('codex', 'skills')))
        self.assertIn('antigravity-browser-add', ids('antigravity', 'skills'))
        self.assertIn('cursor-naukri-add', ids('cursor', 'skills'))
        self.assertIn('other-browser-add', ids('other', 'skills'))
        claude = connect.build_plan('claude-code', ['browser'], 'browser', 'skills', self.ws)['steps']
        add = next(s for s in claude if s['id'] == 'claude-browser-add')
        self.assertEqual(add['argv'][:6], ['claude', 'mcp', 'add-json', '--scope', 'user', 'jobfather-browser'])
        self.assertEqual(json.loads(add['argv'][6]), {'type': 'stdio', 'command': 'uvx', 'env': {'UV_HTTP_TIMEOUT': '300'},
                                                      'args': ['--from', connect.mcp_source()['spec'], 'jobfather-browser']})
        codex = connect.build_plan('codex', ['naukri'], 'browser', 'skills', self.ws)['steps']
        add = next(s for s in codex if s['id'] == 'codex-naukri-add')
        self.assertEqual(add['argv'][:7], ['codex', 'mcp', 'add', 'jobfather_naukri', '--env', 'UV_HTTP_TIMEOUT=300', '--'])
        keys = next(s for s in codex if s['id'] == 'codex-naukri-keys')
        self.assertEqual(keys['tables'], {'mcp_servers.jobfather_naukri.tools.naukri_apply': {'approval_mode': 'prompt'}})

    def test_opt_in_steps_disappear_once_enabled(self):
        steps = lambda: [s['id'] for s in connect.build_plan('codex', ['naukri', 'linkedin'], 'mcp', 'plugin', self.ws)['steps']]
        self.assertTrue({'naukri-opt-in', 'linkedin-opt-in'} <= set(steps()))
        connect.enable(self.ws, 'naukri', 'Yes, I understand and want Naukri.')
        connect.enable(self.ws, 'linkedin', 'Yes, LinkedIn too.', 'browser')
        self.assertNotIn('naukri-opt-in', steps())
        self.assertIn('linkedin-mode', steps())  # enabled for the browser, plan wants MCP mode
        connect.enable(self.ws, 'linkedin', 'Switch LinkedIn to the MCP server.', 'mcp')
        self.assertFalse({'naukri-opt-in', 'linkedin-opt-in', 'linkedin-mode'} & set(steps()))

    def test_claude_permission_rules(self):
        plan = connect.build_plan('claude-code', ['naukri', 'linkedin', 'gmail'], 'mcp', 'plugin', self.ws)
        step = next(s for s in plan['steps'] if s['id'] == 'claude-permissions')
        ops = {op['path'][-1]: op['values'] for op in step['ops']}
        self.assertIn('mcp__plugin_the-jobfather_naukri__naukri_apply', ops['ask'])
        self.assertIn('mcp__jobfather-naukri__naukri_apply', ops['ask'])
        self.assertTrue({'mcp__claude_ai_Gmail__send_message', 'mcp__claude_ai_Gmail__reply',
                         'mcp__claude_ai_Gmail__forward'} <= set(ops['ask']))
        self.assertTrue({'mcp__jobfather-linkedin__send_message', 'mcp__jobfather-linkedin__connect_with_person',
                         'mcp__jobfather-linkedin__get_inbox', 'mcp__jobfather-linkedin__get_feed',
                         'mcp__claude_ai_Gmail__trash_message', 'mcp__claude_ai_Gmail__mark_thread_spam',
                         'mcp__claude_ai_Gmail__delete_label'} <= set(ops['deny']))
        self.assertTrue(all(len(n) <= 64 for values in ops.values() for n in values))
        self.assertTrue(step['requires_consent'])
        self.assertEqual(step['file'], str(self.home / '.claude' / 'settings.json'))

    def test_tool_names_over_64_characters_are_refused(self):
        with self.assertRaises(ValueError):
            connect.check_tool_names(['mcp__' + 'x' * 60])
        connect.check_tool_names(['mcp__plugin_the-jobfather_naukri__naukri_recommended_jobs'])
        with mock.patch.object(connect, 'PLUGIN_NAME', 'the-jobfather-with-a-much-longer-plugin-name'):
            with self.assertRaises(ValueError):
                connect.build_plan('claude-code', ['naukri'], 'browser', 'plugin', self.ws)

    def test_bad_arguments(self):
        for args in ((('emacs',), ['browser']), (('codex',), ['fax']), (('codex',), [])):
            with self.assertRaises(ValueError):
                connect.build_plan(args[0][0], args[1], 'browser', 'skills', self.ws)
        with self.assertRaises(ValueError):
            connect.build_plan('codex', ['linkedin'], 'api', 'skills', self.ws)


# ------------------------------------------------------------------ running steps
class RunTests(ConnectCase):
    def test_consent_is_required_and_plans_cannot_be_edited(self):
        path, _ = self.plan_file('claude-code', 'naukri,gmail', install='plugin')
        settings = self.home / '.claude' / 'settings.json'
        for words in (None, '', '   ', 'y'):
            with self.assertRaises(ValueError):
                connect.run_step(self.ws, path, 'claude-permissions', words)
        self.assertFalse(settings.exists())
        self.assertEqual(self.log(), [])
        plan = json.loads(path.read_text(encoding='utf-8'))
        plan['steps'][0]['argv'] = ['rm', '-rf', '/']
        path.write_text(json.dumps(plan), encoding='utf-8')
        with self.assertRaises(ValueError):
            connect.run_step(self.ws, path, plan['steps'][0]['id'], 'yes')
        with self.assertRaises(ValueError):
            connect.run_command(['rm', '-rf', '/'])
        with self.assertRaises(ValueError):
            connect.run_command(['/usr/bin/claude', 'mcp', 'list'])

    def test_json_merge_with_backup_and_undo(self):
        settings = self.home / '.claude' / 'settings.json'
        settings.parent.mkdir(parents=True)
        original = ('{\n    "theme": "dark",\n    "permissions": {\n        "allow": ["Bash(ls:*)"],\n'
                    '        "deny": ["mcp__claude_ai_Gmail__trash_message"]\n    }\n}\n').encode('utf-8')
        settings.write_bytes(original)
        os.chmod(settings, 0o644)
        path, _ = self.plan_file('claude-code', 'naukri,gmail', install='plugin')
        result = connect.run_step(self.ws, path, 'claude-permissions', 'Yes, add those rules.')
        data = json.loads(settings.read_text(encoding='utf-8'))
        self.assertEqual(data['theme'], 'dark')
        self.assertEqual(data['permissions']['allow'], ['Bash(ls:*)'])
        self.assertEqual(data['permissions']['deny'].count('mcp__claude_ai_Gmail__trash_message'), 1)
        self.assertIn('mcp__plugin_the-jobfather_naukri__naukri_apply', data['permissions']['ask'])
        self.assertIn('\n    "theme"', settings.read_text(encoding='utf-8'))  # indentation kept
        self.assertEqual(Path(result['backup']).read_bytes(), original)
        self.assertRegex(Path(result['backup']).name, r'^settings\.json\.bak-jobfather-\d{8}T\d{6}Z$')
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(settings.stat().st_mode), 0o644)
        again = connect.run_step(self.ws, path, 'claude-permissions', 'Yes.')
        self.assertFalse(again['changed'])
        status = connect.load_connections(self.ws)['hosts']['claude-code']['services']
        self.assertIn('mcp__claude_ai_Gmail__send_message', status['gmail']['restrictions']['permissions']['ask'])
        connect.undo(self.ws, 'claude-permissions')
        self.assertEqual(settings.read_bytes(), original)
        with self.assertRaises(ValueError):
            connect.undo(self.ws, 'claude-permissions')

    def test_json_undo_after_other_edits_removes_only_our_rules(self):
        path, _ = self.plan_file('claude-code', 'naukri', install='plugin')
        settings = self.home / '.claude' / 'settings.json'
        result = connect.run_step(self.ws, path, 'claude-permissions', 'Yes.')
        self.assertIsNone(result['backup'])  # the file did not exist
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(settings.stat().st_mode), 0o600)
        data = json.loads(settings.read_text(encoding='utf-8'))
        data['permissions']['ask'].append('Bash(git push:*)')
        settings.write_text(json.dumps(data, indent=2), encoding='utf-8')
        connect.undo(self.ws, 'claude-permissions')
        self.assertEqual(json.loads(settings.read_text(encoding='utf-8'))['permissions']['ask'], ['Bash(git push:*)'])

    def test_json_with_comments_is_never_rewritten(self):
        settings = self.home / '.claude' / 'settings.json'
        settings.parent.mkdir(parents=True)
        settings.write_text('{\n  // my notes\n  "theme": "dark"\n}\n', encoding='utf-8')
        before = settings.read_bytes()
        path, _ = self.plan_file('claude-code', 'naukri', install='plugin')
        with self.assertRaises(ValueError):
            connect.run_step(self.ws, path, 'claude-permissions', 'Yes.')
        self.assertEqual(settings.read_bytes(), before)
        self.assertFalse(self.log()[-1]['ok'])

    def write_codex(self, text=CODEX_CONFIG):
        config = self.home / '.codex' / 'config.toml'
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_bytes(text.encode('utf-8'))
        return config

    def test_toml_insertion_backup_verification_and_undo(self):
        for newline in ('\n', '\r\n'):
            with self.subTest(newline=repr(newline)):
                config = self.write_codex(CODEX_CONFIG.replace('\n', newline))
                original = config.read_bytes()
                path, _ = self.plan_file('codex', 'naukri')
                result = connect.run_step(self.ws, path, 'codex-naukri-keys', 'Yes, add the settings.')
                text = config.read_bytes().decode('utf-8')
                self.assertEqual(Path(result['backup']).read_bytes(), original)
                self.assertEqual(connect.toml_verify(text, 'mcp_servers.jobfather_naukri', {'enabled_tools': []}), [])
                head, rest = text.split('[mcp_servers.jobfather_naukri.env]', 1)
                self.assertIn('enabled_tools = ["naukri_status", ', head)
                self.assertIn('tool_timeout_sec = 300' + newline, head)
                self.assertTrue(head.index('note = """') < head.index('env_vars = ["JOBFATHER_HOME"]'))
                self.assertTrue(text.endswith('[mcp_servers.jobfather_naukri.tools.naukri_apply]' + newline +
                                              'approval_mode = "prompt"' + newline))
                self.assertNotIn('\r\n' if newline == '\n' else '\r\r', text)
                for line in original.decode('utf-8').splitlines(True):
                    self.assertIn(line, text)  # nothing of the user's changed
                self.assertFalse(connect.run_step(self.ws, path, 'codex-naukri-keys', 'Yes.')['changed'])
                connect.undo(self.ws, 'codex-naukri-keys')
                self.assertEqual(config.read_bytes(), original)
                for backup in config.parent.glob('config.toml.bak-jobfather-*'):
                    backup.unlink()
                (self.ws / layout.CONNECT_LOG).unlink()

    def test_toml_undo_after_a_host_rewrote_the_file(self):
        config = self.write_codex()
        path, _ = self.plan_file('codex', 'naukri')
        connect.run_step(self.ws, path, 'codex-naukri-keys', 'Yes.')
        rewritten = config.read_text(encoding='utf-8').replace('startup_timeout_sec = 120', 'startup_timeout_sec = 120.0')
        rewritten += '\n[mcp_servers.other]\ncommand = "fictional"\n'
        config.write_text(rewritten, encoding='utf-8')
        result = connect.undo(self.ws, 'codex-naukri-keys')
        text = config.read_text(encoding='utf-8')
        self.assertEqual(result['removed_lines'], 7)
        for gone in ('env_vars', 'startup_timeout_sec', 'enabled_tools', 'naukri_apply', 'approval_mode'):
            self.assertNotIn(gone, text)
        self.assertIn('[mcp_servers.other]', text)
        self.assertIn('note = """multi-line', text)

    def test_toml_refusals_leave_the_file_alone(self):
        path, _ = self.plan_file('codex', 'naukri')
        with self.assertRaises(ValueError):
            connect.run_step(self.ws, path, 'codex-naukri-keys', 'Yes.')  # no config yet
        config = self.write_codex(CODEX_CONFIG.replace('[mcp_servers.jobfather_naukri.env]',
                                                       'tools = { naukri_apply = { approval_mode = "auto" } }\n\n'
                                                       '[mcp_servers.jobfather_naukri.env]'))
        before = config.read_bytes()
        with self.assertRaises(ValueError):
            connect.run_step(self.ws, path, 'codex-naukri-keys', 'Yes.')
        self.assertEqual(config.read_bytes(), before)
        config = self.write_codex()
        before = config.read_bytes()
        with mock.patch.object(connect, 'toml_verify', return_value=['simulated problem']):
            with self.assertRaises(ValueError):
                connect.run_step(self.ws, path, 'codex-naukri-keys', 'Yes.')
        self.assertEqual(config.read_bytes(), before)
        config = self.write_codex(CODEX_CONFIG.replace('[mcp_servers.jobfather_naukri]\n', '[mcp_servers.jobfather_naukri]\n'
                                                       'tool_timeout_sec = 600\n'))
        result = connect.run_step(self.ws, path, 'codex-naukri-keys', 'Yes.')
        self.assertIn('mcp_servers.jobfather_naukri.tool_timeout_sec', result['kept'])
        self.assertIn('tool_timeout_sec = 600', config.read_text(encoding='utf-8'))

    def test_toml_scanner(self):
        _, scan = connect.toml_scan(CODEX_CONFIG)
        headers = [i['name'] for i in scan if i['kind'] == 'header']
        self.assertEqual(headers, [('mcp_servers', 'jobfather_browser'), ('mcp_servers', 'jobfather_browser', 'env'),
                                   ('mcp_servers', 'jobfather_naukri'), ('mcp_servers', 'jobfather_naukri', 'env'),
                                   ('profiles', 'quoted name')])
        with self.assertRaises(ValueError):
            connect.toml_scan('[a]\nb = [1, 2\n')
        self.assertEqual(connect.toml_value(['a"b', 'c\\d', 'e\nf']), '["a\\"b", "c\\\\d", "e\\nf"]')
        self.assertEqual(connect.toml_value(True), 'true')
        self.assertEqual(connect.toml_key('needs quotes'), '"needs quotes"')

    def fake_cli(self, outputs):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            code, out, err = outputs.pop(0)
            return mock.Mock(returncode=code, stdout=out, stderr=err)

        return calls, run

    def test_command_steps_record_registration_and_resume(self):
        path, plan = self.plan_file('claude-code', 'browser')
        calls, run = self.fake_cli([(0, 'Added stdio MCP server jobfather-browser to user config', ''),
                                    (0, 'Removed MCP server jobfather-browser', '')])
        with mock.patch.object(connect.shutil, 'which', lambda name: '/opt/fake/bin/' + name), \
                mock.patch.object(connect.subprocess, 'run', side_effect=run):
            result = connect.run_step(self.ws, path, 'claude-browser-add', 'Yes, register it.')
            self.assertEqual(calls[0][0][:4], ['/opt/fake/bin/claude', 'mcp', 'add-json', '--scope'])
            self.assertFalse(calls[0][1]['shell'])
            self.assertIn('restart', result)
            data = connect.load_connections(self.ws)
            entry = data['hosts']['claude-code']['services']['browser']
            self.assertEqual(entry['registration']['method'], 'claude-cli')
            self.assertEqual(entry['registration']['server_key'], 'jobfather-browser')
            self.assertEqual(entry['status']['installed'], 'yes')
            self.assertEqual(data['resume']['host'], 'claude-code')
            self.assertIn('Quit Claude Code', data['resume']['action'])
            undone = connect.undo(self.ws, 'claude-browser-add')
        self.assertEqual(calls[1][0], ['/opt/fake/bin/claude', 'mcp', 'remove', '--scope', 'user', 'jobfather-browser'])
        self.assertTrue(undone['ok'])
        entry = connect.load_connections(self.ws)['hosts']['claude-code']['services']['browser']
        self.assertEqual((entry['registration'], entry['status']['installed']), (None, 'no'))
        events = [(e['event'], e['step'], e['ok']) for e in self.log() if e.get('step')]
        self.assertEqual(events, [('run', 'claude-browser-add', True), ('undo', 'claude-browser-add', True)])
        self.assertEqual(self.log()[0]['consent'], 'Yes, register it.')

    def test_failures_and_existing_registrations(self):
        path, _ = self.plan_file('claude-code', 'browser')
        calls, run = self.fake_cli([(1, '', 'MCP server jobfather-browser already exists in user config'),
                                    (2, '', 'boom')])
        with mock.patch.object(connect.shutil, 'which', lambda name: '/opt/fake/bin/' + name), \
                mock.patch.object(connect.subprocess, 'run', side_effect=run):
            self.assertTrue(connect.run_step(self.ws, path, 'claude-browser-add', 'Yes.')['already_present'])
            with self.assertRaises(ValueError):
                connect.run_step(self.ws, path, 'claude-browser-add', 'Yes.')
        self.assertEqual(connect.load_connections(self.ws)['hosts']['claude-code']['services']['browser']['last_error'][:9],
                         'claude ex')
        with mock.patch.object(connect.shutil, 'which', return_value=None):
            with self.assertRaises(ValueError) as caught:
                connect.run_step(self.ws, path, 'claude-browser-add', 'Yes.')
        self.assertIn('doctor.py', str(caught.exception))

    def test_manual_and_probe_steps_only_show_instructions(self):
        path, _ = self.plan_file('claude-code', 'gmail')
        result = connect.run_step(self.ws, path, 'gmail-connector')
        self.assertTrue(result['shown'])
        self.assertIn('Connectors', result['instructions'])
        self.assertEqual(connect.load_connections(self.ws)['resume']['host'], 'claude-code')
        result = connect.run_step(self.ws, path, 'gmail-verify')
        self.assertTrue(result['shown'])
        path, _ = self.plan_file('other', 'gmail')
        step = next(s for s in json.loads(path.read_text(encoding='utf-8'))['steps'] if s['id'] == 'gmail-workspace-mcp')
        self.assertEqual(step['config']['mcpServers']['gmail']['args'], ['workspace-mcp', '--tools', 'gmail'])
        self.assertTrue(step['config']['mcpServers']['gmail']['env']['GOOGLE_OAUTH_CLIENT_SECRET'].startswith('<'))


# ------------------------------------------------------------------ states
class StateTests(ConnectCase):
    def test_connections_schema_and_no_identities(self):
        evidence = self.demo.base / 'evidence.txt'
        evidence.write_text('Signed in as alex.rivera@mail.example', encoding='utf-8')
        connect.record(self.ws, 'codex', 'naukri', 'installed', 'yes')
        connect.record(self.ws, 'codex', 'naukri', 'authenticated', 'no', evidence_file=evidence,
                       error='Login page asked alex.rivera@mail.example for code 123456')
        data = json.loads((self.ws / layout.CONNECTIONS).read_text(encoding='utf-8'))
        self.assertEqual(set(data), {'schema', 'machine', 'updated_at', 'hosts', 'resume'})
        self.assertEqual(data['schema'], 'the-jobfather/connections@1')
        self.assertRegex(data['machine'], r'^[0-9a-f]{16}$')
        entry = data['hosts']['codex']['services']['naukri']
        self.assertTrue({'registration', 'restrictions', 'status', 'checked_at', 'last_error'} <= set(entry))
        self.assertEqual(set(entry['status']), set(connect.CHECKS))
        self.assertEqual(entry['status']['authenticated'], 'no')
        self.assertEqual(set(entry['evidence']['authenticated']), {'sha256', 'bytes'})
        raw = (self.ws / layout.CONNECTIONS).read_text(encoding='utf-8')
        for secret in ('alex.rivera', 'mail.example', '123456', 'fictional-machine-id'):
            self.assertNotIn(secret, raw)
        with self.assertRaises(ValueError):
            connect.record(self.ws, 'codex', 'naukri', 'logged-in', 'yes')

    def test_flags_and_resume_clearing(self):
        clock = connect._utc()
        old = (clock - connect.timedelta(days=8)).isoformat()
        entry = {'status': {'installed': 'yes', 'visible': 'yes', 'authenticated': 'yes', 'verified': 'yes'},
                 'checked_at': {'authenticated': old}}
        self.assertEqual(connect.flags(entry, 'naukri', clock=clock), ['stale'])
        self.assertEqual(connect.flags(entry, 'linkedin', clock=clock), [])
        self.assertEqual(connect.level(entry), 'verified')
        entry['status']['authenticated'] = 'no'
        self.assertEqual(connect.flags(entry, 'naukri', 'codex', 'codex', clock), ['reauth_needed', 'restart_pending'])
        self.assertEqual(connect.flags({'status': {}}, 'browser'), ['not_connected'])
        path, plan = self.plan_file('codex', 'browser')
        data = connect.load_connections(self.ws)
        connect.set_resume(data, path, plan, plan['steps'][0])
        connect.save_connections(self.ws, data)
        self.assertIn('connect_resume=yes', connect.brief(connect.status(self.ws)))
        self.assertIn('Codex thread', connect.resume(self.ws)['resume']['action'])
        connect.record(self.ws, 'claude-code', 'browser', 'visible', 'yes')  # another host: still pending
        self.assertIsNotNone(connect.load_connections(self.ws)['resume'])
        connect.record(self.ws, 'codex', 'browser', 'installed', 'yes')  # installed is not proof of a restart
        self.assertIsNotNone(connect.load_connections(self.ws)['resume'])
        self.assertTrue(connect.record(self.ws, 'codex', 'browser', 'visible', 'yes')['resume_cleared'])
        self.assertIsNone(json.loads((self.ws / layout.CONNECTIONS).read_text(encoding='utf-8')).get('resume'))
        self.assertEqual(connect.resume(self.ws)['resume'], None)

    def test_enable_shows_the_notice_first_then_records_the_words(self):
        shown = connect.enable(self.ws, 'naukri')
        self.assertFalse(shown['enabled'])
        self.assertIn('terms', shown['notice'])
        self.assertIn('restricted', shown['notice'])
        self.assertFalse(json.loads((self.ws / layout.PLATFORMS).read_text(encoding='utf-8'))['naukri']['enabled'])
        connect.enable(self.ws, 'naukri', 'I understand the risk; turn Naukri on.')
        data = json.loads((self.ws / layout.PLATFORMS).read_text(encoding='utf-8'))
        self.assertTrue(data['naukri']['enabled'])
        self.assertEqual(set(data['naukri']['acknowledged']), {'at', 'notice_version', 'statement'})
        self.assertEqual(data['ats']['greenhouse']['daily_cap'], 15)  # other settings untouched
        connect.enable(self.ws, 'linkedin', 'Yes for LinkedIn.', 'mcp')
        self.assertEqual(json.loads((self.ws / layout.PLATFORMS).read_text(encoding='utf-8'))['linkedin']['mode'], 'mcp')
        with self.assertRaises(ValueError):
            connect.enable(self.ws, 'naukri', 'ok', 'mcp')
        connect.disable(self.ws, 'naukri')
        self.assertFalse(json.loads((self.ws / layout.PLATFORMS).read_text(encoding='utf-8'))['naukri']['enabled'])
        self.assertEqual([e['event'] for e in self.log()], ['enable', 'enable', 'disable'])


class CommandLineTests(ConnectCase):
    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            connect.main(list(argv))
        return out.getvalue()

    def test_cli_round_trip(self):
        shown = json.loads(self.cli('workspace', '--show'))
        self.assertEqual((shown['workspace'], shown['source'], shown['initialized']), (str(self.ws), 'JOBFATHER_HOME', True))
        planned = json.loads(self.cli('plan', '--host', 'cursor', '--services', 'browser', '--install', 'skills'))
        self.assertTrue(Path(planned['plan']).is_file())
        self.assertEqual(planned['steps'][0]['id'], 'cursor-browser-add')
        merged = json.loads(self.cli('run', '--plan', planned['plan'], '--step', 'cursor-browser-add', '--consent', 'ok go'))
        self.assertTrue(merged['changed'])
        self.assertIn('jobfather-browser', json.loads((self.home / '.cursor' / 'mcp.json').read_text())['mcpServers'])
        self.assertIn('connect cursor: browser=installed', self.cli('status', '--brief'))
        self.assertIn('cursor', json.loads(self.cli('status', '--json'))['hosts'])
        self.assertIn('Reload Cursor', json.loads(self.cli('resume'))['resume']['action'])
        self.cli('record', '--host', 'cursor', '--service', 'browser', '--check', 'visible', '--result', 'yes')
        self.assertIn('Nothing is waiting', self.cli('resume'))
        self.cli('undo', '--step', 'cursor-browser-add')
        self.assertFalse((self.home / '.cursor' / 'mcp.json').exists())  # it did not exist before
        detected = json.loads(self.cli('detect', '--host', 'cursor', '--json'))
        self.assertEqual(detected['host'], 'cursor')
        self.assertIn('Host: codex', self.cli('detect', '--host', 'codex'))

    def test_workspace_init_delegates_to_onboarding(self):
        target = self.demo.base / 'new-ws'
        created = json.loads(self.cli('workspace', '--init', str(target)))
        self.assertEqual(created['workspace'], str(target))
        self.assertIsNone(created['pointer'])  # tests never write the real pointer
        self.assertTrue((target / 'config.json').is_file())

    def test_prewarm_runs_only_available_tools(self):
        calls, run = [], lambda argv, **k: calls.append(argv) or mock.Mock(returncode=0, stdout='Version 0.0.83', stderr='')
        with mock.patch.object(connect.shutil, 'which', lambda name: None if name == 'uvx' else '/opt/fake/bin/' + name), \
                mock.patch.object(connect.subprocess, 'run', side_effect=run):
            result = connect.prewarm(['browser', 'linkedin'])
        self.assertEqual(calls, [['/opt/fake/bin/npx', '-y', '@playwright/mcp@0.0.83', '--version']])
        skipped = [r['name'] for r in result['results'] if r.get('skipped')]
        self.assertEqual(skipped, ['jobfather-mcp', 'linkedin-mcp'])


if __name__ == '__main__':
    unittest.main()
