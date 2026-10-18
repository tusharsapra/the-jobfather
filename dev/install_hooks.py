#!/usr/bin/env python3
"""Install The Jobfather's git hooks and (for the maintainer) the project identity.

    python3 dev/install_hooks.py            # contributors: hooks only
    python3 dev/install_hooks.py --author   # maintainer: hooks + no-reply identity, denylist required
"""
import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DENYLIST = Path('~/.config/the-jobfather-dev/denylist.txt').expanduser()


def git(*args):
    subprocess.run(['git', '-C', str(REPO), *args], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--author', action='store_true', help='set the maintainer no-reply identity and require the denylist')
    args = ap.parse_args()
    git('config', 'core.hooksPath', 'dev/hooks')
    for hook in (REPO / 'dev' / 'hooks').iterdir():
        hook.chmod(0o755)
    if args.author:
        git('config', 'user.name', 'tusharsapra')
        git('config', 'user.email', '66330793+tusharsapra@users.noreply.github.com')
        if not DENYLIST.exists():
            print('Hooks installed, but the private denylist is missing. Run dev/build_denylist.py first.', file=sys.stderr)
            return 1
    print('Hooks installed (core.hooksPath=dev/hooks).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
