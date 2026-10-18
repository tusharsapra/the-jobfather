"""Learn from what the user actually sent: compare saved drafts with the sent versions word by word.

    learn_diff.py --pairs PAIRS.json [--checks CHECKS.json] [--min-messages 2] [--run-id ID]
    learn_diff.py --drafts DIR --sent DIR [...]

PAIRS.json is a list of {"draft", "sent", "subject", "thread_id", "recipient"} objects the host
collected with the mail connector (plain text or HTML bodies); the folder form pairs DIR/NAME.txt
files by name. Signatures (profile/signature.html and "-- " blocks), quoted threads and forwarded
headers are removed, and whitespace, punctuation spacing, typographic quotes and auto-link
artefacts are ignored. Results go to learning/diffs/<run_id>/: edit-comparison.json (per pair: ids
and an operations summary) and edit-counts.json (recurring edits ranked by how many messages show
them). Bodies, subjects and recipients are not stored; edit fragments are clipped and show a
paragraph break as ¶. The summary on
stdout is evidence for a VOICE.md proposal, and explicit rules in VOICE.md beat contrary examples.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import difflib
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata

import layout
from check_text import avoid_list
from importer import html_text, looks_html
from local import add_workspace_arg, atomic_json, contained, now, read_text, require_workspace

DIFFS = getattr(layout, 'DIFFS', layout.LEARNING + '/diffs')
MAX_PAIRS = 5000
MAX_BODY = 400000
RUN_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
WHERE = ('anywhere', 'opening', 'closing', 'first-line', 'last-line', 'any-line')
EXPECT = ('added', 'removed', 'changed', 'present')
PARA = '¶'  # paragraph break inside edit fragments
NGRAM_SPAN = 60  # longest inserted/deleted run mined for recurring phrases
STOP = set('a an the and or but nor to of in on for with at by from as is are was were be been being it its this '
           'that these those i me my we us our you your he him his she her they them their so if than then there '
           'here not no do does did have has had will would can could should may might just also very about into '
           'over up out all any more most some such what which who whom when where why how am'.split())
DEFAULT_CHECKS = [
    {'id': 'greeting-changed', 'label': 'Opening (greeting) changed', 'where': 'opening', 'expect': 'changed'},
    {'id': 'closing-changed', 'label': 'Closing (sign-off) changed', 'where': 'closing', 'expect': 'changed'},
    {'id': 'thanks-close-added', 'label': 'A "Thanks" closing was added', 'where': 'closing',
     'phrases': ['thanks', 'thank you'], 'expect': 'added'},
    {'id': 'regards-close-removed', 'label': 'A "regards" closing was removed', 'where': 'closing',
     'phrases': ['regards', 'best wishes', 'sincerely'], 'expect': 'removed'},
    {'id': 'link-line-added', 'label': 'A line with a link was added', 'where': 'any-line',
     'pattern': r'https?://|www\.|\b[a-z0-9-]+\.(?:com|org|net|io|dev|me|ai|co|app|example)\b', 'expect': 'added'},
    {'id': 'caveat-removed', 'label': 'A vacancy caveat was removed', 'expect': 'removed',
     'phrases': ['if the role is still open', 'if this role is still open', 'if the position is still open',
                 'if the position is still available', 'if you are still hiring', 'if you are hiring',
                 'in case the role']},
]
HTML_CUT = re.compile(
    r'<[^>]+\b(?:class|id)\s*=\s*["\']?[^"\'>]*\b(?:gmail_signature|gmail_quote|gmail_attr|yahoo_quoted|'
    r'protonmail_quote|protonmail_signature_block|moz-cite-prefix|moz-signature|divRplyFwdMsg|appendonsend|'
    r'x_divRplyFwdMsg|ms-outlook-mobile-signature|zmail_extra|Signature)\b|<blockquote\b|'
    r'<hr\b[^>]*\bid\s*=\s*["\']?stopSpelling', re.I)
REPLY_HEADER = re.compile(
    r'^[ \t]*(?:On\s.{0,300}?\swrote|Le\s.{0,300}?\sa\s+écrit|Am\s.{0,300}?\sschrieb\s*.{0,120}?|'
    r'El\s.{0,300}?\sescribió|Il\s+giorno\s.{0,300}?\sha\s+scritto|Op\s.{0,300}?\sgeschreven)[ \t]*:[ \t]*$',
    re.M | re.S | re.I)
FORWARD = re.compile(
    r'^[ \t]*(?:-{2,}[ \t]*(?:Forwarded message|Original Message|Message transf[ée]r[ée]|Weitergeleitete Nachricht|'
    r'Mensaje reenviado|Messaggio inoltrato)[ \t]*-{2,}|Begin forwarded message:|_{10,})[ \t]*$', re.M | re.I)
OUTLOOK_HEADER = re.compile(r'^[ \t]*From:[ \t]+\S.*\n[ \t]*(?:Sent|Date):[ \t]+\S', re.M | re.I)
SIG_DELIM = re.compile(r'^--[ \t]?$', re.M)
QUOTES = str.maketrans({'‘': "'", '’': "'", '‚': "'", '‛': "'", '′': "'",
                        '“': '"', '”': '"', '„': '"', '‟': '"', '″': '"', '…': '...'})
INVISIBLE = dict.fromkeys(map(ord, '​‌‍⁠﻿­'), None)
URL = re.compile(r'(?:https?://|mailto:|www\.)[^\s<>()"]+', re.I)


# ------------------------------------------------------------------ cleaning
def _cut_html(markup):
    m = HTML_CUT.search(markup)
    return markup[:m.start()] if m else markup


def strip_quoted(text):
    """Drop reply headers ("On ... wrote:") and everything after, forwarded blocks and "> " lines."""
    cut = len(text)
    for m in REPLY_HEADER.finditer(text):
        if re.search(r'@|\d{1,2}:\d{2}|\b(?:19|20)\d{2}\b', m.group(0)):
            cut = m.start()
            break
    for pattern in (FORWARD, OUTLOOK_HEADER):
        m = pattern.search(text)
        if m:
            cut = min(cut, m.start())
    return '\n'.join(line for line in text[:cut].split('\n') if not line.lstrip().startswith('>'))


def _token(word):
    return re.sub(r'^\W+|\W+$', '', word.casefold())


def signature_tokens(signature):
    text = html_text(signature) if looks_html(signature) else signature
    return [t for t in (_token(w) for w in text.split()) if t]


def strip_signature(text, signature=()):
    """Cut at a "-- " delimiter and at the user's saved signature (matched by its first words).

    A last line that is only the start of the signature (the user's name under "Thanks,") goes too,
    so a name typed in the draft and the same name inside the sent signature do not count as an edit.
    """
    m = SIG_DELIM.search(text)
    if m:
        text = text[:m.start()]
    if not signature:
        return text
    spans = [(m.start(), _token(m.group(0))) for m in re.finditer(r'\S+', text)]
    spans = [(pos, tok) for pos, tok in spans if tok]
    words = [tok for _, tok in spans]
    k = min(len(signature), 6)
    head = list(signature[:k])
    for i in range(len(words) - k, -1, -1):
        if words[i:i + k] == head and (k >= 3 or len(words) - i <= k + 3):
            text = text[:spans[i][0]]
            break
    lines = text.rstrip().split('\n')
    last = [t for t in (_token(w) for w in lines[-1].split()) if t] if lines else []
    if last and len(last) <= 3 and len(lines) > 1 and last == list(signature[:len(last)]):
        text = '\n'.join(lines[:-1])
    return text


def _canon_url(m):
    url, tail = m.group(0), ''
    while url and url[-1] in '.,;:!?)\'"':
        url, tail = url[:-1], url[-1] + tail
    url = re.sub(r'^(?:https?://|mailto:)', '', url, flags=re.I)
    url = re.sub(r'^www\.', '', url, flags=re.I).rstrip('/')
    host, slash, rest = url.partition('/')
    return host.casefold() + slash + rest + tail


def normalize(text):
    """Lines of comparable text: invisible characters, quote styles, auto-links and spacing artefacts removed."""
    text = unicodedata.normalize('NFKC', text).translate(INVISIBLE).translate(QUOTES)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'[ \t]*<(?:https?://|mailto:|tel:)[^>\s]*>', '', text)  # "label <url>" from HTML-to-text
    text = URL.sub(_canon_url, text)
    text = re.sub(r'[ \t]+([,.;:!?)\]}%])', r'\1', text)
    text = re.sub(r'([(\[{])[ \t]+', r'\1', text)
    lines = [' '.join(line.split()) for line in text.split('\n')]
    out = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    while out and not out[-1]:
        out.pop()
    return out


def clean(body, signature=()):
    """Comparable lines of one message body (text or HTML)."""
    if looks_html(body):
        body = html_text(_cut_html(body))
    return normalize(strip_signature(strip_quoted(body), signature))


# ------------------------------------------------------------------ comparison
def paragraphs(lines):
    out, current = [], []
    for line in lines:
        if line:
            current.append(line)
        elif current:
            out.append(current)
            current = []
    if current:
        out.append(current)
    return out


def _tokens(lines):
    """Words with paragraph markers and, per word, its place: opening (first paragraph), body or closing (last)."""
    words, where = [], []
    paras = paragraphs(lines)
    for p, para in enumerate(paras):
        position = 'opening' if p == 0 else ('closing' if p == len(paras) - 1 else 'body')
        if p:
            words.append(PARA)
            where.append(position)
        for line in para:
            for word in line.split():
                words.append(word)
                where.append(position)
    return words, where


def _plain(words):
    return [w for w in words if w != PARA]


def _clip(words, limit):
    if len(words) <= limit:
        return ' '.join(words), False
    head = max(1, limit - 4)
    return ' '.join(words[:head]) + ' … ' + ' '.join(words[-3:]), True


def _matcher(check):
    if check.get('pattern'):
        return re.compile(check['pattern'], re.I)
    if check.get('phrases'):
        return re.compile('|'.join(r'(?<!\w)%s(?!\w)' % re.escape(p) for p in check['phrases']), re.I)
    return None


def _part(lines, where):
    content = [line for line in lines if line]
    if where in ('opening', 'closing'):
        paras = paragraphs(lines)
        return (paras[0] if where == 'opening' else paras[-1]) if paras else []
    if where == 'first-line':
        return content[:1]
    if where == 'last-line':
        return content[-1:]
    return content


def evaluate(check, before, after):
    """Did this pair (lists of lines) show the checked edit?"""
    where, expect = check.get('where', 'anywhere'), check.get('expect', 'added')
    b, a = _part(before, where), _part(after, where)
    if expect == 'changed':
        return b != a
    matcher = check['_re']
    if where == 'any-line':
        mb, ma = {l for l in b if matcher.search(l)}, {l for l in a if matcher.search(l)}
        return bool({'added': ma - mb, 'removed': mb - ma, 'present': ma}[expect])
    pb, pa = any(matcher.search(l) for l in b), any(matcher.search(l) for l in a)
    return {'added': pa and not pb, 'removed': pb and not pa, 'present': pa}[expect]


def load_checks(value):
    checks = value.get('checks') if isinstance(value, dict) else value
    if not isinstance(checks, list) or not checks:
        raise ValueError('Checks file needs a non-empty list (or {"checks": [...]})')
    out = []
    for check in checks:
        if not isinstance(check, dict) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', str(check.get('id', ''))):
            raise ValueError('Each check needs an id (lowercase slug)')
        if check.get('where', 'anywhere') not in WHERE or check.get('expect', 'added') not in EXPECT:
            raise ValueError('Check %s: where is one of %s; expect is one of %s'
                             % (check['id'], ', '.join(WHERE), ', '.join(EXPECT)))
        phrases = check.get('phrases')
        if phrases is not None and (not isinstance(phrases, list) or not all(isinstance(p, str) and p.strip() and
                                                                             len(p) <= 200 for p in phrases)):
            raise ValueError('Check %s: phrases must be short non-empty strings' % check['id'])
        if check.get('pattern') is not None and (not isinstance(check['pattern'], str) or len(check['pattern']) > 500):
            raise ValueError('Check %s: pattern must be a short regular expression' % check['id'])
        try:
            matcher = _matcher(check)
        except re.error as error:
            raise ValueError('Check %s: bad pattern: %s' % (check['id'], error))
        if matcher is None and check.get('expect', 'added') != 'changed':
            raise ValueError('Check %s needs phrases or a pattern' % check['id'])
        out.append(dict(check, _re=matcher))
    return out


def _ngrams(words, size):
    """Phrases of 1..size words inside one edited run; they never cross a paragraph break."""
    grams = set()
    runs, current = [], []
    for word in words:
        if word == PARA:
            runs.append(current)
            current = []
        else:
            current.append(word)
    runs.append(current)
    for run_words in runs:
        tokens = [_token(w) for w in run_words[:NGRAM_SPAN]]
        for n in range(1, size + 1):
            for i in range(len(tokens) - n + 1):
                gram = tuple(tokens[i:i + n])
                if '' not in gram and not all(t in STOP for t in gram):
                    grams.add(gram)
    return grams


def _maximal(table, min_messages):
    """Recurring n-grams, dropping any that only occur inside a longer one in exactly the same messages."""
    kept = {g: ids for g, ids in table.items() if len(ids) >= min_messages}
    dominated = set()
    for gram, ids in kept.items():
        for n in range(1, len(gram)):
            for i in range(len(gram) - n + 1):
                part = gram[i:i + n]
                if kept.get(part) == ids:
                    dominated.add(part)
    rows = [(g, ids) for g, ids in kept.items() if g not in dominated]
    rows.sort(key=lambda row: (-len(row[1]), -len(row[0]), row[0]))
    return rows


def compare(pairs, signature=(), checks=None, min_messages=2, fragment_words=20, ngram_size=8):
    """Per-pair summaries and recurring edits for a list of {"id", "draft", "sent", ...} pairs."""
    checks = load_checks(DEFAULT_CHECKS) if checks is None else checks
    results, edits = [], {}
    added, removed = {}, {}
    positions, check_hits = Counter(), {c['id']: [] for c in checks}
    tallies = Counter()
    for pair in pairs:
        row = {'id': pair['id']}
        if pair.get('thread_id'):
            row['thread_id'] = pair['thread_id']
        if pair.get('skip'):
            row.update(status='skipped', reason=pair['skip'])
            tallies['skipped'] += 1
            results.append(row)
            continue
        before, after = clean(pair['draft'], signature), clean(pair['sent'], signature)
        a, a_where = _tokens(before)
        b, b_where = _tokens(after)
        matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
        ops, counts = [], Counter()
        pair_added, pair_removed = set(), set()
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == 'equal' or _plain(a[i1:i2]) == _plain(b[j1:j2]):
                continue  # equal, or only paragraph spacing differs
            where = b_where[j1] if tag == 'insert' else a_where[i1]
            old, old_cut = _clip(a[i1:i2], fragment_words)
            new, new_cut = _clip(b[j1:j2], fragment_words)
            op = {'op': tag, 'before': old, 'after': new, 'where': where}
            if old_cut or new_cut:
                op['clipped'] = True
            ops.append(op)
            counts[tag] += 1
            positions[where] += 1
            key = (tag, old.casefold(), new.casefold())
            entry = edits.setdefault(key, {'op': tag, 'forms': Counter(), 'ids': [], 'where': Counter()})
            entry['forms'][(old, new)] += 1
            entry['where'][where] += 1
            if pair['id'] not in entry['ids']:
                entry['ids'].append(pair['id'])
            pair_removed |= _ngrams(a[i1:i2], ngram_size)
            pair_added |= _ngrams(b[j1:j2], ngram_size)
        for gram in pair_added:
            added.setdefault(gram, set()).add(pair['id'])
        for gram in pair_removed:
            removed.setdefault(gram, set()).add(pair['id'])
        hits = [c['id'] for c in checks if evaluate(c, before, after)]
        for check_id in hits:
            check_hits[check_id].append(pair['id'])
        changed = bool(ops)
        tallies['changed' if changed else 'unchanged'] += 1
        row.update(status='changed' if changed else 'unchanged', words={'draft': len(_plain(a)), 'sent': len(_plain(b))},
                   similarity=round(matcher.ratio(), 3), ops=dict(counts), edits=ops, checks=hits)
        if pair.get('draft_subject') is not None and pair.get('sent_subject') is not None:
            row['subject_changed'] = ' '.join(pair['draft_subject'].split()) != ' '.join(pair['sent_subject'].split())
            tallies['subject_changed'] += int(row['subject_changed'])
        results.append(row)
    ranked = []
    for entry in edits.values():
        if len(entry['ids']) >= min_messages:
            (before, after), _ = entry['forms'].most_common(1)[0]
            ranked.append({'op': entry['op'], 'before': before, 'after': after, 'messages': len(entry['ids']),
                           'examples': entry['ids'][:5], 'where': entry['where'].most_common(1)[0][0]})
    ranked.sort(key=lambda e: (-e['messages'], e['op'], e['before'], e['after']))
    phrase_rows = lambda table: [{'text': ' '.join(g), 'messages': len(ids), 'examples': sorted(ids)[:5]}
                                 for g, ids in _maximal(table, min_messages)[:40]]
    compared = tallies['changed'] + tallies['unchanged']
    counts = {'pairs': {'total': len(pairs), 'compared': compared, 'changed': tallies['changed'],
                        'unchanged': tallies['unchanged'], 'skipped': tallies['skipped']},
              'min_messages': min_messages, 'edits': ranked[:60], 'added_phrases': phrase_rows(added),
              'removed_phrases': phrase_rows(removed), 'positions': dict(positions),
              'checks': [{'id': c['id'], 'label': c.get('label', c['id']), 'hits': len(check_hits[c['id']]),
                          'of': compared, 'examples': check_hits[c['id']][:5]} for c in checks]}
    if tallies['subject_changed']:
        counts['pairs']['subject_changed'] = tallies['subject_changed']
    return results, counts


# ------------------------------------------------------------------ voice rules
def use_list(voice_text):
    """Phrases under '## Words and phrases I use' in VOICE.md."""
    m = re.search(r'(?im)^##\s+words and phrases i use\s*$', voice_text or '')
    if not m:
        return []
    out = []
    for line in voice_text[m.end():].splitlines():
        if line.startswith('## '):
            break
        item = re.match(r'^\s*[-*]\s+(.+?)\s*$', line)
        if item:
            quoted = [a or b for a, b in re.findall(r'"([^"]+)"|“([^”]+)”', item.group(1))]
            out += quoted or [item.group(1).strip('"\'“”')]
    return [p.strip() for p in out if p.strip()]


def voice_conflicts(counts, voice_text):
    """Recurring edits that go against an explicit VOICE.md rule (the rule wins; ask before changing it)."""
    avoid = [p for p in avoid_list(voice_text) if p]
    use = use_list(voice_text)
    found = []

    def has(text, phrase):
        return re.search(r'(?<!\w)%s(?!\w)' % re.escape(phrase.casefold()), text.casefold()) is not None

    for edit in counts['edits']:
        for phrase in avoid:
            if edit['op'] != 'delete' and has(edit['after'], phrase) and not has(edit['before'], phrase):
                found.append({'edit': edit, 'rule': 'VOICE.md lists "%s" under words you avoid' % phrase})
        for phrase in use:
            if edit['op'] != 'insert' and has(edit['before'], phrase) and not has(edit['after'], phrase):
                found.append({'edit': edit, 'rule': 'VOICE.md lists "%s" under words you use' % phrase})
    for row in counts['added_phrases']:
        for phrase in avoid:
            if has(row['text'], phrase):
                found.append({'added_phrase': row['text'], 'messages': row['messages'],
                              'rule': 'VOICE.md lists "%s" under words you avoid' % phrase})
    for row in counts['removed_phrases']:
        for phrase in use:
            if has(row['text'], phrase):
                found.append({'removed_phrase': row['text'], 'messages': row['messages'],
                              'rule': 'VOICE.md lists "%s" under words you use' % phrase})
    return found


# ------------------------------------------------------------------ inputs
def _id(value, number, seen):
    base = str(value or 'pair-%03d' % number)[:120]
    key, n = base, 2
    while key in seen:
        key, n = '%s#%d' % (base, n), n + 1
    seen.add(key)
    return key


def load_pairs(value):
    items = value.get('pairs') if isinstance(value, dict) else value
    if not isinstance(items, list):
        raise ValueError('Pairs file needs a JSON list of {"draft", "sent", ...} objects (or {"pairs": [...]})')
    if len(items) > MAX_PAIRS:
        raise ValueError('At most %d pairs per run' % MAX_PAIRS)
    pairs, seen = [], set()
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError('Pair %d is not an object' % number)
        pair = {'id': _id(item.get('thread_id') or item.get('id') or item.get('message_id'), number, seen)}
        for key in ('thread_id', 'draft_subject', 'sent_subject'):
            if isinstance(item.get(key), str):
                pair[key] = item[key]
        draft, sent = item.get('draft'), item.get('sent')
        if not isinstance(sent, str) or not sent.strip():
            pair['skip'] = 'no sent version (not sent, or not matched)'
        elif not isinstance(draft, str) or not draft.strip():
            pair['skip'] = 'no saved draft'
        elif len(draft) > MAX_BODY or len(sent) > MAX_BODY:
            pair['skip'] = 'message too long to compare'
        else:
            pair.update(draft=draft, sent=sent)
        pairs.append(pair)
    return pairs


def load_folders(drafts, sent):
    def files(folder):
        folder = Path(folder)
        if not folder.is_dir():
            raise ValueError('Not a folder: %s' % folder.name)
        return {p.stem: p for p in sorted(folder.iterdir())
                if p.is_file() and not p.is_symlink() and p.suffix.casefold() in ('.txt', '.md', '.html', '.htm')}

    left, right = files(drafts), files(sent)
    pairs = []
    for stem in sorted(set(left) | set(right)):
        pair = {'id': stem}
        if stem not in right:
            pair['skip'] = 'no sent version (not sent, or not matched)'
        elif stem not in left:
            pair['skip'] = 'no saved draft'
        elif left[stem].stat().st_size > MAX_BODY or right[stem].stat().st_size > MAX_BODY:
            pair['skip'] = 'message too long to compare'
        else:
            pair.update(draft=left[stem].read_bytes().decode('utf-8', errors='replace'),
                        sent=right[stem].read_bytes().decode('utf-8', errors='replace'))
        pairs.append(pair)
    return pairs


def run(ws, pairs, checks=None, min_messages=2, run_id=None, signature=None, fragment_words=20, top=8):
    """Compare, write learning/diffs/<run_id>/ and return the short summary."""
    ws = require_workspace(ws)
    if min_messages < 1:
        raise ValueError('--min-messages must be at least 1')
    if signature is None:
        path = contained(ws, layout.SIGNATURE)
        signature = read_text(path) if path.is_file() else ''
    sig = signature_tokens(signature) if signature else []
    checks = load_checks(DEFAULT_CHECKS if checks is None else checks)
    results, counts = compare(pairs, sig, checks, min_messages, fragment_words)
    voice = contained(ws, layout.VOICE)
    counts['voice_conflicts'] = voice_conflicts(counts, read_text(voice) if voice.is_file() else '')
    if run_id is None:
        fingerprint = hashlib.sha256(json.dumps([pairs, min_messages, [c['id'] for c in checks]],
                                                sort_keys=True).encode('utf-8')).hexdigest()[:8]
        run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + fingerprint
    if not RUN_ID.match(run_id):
        raise ValueError('Run id: letters, digits, "-" and "_" only')
    directory = contained(ws, DIFFS + '/' + run_id)
    if directory.exists():
        raise ValueError('Run %s already exists; pass another --run-id' % run_id)
    stamp = {'run_id': run_id, 'generated_at': now(), 'signature_stripped': bool(sig)}
    atomic_json(directory / 'edit-comparison.json', dict(stamp, schema='the-jobfather/edit-comparison@1',
                                                         pairs=results))
    atomic_json(directory / 'edit-counts.json', dict(stamp, schema='the-jobfather/edit-counts@1', **counts))
    return {'run_id': run_id, 'directory': DIFFS + '/' + run_id, 'pairs': counts['pairs'],
            'top_edits': [{k: e[k] for k in ('op', 'before', 'after', 'messages', 'where')}
                          for e in counts['edits'][:top]],
            'added_phrases': [{k: r[k] for k in ('text', 'messages')} for r in counts['added_phrases'][:top]],
            'removed_phrases': [{k: r[k] for k in ('text', 'messages')} for r in counts['removed_phrases'][:top]],
            'checks': [{k: c[k] for k in ('id', 'hits', 'of')} for c in counts['checks'] if c['hits']],
            'voice_conflicts': counts['voice_conflicts'],
            'next': ('Propose at most one or two VOICE.md rules, only for edits seen in at least %d messages, '
                     'with the counts as evidence. Explicit rules already in VOICE.md win over these examples; '
                     'ask about any conflict instead of changing the rule. Save only what the user approves '
                     '(memory.py).' % min_messages)}


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--pairs', type=Path, help='JSON list of {"draft", "sent", "subject", "thread_id", "recipient"}')
    source.add_argument('--drafts', type=Path, help='folder of draft .txt files (use with --sent)')
    parser.add_argument('--sent', type=Path, help='folder of sent .txt files with the same names as the drafts')
    parser.add_argument('--checks', type=Path, help='JSON list of targeted checks (replaces the defaults)')
    parser.add_argument('--min-messages', type=int, default=2, help='report edits seen in at least this many messages')
    parser.add_argument('--run-id')
    parser.add_argument('--signature', type=Path, help='signature file to strip (default: profile/signature.html)')
    parser.add_argument('--no-signature', action='store_true', help='do not strip a saved signature')
    parser.add_argument('--fragment-words', type=int, default=20, help='longest edit fragment kept, in words')
    parser.add_argument('--top', type=int, default=8)
    args = parser.parse_args()
    if args.drafts and not args.sent:
        parser.error('--drafts needs --sent')
    pairs = load_pairs(json.loads(read_text(args.pairs))) if args.pairs else load_folders(args.drafts, args.sent)
    checks = json.loads(read_text(args.checks)) if args.checks else None
    signature = '' if args.no_signature else (read_text(args.signature) if args.signature else None)
    result = run(args.workspace, pairs, checks, args.min_messages, args.run_id, signature,
                 max(4, args.fragment_words), max(1, args.top))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit('Draft comparison failed: ' + str(error))
