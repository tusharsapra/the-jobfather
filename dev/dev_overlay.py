#!/usr/bin/env python3
"""Make a local copy of the plugin whose MCP servers run from this checkout (for development).

    python3 dev/dev_overlay.py
    claude --plugin-dir ~/.cache/jobfather-dev/overlay/the-jobfather
    codex plugin marketplace add ~/.cache/jobfather-dev/overlay   # Codex

A copy (not symlinks) is used because hosts reject plugin files that point outside the plugin.
Skills-folder installs can instead set JOBFATHER_DEV_MCP_SOURCE=<repo>/packages/jobfather-mcp
so connect.py registers `uv run --directory …` commands.
"""
import json
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / 'plugins' / 'the-jobfather'
OUT = Path('~/.cache/jobfather-dev/overlay').expanduser()


def rewrite(path, package):
    data = json.loads(path.read_text(encoding='utf-8'))
    for server in data.get('mcpServers', {}).values():
        args = server.get('args', [])
        if server.get('command') == 'uvx' and '--from' in args:
            entry = args[args.index('--from') + 2]
            server['command'] = 'uv'
            server['args'] = ['run', '--directory', str(package), entry] + args[args.index('--from') + 3:]
    path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')


def main():
    target = OUT / 'the-jobfather'
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(PLUGIN, target, ignore=shutil.ignore_patterns('__pycache__', '.DS_Store'))
    package = REPO / 'packages' / 'jobfather-mcp'
    for name in ('.mcp.json', '.codex-mcp.json', 'mcp_config.json'):
        rewrite(target / name, package)
    marketplace = {'name': 'the-jobfather-dev', 'interface': {'displayName': 'The Jobfather (dev)'},
                   'plugins': [{'name': 'the-jobfather', 'source': {'source': 'local', 'path': './the-jobfather'},
                                'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'},
                                'category': 'Productivity'}]}
    (OUT / '.agents' / 'plugins').mkdir(parents=True, exist_ok=True)
    (OUT / '.agents' / 'plugins' / 'marketplace.json').write_text(json.dumps(marketplace, indent=2) + '\n', encoding='utf-8')
    print('Overlay ready: %s' % target)


if __name__ == '__main__':
    main()
