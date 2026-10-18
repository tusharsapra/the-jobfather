"""Controlled segmentation of the user's master CV using persistent `% @jf:` markers.

Everything outside a slot is locked and hashed; slots hold plain text that is
escaped once on the way in. Unchanged slots keep their original TeX byte-for-byte.
"""
import hashlib
from pathlib import Path
import re
import unicodedata

import layout
from local import read_json, read_text

ESCAPES = {'\\': r'\textbackslash{}', '%': r'\%', '&': r'\&', '$': r'\$', '_': r'\_', '#': r'\#',
           '{': r'\{', '}': r'\}', '~': r'\textasciitilde{}', '^': r'\textasciicircum{}'}
DECODE = sorted(((v, k) for k, v in ESCAPES.items()), key=lambda kv: -len(kv[0]))
MARKER = re.compile(r'^[ \t]*% @jf:(slot|entry|end)[ \t]+([A-Za-z][A-Za-z0-9_-]{0,31})((?:[ \t]+[a-z_]+=(?:"[^"]*"|\S+))*)[ \t]*$')
ATTR = re.compile(r'([a-z_]+)=("[^"]*"|\S+)')
TYPOGRAPHIC = set('‘’“”–—…•€£¥©®™°±×÷·\u00a0')


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def escape(text):
    """One pass, so emitted commands are never escaped a second time."""
    return ''.join(ESCAPES.get(char, char) for char in text)


def unsupported_chars(text):
    """Characters the classic pdfLaTeX template (T1 + utf8) cannot typeset reliably."""
    bad = set()
    for char in text:
        if char in TYPOGRAPHIC or (char.isascii() and (char.isprintable() or char in '\n\t')):
            continue
        name = unicodedata.name(char, '')
        if 'LATIN' in name and ord(char) < 0x250:
            continue
        bad.add(char)
    return sorted(bad)


def fragment(tex):
    """Decode generated slot TeX (escapes and one level of \\textbf) back to text and bold spans."""
    def parse(start, nested=False):
        output, spans, index = [], [], start
        while index < len(tex):
            if tex.startswith(r'\textbf{', index):
                if nested:
                    raise ValueError('Nested emphasis is not supported')
                inner, _, index = parse(index + len(r'\textbf{'), True)
                offset = len(''.join(output))
                output.append(inner)
                spans.append({'start': offset, 'end': offset + len(inner)})
                continue
            if tex[index] == '}' and nested:
                return ''.join(output), spans, index + 1
            if tex[index] == '\\':
                for code, char in DECODE:
                    if tex.startswith(code, index):
                        output.append(char)
                        index += len(code)
                        break
                else:
                    raise ValueError('Unknown command inside CV text: ' + tex[index:index + 24])
                continue
            if tex[index] in '{}':
                raise ValueError('Unexpected brace inside CV text')
            output.append(' ' if tex[index] == '~' else tex[index])
            index += 1
        if nested:
            raise ValueError('Unbalanced emphasis')
        return ''.join(output), spans, index
    text, spans, _ = parse(0)
    return {'text': text, 'bold_spans': spans}


def markdown_bold(text):
    """'**bold** words' -> ('bold words', [{'start': 0, 'end': 4}])."""
    parts = text.split('**')
    if len(parts) % 2 == 0:
        raise ValueError('Unbalanced ** emphasis in: ' + text)
    plain, spans = '', []
    for i, part in enumerate(parts):
        if i % 2:
            if not part.strip():
                raise ValueError('Empty ** emphasis in: ' + text)
            spans.append({'start': len(plain), 'end': len(plain) + len(part)})
        plain += part
    return plain, spans


def render(text, spans):
    output, last = [], 0
    for span in spans:
        output.append(escape(text[last:span['start']]))
        output.append(r'\textbf{' + escape(text[span['start']:span['end']]) + '}')
        last = span['end']
    output.append(escape(text[last:]))
    return ''.join(output)


def _attrs(raw):
    return {k: v.strip('"') for k, v in ATTR.findall(raw or '')}


def parse(source):
    """Find marker-delimited entries and slots: {'slots': {...}, 'entries': {...}, 'order': [...]}."""
    slots, entries, stack, order = {}, {}, [], []
    offset = 0
    for line in source.splitlines(keepends=True):
        stripped = line.rstrip('\r\n')
        m = MARKER.match(stripped)
        if m:
            kind, ident, attrs = m.group(1), m.group(2), _attrs(m.group(3))
            if kind in ('slot', 'entry'):
                if kind == 'entry' and any(s[0] == 'slot' for s in stack):
                    raise ValueError('An entry cannot sit inside a slot')
                if kind == 'slot' and any(s[0] == 'slot' for s in stack):
                    raise ValueError('Slots cannot nest')
                if ident in slots or ident in entries:
                    raise ValueError('Duplicate marker id: ' + ident)
                stack.append((kind, ident, attrs, offset + len(line)))
            else:
                if not stack or stack[-1][1] != ident:
                    raise ValueError('Unmatched end marker: ' + ident)
                kind, ident, attrs, begin = stack.pop()
                end = max(begin, offset - 1)  # stop before the newline preceding the end marker
                item = {'kind': attrs.get('kind', 'entry'), 'attrs': attrs, 'begin': begin, 'end': end,
                        'entry': next((s[1] for s in reversed(stack) if s[0] == 'entry'), None)}
                if kind == 'slot':
                    slots[ident] = item
                else:
                    entries[ident] = item
                order.append((kind, ident))
        offset += len(line)
    if stack:
        raise ValueError('Unclosed marker: ' + stack[-1][1])
    return {'slots': slots, 'entries': entries, 'order': order}


def item_ranges(source, begin, end, expected, label):
    """Top-level \\item bodies between begin and end (brace- and comment-aware)."""
    body = source[begin:end]
    items, depth, index = [], 0, 0
    while index < len(body):
        char = body[index]
        if char == '%':
            index = body.find('\n', index)
            if index == -1:
                break
        elif char == '\\':
            token = re.match(r'\\([A-Za-z]+|.)', body[index:])
            if token is None:
                raise ValueError('Incomplete TeX token')
            if token.group(1) == 'item' and depth == 0:
                items.append(index)
            index += len(token.group(0))
            continue
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
        if depth < 0:
            raise ValueError('Unbalanced bullet braces')
        index += 1
    if depth or len(items) != expected:
        raise ValueError('Expected %d bullets in %s, found %d' % (expected, label, len(items)))
    ranges = []
    for i, position in enumerate(items):
        start = position + len(r'\item')
        stop = items[i + 1] if i + 1 < len(items) else len(body)
        while start < stop and body[start].isspace():
            start += 1
        while stop > start and body[stop - 1].isspace():
            stop -= 1
        ranges.append((begin + start, begin + stop))
    return ranges


def load_master(ws):
    ws = Path(ws)
    if not (ws / layout.CV_MASTER).is_file() or not (ws / layout.CV_MANIFEST).is_file():
        raise ValueError('No master CV yet; run cv_init.py render, check and adopt')
    source = read_text(ws / layout.CV_MASTER)
    manifest = read_json(ws / layout.CV_MANIFEST)
    if sha(source) != manifest['master']['sha256']:
        raise ValueError('The master CV changed outside The Jobfather; re-render and adopt it with cv_init.py')
    return source, manifest


def segment(source, manifest):
    """Locked chunks plus named slots for a master source and its manifest."""
    parsed = parse(source)
    if set(parsed['slots']) != set(manifest['slots']):
        raise ValueError('Master markers and manifest slots disagree')
    ranges = []
    for ident, slot in parsed['slots'].items():
        policy = manifest['slots'][ident]
        if slot['kind'] != policy['kind']:
            raise ValueError('Slot %s kind differs from the manifest' % ident)
        if policy['kind'] == 'bullets':
            for i, (start, end) in enumerate(item_ranges(source, slot['begin'], slot['end'], policy['count'], ident)):
                ranges.append((start, end, '%s.%d' % (ident, i)))
        else:
            ranges.append((slot['begin'], slot['end'], ident))
    ranges.sort()
    chunks, slots, last = [], {}, 0
    for start, end, name in ranges:
        if start < last:
            raise ValueError('Overlapping CV slots')
        chunks.append({'kind': 'locked', 'text': source[last:start], 'sha256': sha(source[last:start])})
        raw = source[start:end]
        slots[name] = dict(fragment(raw), tex=raw)
        chunks.append({'kind': 'slot', 'name': name})
        last = end
    chunks.append({'kind': 'locked', 'text': source[last:], 'sha256': sha(source[last:])})
    baseline = {}
    for ident, policy in manifest['slots'].items():
        if policy['kind'] == 'bullets':
            baseline[ident] = [slots['%s.%d' % (ident, i)]['text'] for i in range(policy['count'])]
        elif policy['kind'] == 'list':
            baseline[ident] = [x.strip() for x in slots[ident]['text'].split(policy.get('sep', ', ').strip()) if x.strip()]
        else:
            baseline[ident] = slots[ident]['text']
    return {'master_sha256': sha(source), 'chunks': chunks, 'slots': slots, 'baseline_patch': baseline,
            'manifest': manifest}


def template(ws):
    source, manifest = load_master(ws)
    return segment(source, manifest)


def value(patch, name, manifest):
    if '.' in name:
        ident, index = name.rsplit('.', 1)
        return patch[ident][int(index)]
    item = patch[name]
    if isinstance(item, list):
        return manifest['slots'][name].get('sep', ', ').join(item)
    return item


def emphasis_for(text, original, explicit=None):
    if explicit is not None:
        spans = explicit
    else:
        spans = []
        for span in original['bold_spans']:
            phrase = original['text'][span['start']:span['end']]
            if text.count(phrase) == 1:
                start = text.index(phrase)
                spans.append({'start': start, 'end': start + len(phrase)})
    if not isinstance(spans, list):
        raise ValueError('Emphasis must be a list of offsets')
    cleaned = []
    for span in spans:
        if not isinstance(span, dict) or set(span) != {'start', 'end'}:
            raise ValueError('Emphasis accepts only start/end offsets')
        start, end = span['start'], span['end']
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise ValueError('Invalid emphasis offsets')
        if start == 0 and end == len(text):
            raise ValueError('Do not bold a whole bullet')
        cleaned.append({'start': start, 'end': end})
    cleaned.sort(key=lambda x: x['start'])
    if any(a['end'] > b['start'] for a, b in zip(cleaned, cleaned[1:])):
        raise ValueError('Overlapping emphasis spans')
    return cleaned


def assemble(patch, emphasis=None, ws=None, data=None):
    import cv_content
    data = data or template(ws)
    checked = cv_content.validate(patch, ws, data=data)
    if not checked['ok']:
        raise ValueError('; '.join(checked['errors']))
    manifest = data['manifest']
    emphasis = emphasis or {}
    allowed = {n for n in data['slots'] if '.' in n and manifest['slots'][n.rsplit('.', 1)[0]].get('emphasis')}
    if not isinstance(emphasis, dict) or set(emphasis) - allowed:
        raise ValueError('Explicit emphasis is allowed only inside editable bullets')
    output, protected, offset = [], [], 0
    for chunk in data['chunks']:
        if chunk['kind'] == 'locked':
            text = chunk['text']
            protected.append({'start': offset, 'end': offset + len(text), 'sha256': chunk['sha256']})
        else:
            name = chunk['name']
            original = data['slots'][name]
            if value(patch, name, manifest) == value(data['baseline_patch'], name, manifest) and name not in emphasis:
                text = original['tex']
            else:
                content = value(patch, name, manifest)
                spans = emphasis_for(content, original, emphasis.get(name))
                text = render(content, spans)
        output.append(text)
        offset += len(text)
    source = ''.join(output)
    return {'source': source, 'source_sha256': sha(source), 'protected_spans': protected,
            'master_sha256': data['master_sha256'], 'slot_count': len(data['slots'])}


def check_source(source, assembled):
    problems = []
    for span in assembled['protected_spans']:
        if sha(source[span['start']:span['end']]) != span['sha256']:
            problems.append('Protected source span modified')
    if sha(source) != assembled['source_sha256']:
        problems.append('Source differs from the reviewed content assembly')
    return {'ok': not problems, 'errors': sorted(set(problems)), 'protected_spans': len(assembled['protected_spans'])}


# ------------------------------------------------------------------ locked visible text
VISIBLE_MACROS = {'jfName': 1, 'jfText': 1, 'jfLabel': 1, 'section': 1, 'resumeEntry': 4, 'resumeGroup': 2}


def _groups(text, index, count):
    """Read `count` brace groups after index, skipping whitespace; returns (args, end)."""
    args = []
    while len(args) < count:
        while index < len(text) and text[index] in ' \t\r\n':
            index += 1
        if index >= len(text) or text[index] != '{':
            raise ValueError('Expected a {group}')
        depth, start = 0, index
        while index < len(text):
            char = text[index]
            if char == '\\':
                index += 2
                continue
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    break
            index += 1
        args.append(text[start + 1:index])
        index += 1
    return args, index


def locked_visible_text(source):
    """Human-visible strings outside slots, in document order (for PDF validation)."""
    parsed = parse(source)
    masked = list(source)
    for slot in parsed['slots'].values():
        for i in range(slot['begin'], slot['end']):
            masked[i] = ' '
    body = ''.join(masked)
    start = body.find(r'\begin{document}')
    body = body[start:] if start >= 0 else body
    found = []
    for m in re.finditer(r'\\(jfName|jfText|jfLabel|section|resumeEntry|resumeGroup|href)(?![A-Za-z])', body):
        name = m.group(1)
        args, _ = _groups(body, m.end(), 2 if name == 'href' else VISIBLE_MACROS[name])
        for arg in (args[1:] if name == 'href' else args):
            if arg.strip():
                found.append(fragment(arg.strip())['text'])
    # Locked bullets: \item bodies outside slots (slot text was masked to spaces).
    for m in re.finditer(r'\\item\s+(.+?)\s*(?=\\item|\\end\{resumeBullets\})', body, re.S):
        text = m.group(1).strip()
        if text:
            found.append(fragment(text)['text'])
    return found
