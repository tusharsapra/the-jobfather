"""Find local TeX and Poppler tools from machine settings, standard install folders, then PATH."""
import glob
import os
from pathlib import Path
import shutil
import subprocess

from local import machine

REQUIRED = ('pdflatex', 'kpsewhich', 'pdfinfo', 'pdftotext', 'pdftoppm', 'pdffonts')
STANDARD_DIRS = (
    '/Library/TeX/texbin', '/usr/local/texlive/*/bin/*', '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin',
    '~/Library/TinyTeX/bin/*', '~/.TinyTeX/bin/*', '~/.local/share/*/texlive/bin/*', '~/texlive/*/bin/*',
    'C:/texlive/*/bin/*', 'C:/Program Files/MiKTeX/miktex/bin/x64', '~/AppData/Local/Programs/MiKTeX/miktex/bin/x64',
)


def candidate_dirs():
    dirs = [Path(p) for p in machine().get('tool_bin_paths', [])]
    for pattern in STANDARD_DIRS:
        dirs += [Path(p) for p in sorted(glob.glob(os.path.expanduser(pattern)), reverse=True)]
    return dirs


def _exe(directory, name):
    for suffix in ('', '.exe'):
        path = directory / (name + suffix)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.absolute())
    return None


def resolve():
    dirs = candidate_dirs()
    result = {}
    for name in REQUIRED:
        result[name] = next((p for p in (_exe(d, name) for d in dirs) if p), None) or shutil.which(name)
    return result


def require():
    tools = resolve()
    missing = [name for name, path in tools.items() if not path]
    if missing:
        raise RuntimeError('Missing PDF tools: ' + ', '.join(missing) + '; run doctor.py for install steps')
    return tools


def run(args, directory=None, timeout=60, env=None):
    try:
        return subprocess.run([str(x) for x in args], cwd=directory, env=env, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('Local PDF command timed out: ' + str(args[0])) from error
