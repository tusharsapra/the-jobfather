"""Mechanical text checks for outgoing writing; not a replacement for factual or editorial review.

Errors block saving: limits, unfinished placeholders, internal evidence IDs, and phrases
the user listed under "Words and phrases I avoid" in VOICE.md.
Warnings are for the host to fix: stock job-letter and AI-sounding phrases, heavy dashes.
"""
import argparse
import json
from pathlib import Path
import re
import sys

GENERIC_PHRASES = (
    'i am writing to express my interest', 'i am writing to apply', "i'd like to apply for", 'i would like to apply for',
    'i am excited to apply', 'i am thrilled', 'passionate about', 'dear hiring manager', 'to whom it may concern',
    'hiring team', 'as mentioned in the job description', 'as mentioned in the job listing', 'your jd mentions',
    'you are looking for', 'my skills align with', 'i believe i would be a great fit', 'perfect fit',
    'if this opportunity is still open', 'if you are hiring', 'please let me know who', 'results-driven',
    'detail-oriented', 'team player', 'proven track record', 'fast-paced environment', 'hit the ground running',
    'think outside the box', 'synergy', 'spearheaded', 'leverage my', 'dynamic professional', 'go-getter',
    'game-changer', 'cutting-edge', 'delve', 'tapestry', 'testament to', 'navigate the complexities',
    "in today's fast-paced", 'let that sink in', "here's the thing", 'at its core', 'unlock', 'elevate',
)
PLACEHOLDER = re.compile(r'\b(?:TODO|TBD)\b|\[INSERT[^\]]*\]|\{\{[^}]+\}\}|<<[^>]+>>', re.I)
AVOID_HEADING = re.compile(r'(?im)^##\s+words and phrases i avoid\s*$')


def counts(text, include_spaces=True):
    chars = len(text) if include_spaces else len(re.sub(r'\s', '', text))
    return {'characters': chars, 'words': len(text.split()), 'lines': len(text.splitlines())}


def avoid_list(voice_text):
    """Bullets under '## Words and phrases I avoid' in VOICE.md."""
    m = AVOID_HEADING.search(voice_text or '')
    if not m:
        return []
    items = []
    for line in voice_text[m.end():].splitlines():
        if line.startswith('## '):
            break
        item = re.match(r'^\s*[-*]\s+(.+?)\s*$', line)
        if item:
            items.append(item.group(1).strip('"“”\'').strip())
    return [i for i in items if i]


def _has(text, phrase):
    return re.search(r'(?<![\w])' + re.escape(phrase) + r'(?![\w])', text, re.I) is not None


def check(text, max_chars=None, max_words=None, one_line=False, include_spaces=True, catalog=None, ws=None,
          avoid=None):
    result = counts(text, include_spaces)
    errors, warnings = [], []
    if max_chars is not None and result['characters'] > max_chars:
        errors.append('Character limit exceeded')
    if max_words is not None and result['words'] > max_words:
        errors.append('Word limit exceeded')
    if one_line and result['lines'] != 1:
        errors.append('Expected one line')
    if PLACEHOLDER.search(text):
        errors.append('Unfinished placeholder in outgoing text')
    if catalog is None and ws is not None:
        import evidence
        catalog = evidence.load(ws)
    if catalog is not None:
        import evidence
        leaked = evidence.leaks(text, catalog)
        if leaked:
            errors.append('Internal evidence ID in outgoing text: ' + ', '.join(leaked))
    if avoid is None and ws is not None:
        import layout
        voice = Path(ws) / layout.VOICE
        avoid = avoid_list(voice.read_bytes().decode('utf-8')) if voice.is_file() else []
    for phrase in avoid or []:
        if _has(text, phrase):
            errors.append('Uses a phrase you avoid: "%s"' % phrase)
    for phrase in GENERIC_PHRASES:
        if _has(text, phrase) and not any(phrase.casefold() == a.casefold() for a in avoid or []):
            warnings.append('Stock phrase: "%s"' % phrase)
    dashes = text.count('—') + len(re.findall(r'\s–\s', text))
    if dashes > max(2, result['words'] // 80):
        warnings.append('Many dashes (%d); prefer commas or full stops' % dashes)
    return dict(result, ok=not errors, errors=errors, warnings=warnings,
                id_check='done' if catalog is not None else 'skipped')


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from local import add_workspace_arg, require_workspace, WorkspaceError
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    parser.add_argument('file', type=Path)
    parser.add_argument('--max-chars', type=int)
    parser.add_argument('--max-words', type=int)
    parser.add_argument('--one-line', action='store_true')
    parser.add_argument('--exclude-spaces', action='store_true')
    args = parser.parse_args()
    try:
        ws = require_workspace(args.workspace)
    except WorkspaceError:
        ws = None
    text = args.file.read_bytes().decode('utf-8').rstrip('\n')
    result = check(text, args.max_chars, args.max_words, args.one_line, not args.exclude_spaces, ws=ws)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
