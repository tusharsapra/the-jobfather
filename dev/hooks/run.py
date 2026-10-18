#!/usr/bin/env python3
"""Git hook dispatcher for The Jobfather (pre-commit, commit-msg, pre-push)."""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AUTHOR_EMAIL = '66330793+tusharsapra@users.noreply.github.com'


def author_mode() -> bool:
    if os.environ.get('JOBFATHER_CONTRIBUTOR'):
        return False
    email = subprocess.run(['git', '-C', str(REPO), 'config', 'user.email'], capture_output=True, text=True).stdout.strip()
    return email == AUTHOR_EMAIL


def scan(*args, stdin=None) -> int:
    cmd = [sys.executable, str(REPO / 'dev' / 'privacy_scan.py'), *args]
    if author_mode():
        cmd.append('--require-denylist')
    return subprocess.run(cmd, input=stdin, text=True).returncode


def main() -> int:
    hook = sys.argv[1]
    if hook == 'pre-commit':
        code = scan('--staged')
        validator = REPO / 'dev' / 'validate_package.py'
        if code == 0 and validator.exists():
            code = subprocess.run([sys.executable, str(validator), '--quick']).returncode
        return code
    if hook == 'commit-msg':
        return scan('--commit-msg', sys.argv[2])
    if hook == 'pre-push':
        return scan('--push-range', stdin=sys.stdin.read())
    print('unknown hook %s' % hook, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
