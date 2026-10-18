"""Save a standalone reviewed draft that has no job description (outreach, thank-you, notes)."""
import argparse
import json
from pathlib import Path
import uuid

import evidence
import layout
import managed
from applications import KINDS, KIND_GROUP
from check_text import check
from local import (add_workspace_arg, atomic_json, atomic_text, contained, context_revision, digest, now, platform,
                   read_text, require_workspace)


def save(kind, text, host, review, limits=None, ws=None, test=False):
    platform(host)
    ws = require_workspace(ws)
    revision = context_revision(ws)
    if review.get('context_revision'):
        managed.assert_fresh(ws, review['context_revision'], KIND_GROUP.get(kind, 'writing'), 'This draft')
    if kind not in KINDS or not text.strip():
        raise ValueError('Need a supported kind and a non-empty draft')
    limits = limits or {}
    checks = check(text, catalog=evidence.load(ws), ws=ws, **limits)
    if not checks['ok'] or review.get('facts_preserved') is not True or review.get('voice_checked') is not True:
        raise ValueError('Draft needs passing checks and a factual/editorial review: ' + str(checks['errors']))
    rel = layout.sandboxed(layout.DRAFTS, test) + '/' + uuid.uuid4().hex
    directory = contained(ws, rel)
    atomic_text(directory / 'draft.md', text)
    record = {'kind': kind, 'fictional': test, 'path': rel + '/draft.md', 'sha256': digest(directory / 'draft.md'),
              'state': 'generated', 'platform': host, 'generated_at': now(), 'context_revision': revision,
              'limits': limits, 'checks': checks, 'review': review}
    atomic_json(directory / 'record.json', record)
    return record


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__))
    parser.add_argument('--kind', choices=KINDS, required=True)
    parser.add_argument('--file', type=Path, required=True)
    parser.add_argument('--review-file', type=Path, required=True)
    parser.add_argument('--platform', required=True)
    parser.add_argument('--max-chars', type=int)
    parser.add_argument('--max-words', type=int)
    parser.add_argument('--one-line', action='store_true')
    parser.add_argument('--exclude-spaces', action='store_true')
    parser.add_argument('--test', action='store_true', help='keep fictional artifacts in the sandbox')
    args = parser.parse_args()
    limits = {'max_chars': args.max_chars, 'max_words': args.max_words, 'one_line': args.one_line,
              'include_spaces': not args.exclude_spaces}
    review = json.loads(read_text(args.review_file))
    if not review.get('context_revision'):
        raise ValueError('Review needs the context_revision from the task read')
    result = save(args.kind, read_text(args.file).rstrip('\n'), args.platform, review, limits, args.workspace, args.test)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError) as error:
        raise SystemExit(str(error))
