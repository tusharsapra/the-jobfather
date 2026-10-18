"""Import the user's own sources: copy them into the workspace, index them and extract text.

    importer.py add FILE... [--kind cv|linkedin-export|writing-sample|job-post|other] [--note TEXT]
    importer.py list
    importer.py text SOURCE_ID
    importer.py linkedin SOURCE_ID [--markdown]

Originals are copied (never moved) into profile/sources/ and listed in profile/sources/index.json.
Readable text comes from pdftotext (PDF), the document XML (DOCX), a macro stripper (TeX), an HTML
stripper, or the file itself (Markdown, text). A LinkedIn data export ZIP is parsed into structured
JSON, and draft_entries() proposes EXPERIENCE.md sections from it. The host assistant reads the
text and drafts the profile with the user; this script never writes a managed profile file, never
follows links or includes, and never runs anything found inside a source.
"""
import argparse
import csv
import hashlib
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import unicodedata
import xml.etree.ElementTree as ET
from xml.parsers import expat
import zipfile

if __name__ == '__main__':
    sys.modules['importer'] = sys.modules[__name__]

import evidence
import layout
from local import (add_workspace_arg, atomic_bytes, atomic_json, atomic_text, contained, digest, now, read_json,
                   read_text, require_workspace, shared_lock)

KINDS = ('cv', 'linkedin-export', 'writing-sample', 'job-post', 'other')
MAX_BYTES = 20 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024  # largest archive member read into memory (zip-bomb guard)
MAX_TEXT_CHARS = 2000000
INDEX = getattr(layout, 'SOURCES_INDEX', layout.SOURCES + '/index.json')
LOCK = layout.LOCKS + '/sources.lock'
SOURCE_ID = re.compile(r'^SRC-[0-9a-f]{16}$')
UNSUPPORTED = {
    '.doc': 'Old Word format: save it as DOCX or PDF and import again, or read the original yourself.',
    '.rtf': 'RTF is not converted: save it as DOCX or PDF, or read the original yourself.',
    '.odt': 'OpenDocument is not converted: save it as DOCX or PDF, or read the original yourself.',
    '.pages': 'Pages files are not converted: export as PDF or DOCX, or read the original yourself.',
    '.xlsx': 'Spreadsheets are not converted: read the original yourself.',
    '.pptx': 'Slides are not converted: export as PDF, or read the original yourself.',
}
IMAGES = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.heic', '.tif', '.tiff', '.bmp')


# ------------------------------------------------------------------ helpers
def tidy(text):
    """Normalise newlines, trim line ends and collapse runs of blank lines (inner spacing kept)."""
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\f', '\n')
    lines = [line.rstrip() for line in text.split('\n')]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip('\n')


def decode(data, warnings=None):
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        if warnings is not None:
            warnings.append('Not UTF-8; decoded as Windows-1252, so check unusual characters.')
        return data.decode('cp1252', errors='replace')


def safe_name(name):
    name = unicodedata.normalize('NFKD', Path(str(name).replace('\\', '/')).name)
    name = name.encode('ascii', 'ignore').decode('ascii')
    stem, dot, ext = name.rpartition('.')
    if not dot or not stem:
        stem, ext = name, ''
    stem = re.sub(r'[^A-Za-z0-9._-]+', '-', stem).strip('.-_')[:60] or 'source'
    ext = re.sub(r'[^A-Za-z0-9]+', '', ext).lower()[:10]
    return stem + ('.' + ext if ext else '')


def _result(text, method, ok, warnings, linkedin=None):
    if text is not None:
        text = tidy(text)
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS]
            warnings.append('Text cut at %d characters; read the original for the rest.' % MAX_TEXT_CHARS)
        if not text.strip():
            text = None
    return {'text': text, 'linkedin': linkedin,
            'extraction': {'method': method, 'ok': bool(ok and text), 'warnings': warnings,
                           'chars': len(text) if text else 0}}


# ------------------------------------------------------------------ HTML
class _HTMLText(HTMLParser):
    BLOCK = {'p', 'div', 'section', 'article', 'header', 'footer', 'main', 'aside', 'nav', 'ul', 'ol', 'dl',
             'dt', 'dd', 'table', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'hr',
             'address', 'figure', 'figcaption', 'form', 'fieldset'}
    SKIP = {'script', 'style', 'head', 'title', 'noscript', 'template', 'svg', 'object', 'iframe'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif self.skip:
            return
        elif tag == 'br':
            self.parts.append('\n')
        elif tag == 'li':
            self.parts.append('\n- ')
        elif tag in ('td', 'th'):
            self.parts.append(' | ')
        elif tag in self.BLOCK:
            self.parts.append('\n')

    def handle_startendtag(self, tag, attrs):
        if tag not in self.SKIP:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif not self.skip and tag in self.BLOCK:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(re.sub(r'\s+', ' ', data))


def html_text(markup):
    """Readable text from HTML: scripts and styles dropped, blocks become lines, links keep their text."""
    parser = _HTMLText()
    parser.feed(markup)
    parser.close()
    lines = [re.sub(r'^\s*\|\s*', '', re.sub(r' {2,}', ' ', line)).strip() for line in ''.join(parser.parts).split('\n')]
    return tidy('\n'.join(lines))


def looks_html(text):
    return re.search(r'<(?:!doctype html|html|body|div|p|br|span|table|ul|li|a)\b[^>]*>', text[:20000], re.I) is not None


# ------------------------------------------------------------------ TeX
TEX_SECTIONS = {'part', 'chapter', 'section', 'subsection', 'subsubsection', 'paragraph', 'subparagraph',
                'cvsection', 'sectiontitle'}
TEX_ONE_ARG = {'textbf', 'textit', 'emph', 'underline', 'uline', 'textsc', 'texttt', 'textrm', 'textsf',
               'textup', 'textmd', 'textnormal', 'textsl', 'mbox', 'hbox', 'text', 'fbox', 'MakeUppercase',
               'MakeLowercase', 'uppercase', 'lowercase', 'footnote', 'centerline', 'small', 'large',
               'textsuperscript', 'textsubscript'}
TEX_LAST_ARG = {'textcolor': 2, 'colorbox': 2, 'fcolorbox': 3, 'raisebox': 2, 'makebox': 1, 'parbox': 2,
                'rotatebox': 2, 'scalebox': 2, 'resizebox': 3, 'hyperlink': 2, 'hypertarget': 2}
TEX_DROP = {'vspace': 1, 'hspace': 1, 'vskip': 1, 'hskip': 1, 'setlength': 2, 'addtolength': 2, 'setcounter': 2,
            'addtocounter': 2, 'newcommand': 2, 'renewcommand': 2, 'providecommand': 2, 'newenvironment': 3,
            'renewenvironment': 3, 'usepackage': 1, 'RequirePackage': 1, 'documentclass': 1, 'pagestyle': 1,
            'thispagestyle': 1, 'geometry': 1, 'newgeometry': 1, 'hypersetup': 1, 'titleformat': 5,
            'titlespacing': 4, 'definecolor': 3, 'color': 1, 'fontsize': 2, 'fontfamily': 1, 'includegraphics': 1,
            'label': 1, 'ref': 1, 'pageref': 1, 'cite': 1, 'input': 1, 'include': 1, 'includeonly': 1,
            'bibliography': 1, 'bibliographystyle': 1, 'phantom': 1, 'hphantom': 1, 'vphantom': 1, 'rule': 2,
            'linespread': 1, 'setmainfont': 1, 'setsansfont': 1, 'setmonofont': 1, 'pagenumbering': 1,
            'captionsetup': 1, 'setlist': 1, 'cline': 1, 'cmidrule': 1, 'addbibresource': 1, 'pdfbookmark': 2,
            'urlstyle': 1, 'moderncvstyle': 1, 'moderncvcolor': 1, 'photo': 1, 'enlargethispage': 1,
            'newlength': 1, 'graphicspath': 1}
TEX_SYMBOLS = {'LaTeX': 'LaTeX', 'TeX': 'TeX', 'ldots': '...', 'dots': '...', 'textellipsis': '...',
               'textbullet': '•', 'bullet': '•', 'cdot': '·', 'textperiodcentered': '·', 'textbar': '|',
               'vert': '|', 'mid': '|', 'textendash': '–', 'textemdash': '—', 'textasciitilde': '~',
               'textbackslash': '\\', 'copyright': '©', 'pounds': '£', 'euro': '€', 'texteuro': '€',
               'textdollar': '$', 'S': '§', 'ss': 'ß', 'o': 'ø', 'O': 'Ø', 'ae': 'æ', 'AE': 'Æ', 'aa': 'å',
               'AA': 'Å', 'l': 'ł', 'L': 'Ł', 'newline': '\n', 'linebreak': '\n', 'par': '\n',
               'quad': ' ', 'qquad': ' ', 'enspace': ' ', 'thinspace': ' ', 'space': ' ', 'times': '×',
               'rightarrow': '→', 'to': '→', 'textrightarrow': '→', 'pm': '±', 'textpm': '±',
               'textdegree': '°', 'degree': '°', 'textregistered': '®', 'texttrademark': '™', 'cr': '\n'}
TEX_ACCENTS = {"'": '\u0301', '`': '\u0300', '^': '\u0302', '"': '\u0308', '~': '\u0303', '=': '\u0304',
               '.': '\u0307', 'u': '\u0306', 'v': '\u030c', 'H': '\u030b', 'c': '\u0327', 'k': '\u0328',
               'r': '\u030a', 'd': '\u0323', 'b': '\u0331'}
TEX_ENV_ARGS = {'tabular': 1, 'tabular*': 2, 'tabularx': 2, 'tabulary': 2, 'longtable': 1, 'array': 1,
                'minipage': 1, 'multicols': 1, 'multicols*': 1, 'adjustbox': 1, 'wrapfigure': 2,
                'list': 2, 'paracol': 1, 'tcolorbox': 0, 'cvcolumns': 0}


def _brace(s, i):
    """s[i] == '{': return (index after the matching brace, inner text)."""
    depth, j, n = 0, i, len(s)
    while j < n:
        c = s[j]
        if c == '\\':
            j += 2
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return j + 1, s[i + 1:j]
        j += 1
    return n, s[i + 1:]


def _bracket(s, i):
    """s[i] == '[': return (index after the matching bracket, inner text), braces nest inside."""
    depth, j, n, lines = 0, i + 1, len(s), 0
    while j < n:
        c = s[j]
        if c == '\\':
            j += 2
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
        elif c == ']' and depth <= 0:
            return j + 1, s[i + 1:j]
        elif c == '\n':
            lines += 1
            if lines > 2 and depth <= 0:
                break  # a stray "[" in text, not an option
        j += 1
    return None, None


def _next(s, k):
    """Index of the next non-space character, or None when a blank line (paragraph) comes first."""
    j, newlines, n = k, 0, len(s)
    while j < n and s[j] in ' \t\r\n':
        newlines += s[j] == '\n'
        j += 1
    return None if newlines > 1 or j >= n else j


def _options(s, k):
    """Skip [..] option groups after a command."""
    while True:
        j = _next(s, k)
        if j is None or s[j] != '[':
            return k
        end, _ = _bracket(s, j)
        if end is None:
            return k
        k = end


def _arguments(s, k, limit=None):
    """Up to `limit` mandatory {..} groups (options in between are skipped); a blank line ends them."""
    args = []
    while limit is None or len(args) < limit:
        j = _next(s, k)
        if j is None:
            break
        if s[j] == '[':
            end, _ = _bracket(s, j)
            if end is None:
                break
            k = end
        elif s[j] == '{':
            k, inner = _brace(s, j)
            args.append(inner)
        else:
            break
    return k, args


def _tex_command(s, i, out, depth):
    n = len(s)
    j = i + 1
    if j >= n:
        return n
    if s[j].isalpha() or s[j] == '@':
        k = j
        while k < n and (s[k].isalpha() or s[k] == '@'):
            k += 1
        name = s[j:k]
    else:
        name, k = s[j], j + 1
    if name in '&%$#_{}':
        out.append(name)
        return k
    if name == '\\':
        if k < n and s[k] == '*':
            k += 1
        if k < n and s[k] == '[':
            end, _ = _bracket(s, k)
            k = end or k
        m = re.match(r'[ \t]*\r?\n', s[k:k + 200])
        out.append('\n')
        return k + (m.end() if m else 0)  # the source line break after \\ is the same break
    if name in TEX_ACCENTS and (len(name) == 1 and not name.isalpha() or k < n and s[k] in '{ '):
        m = k
        while m < n and s[m] == ' ':
            m += 1
        if m < n and s[m] == '{':
            m, base = _brace(s, m)
            base = _tex(base, depth + 1)
        elif m < n:
            base, m = s[m], m + 1
        else:
            base = ''
        out.append(unicodedata.normalize('NFC', base[:1] + TEX_ACCENTS[name] + base[1:]) if base else '')
        return m
    if not (name.isalpha() or '@' in name):
        out.append(' ' if name in ', ;:' else '')
        return k
    if k < n and s[k] == '*':
        k += 1
    if name in ('begin', 'end'):
        while k < n and s[k] == ' ':
            k += 1
        env = ''
        if k < n and s[k] == '{':
            k, env = _brace(s, k)
        if name == 'begin':
            k = _options(s, k)
            if TEX_ENV_ARGS.get(env.strip()):
                k, _ = _arguments(s, k, TEX_ENV_ARGS[env.strip()])
            k = _options(s, k)
        out.append('\n')
        return k
    if name in TEX_SYMBOLS:
        if s.startswith('{}', k):
            k += 2
        out.append(TEX_SYMBOLS[name])
        return k
    while k < n and s[k] in ' \t':
        k += 1  # TeX eats the space after a control word
    if name == 'item':
        end, label = (_bracket(s, k) if k < n and s[k] == '[' else (None, None))
        out.append('\n- ' + (_tex(label, depth + 1).strip() + ' ' if label else ''))
        return end or k
    if name in TEX_DROP:
        return _arguments(s, k, TEX_DROP[name])[0]
    limit = 1 if (name in TEX_ONE_ARG or name in TEX_SECTIONS or name == 'url') else (
        2 if name == 'href' else TEX_LAST_ARG.get(name))
    end, args = _arguments(s, k, limit)
    if not args:
        return k
    if name == 'url':
        out.append(args[0].strip())
        return end
    if name == 'href':
        url = args[0].strip()
        label = _tex(args[1], depth + 1).strip() if len(args) > 1 else ''
        plain = re.sub(r'^(?:mailto:|tel:|https?://)', '', url).rstrip('/')
        out.append(label if label in (url, plain) or not url else (label + ' (' + url + ')' if label else url))
        return end
    texts = [re.sub(r'[ \t]+', ' ', _tex(a, depth + 1)).strip() for a in args]
    if name in TEX_SECTIONS:
        out.append('\n\n' + ' '.join(texts) + '\n')
    elif name in TEX_LAST_ARG:
        out.append(texts[-1])
    elif 'item' in name.lower() and name not in TEX_ONE_ARG:
        out.append('\n- ' + ' | '.join(t for t in texts if t))
    else:
        out.append(' | '.join(t for t in texts if t))
    return end


def _tex(s, depth=0):
    if depth > 40:
        return ''
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c == '%':
            j = s.find('\n', i)
            i = n if j < 0 else j + 1
        elif c == '\\':
            i = _tex_command(s, i, out, depth)
        elif c in '{}$':
            i += 1
        elif c == '&':
            out.append(' | ')
            i += 1
        elif c == '~':
            out.append(' ')
            i += 1
        elif s.startswith('---', i):
            out.append('—')
            i += 3
        elif s.startswith('--', i):
            out.append('–')
            i += 2
        elif s.startswith('``', i) or s.startswith("''", i):
            out.append('"')
            i += 2
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def _uncommented(pattern, source, start=0):
    """First match of `pattern` that is not inside a % comment."""
    for m in re.compile(pattern).finditer(source, start):
        line = source[source.rfind('\n', 0, m.start()) + 1:m.start()]
        if not re.search(r'(?<!\\)(?:\\\\)*%', line):
            return m
    return None


def tex_text(source):
    """Readable text from a LaTeX CV: comments and preamble dropped, common macros unwrapped.

    Nothing is compiled or expanded and \\input/\\include are not followed.
    """
    body = source
    begin = _uncommented(r'\\begin\s*\{document\}', source)
    if begin:
        end = _uncommented(r'\\end\s*\{document\}', source, begin.end())
        body = source[begin.end():end.start()] if end else source[begin.end():]
    lines = []
    for line in _tex(body).split('\n'):
        line = re.sub(r'[ \t]+', ' ', line).strip()
        line = re.sub(r'(?:\s*\|\s*){2,}', ' | ', line)
        lines.append(re.sub(r'^\|\s*|\s*\|$', '', line).strip())
    kept = [line for i, line in enumerate(lines)
            if line or not (i and lines[i - 1].startswith('- ') and i + 1 < len(lines) and lines[i + 1].startswith('- '))]
    return tidy('\n'.join(kept))


# ------------------------------------------------------------------ DOCX
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
MC_FALLBACK = '{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback'
DOCX_SKIP = {W + t for t in ('pPr', 'rPr', 'sectPr', 'tblPr', 'trPr', 'tcPr', 'tblGrid', 'instrText', 'delText',
                             'del', 'moveFrom', 'sdtPr', 'footnoteReference', 'endnoteReference',
                             'commentReference')} | {MC_FALLBACK}


def parse_xml(data):
    """Parse XML with expat while refusing DTDs and entity declarations (no entity expansion)."""
    builder = ET.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator='}')

    def refuse(*_):
        raise ValueError('XML with a DTD or entity declarations is refused')

    def name(raw):
        return '{' + raw if '}' in raw else raw

    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.UnparsedEntityDeclHandler = refuse
    parser.StartElementHandler = lambda tag, attrs: builder.start(name(tag), {name(k): v for k, v in attrs.items()})
    parser.EndElementHandler = lambda tag: builder.end(name(tag))
    parser.CharacterDataHandler = builder.data
    parser.buffer_text = True
    try:
        parser.Parse(data, True)
    except expat.ExpatError as error:
        raise ValueError('Malformed XML: %s' % error)
    return builder.close()


def _docx_node(node, out, depth=0):
    tag = node.tag
    if tag in DOCX_SKIP or depth > 300:
        return
    if tag == W + 't':
        out.append(node.text or '')
    elif tag == W + 'tab':
        out.append('\t')
    elif tag in (W + 'br', W + 'cr'):
        out.append('\n')
    elif tag == W + 'noBreakHyphen':
        out.append('-')
    elif tag == W + 'tbl':
        out.append('\n')
        for row in (r for r in node if r.tag == W + 'tr'):
            cells = []
            for cell in (c for c in row if c.tag == W + 'tc'):
                parts = []
                for child in cell:
                    _docx_node(child, parts, depth + 1)
                cells.append(tidy(''.join(parts)))
            cells = [c for c in cells if c]
            joiner = ' | ' if all('\n' not in c for c in cells) else '\n'
            out.append(joiner.join(cells) + '\n')
    elif tag == W + 'p':
        props = node.find(W + 'pPr')
        if props is not None and props.find(W + 'numPr') is not None:
            out.append('- ')
        for child in node:
            _docx_node(child, out, depth + 1)
        out.append('\n')
    else:
        for child in node:
            _docx_node(child, out, depth + 1)


def _member(archive, info):
    if info.file_size > MAX_MEMBER_BYTES:
        raise ValueError('%s is too large to read safely' % info.filename)
    with archive.open(info) as stream:
        data = stream.read(MAX_MEMBER_BYTES + 1)
    if len(data) > MAX_MEMBER_BYTES:
        raise ValueError('%s is too large to read safely' % info.filename)
    return data


def docx_text(archive):
    """Paragraphs, tables and line breaks of the headers, body and footers (no styles, no macros)."""
    names = archive.namelist()
    parts = []
    ordered = (sorted(n for n in names if re.fullmatch(r'word/header\d*\.xml', n)) + ['word/document.xml'] +
               sorted(n for n in names if re.fullmatch(r'word/footer\d*\.xml', n)))
    for name in ordered:
        out = []
        _docx_node(parse_xml(_member(archive, archive.getinfo(name))), out)
        text = tidy(''.join(out))
        if text:
            parts.append(text)
    return '\n\n'.join(parts)


# ------------------------------------------------------------------ LinkedIn data export
LINKEDIN_FILES = {'profile.csv': 'profile', 'positions.csv': 'positions', 'education.csv': 'education',
                  'skills.csv': 'skills', 'certifications.csv': 'certifications', 'projects.csv': 'projects',
                  'languages.csv': 'languages'}
LINKEDIN_COLUMNS = {
    'profile': ('First Name', 'Last Name', 'Headline', 'Summary', 'Industry', 'Geo Location', 'Websites'),
    'positions': ('Company Name', 'Title', 'Description', 'Location', 'Started On', 'Finished On'),
    'education': ('School Name', 'Start Date', 'End Date', 'Notes', 'Degree Name', 'Activities'),
    'skills': ('Name',),
    'certifications': ('Name', 'Url', 'Authority', 'Started On', 'Finished On'),
    'projects': ('Title', 'Description', 'Url', 'Started On', 'Finished On'),
    'languages': ('Name', 'Proficiency'),
}
MONTHS = {m: i for i, m in enumerate(('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct',
                                      'nov', 'dec'), 1)}


def li_date(value):
    """'Mar 2021', 'March 2021', '2021', '03/2021', '2021-03', '5 Mar 2021' -> 'YYYY-MM' or 'YYYY'."""
    v = re.sub(r'\s+', ' ', (value or '').strip()).rstrip('.')
    if not v:
        return None

    def ym(year, month=None):
        year = int(year)
        if not 1900 <= year <= 2100:
            return None
        if month is None:
            return '%04d' % year
        month = MONTHS.get(str(month)[:3].casefold()) if not str(month).isdigit() else int(month)
        return '%04d-%02d' % (year, month) if month and 1 <= month <= 12 else None

    for pattern, build in (
            (r'(\d{4})', lambda m: ym(m.group(1))),
            (r'([A-Za-z]{3,9})\.? (\d{4})', lambda m: ym(m.group(2), m.group(1))),
            (r'(\d{1,2})[/.-](\d{4})', lambda m: ym(m.group(2), m.group(1))),
            (r'(\d{4})[/.-](\d{1,2})(?:[/.-]\d{1,2})?', lambda m: ym(m.group(1), m.group(2))),
            (r'\d{1,2} ([A-Za-z]{3,9})\.? (\d{4})', lambda m: ym(m.group(2), m.group(1))),
            (r'([A-Za-z]{3,9})\.? \d{1,2},? (\d{4})', lambda m: ym(m.group(2), m.group(1)))):
        m = re.fullmatch(pattern, v)
        if m:
            return build(m)
    return None


def _csv_rows(data, columns, warnings, label):
    text = decode(data, warnings)
    try:
        rows = list(csv.reader(io.StringIO(text, newline='')))
    except csv.Error as error:
        warnings.append('%s could not be read as CSV: %s' % (label, error))
        return []
    wanted = {c.casefold() for c in columns}
    for start, row in enumerate(rows[:10]):
        header = [cell.strip().casefold() for cell in row]
        if wanted & set(header):
            out = []
            for raw in rows[start + 1:]:
                if any(cell.strip() for cell in raw):
                    out.append({header[i]: (raw[i].strip() if i < len(raw) else '') for i in range(len(header))})
            return out
    warnings.append('%s has none of the expected columns' % label)
    return []


def _websites(value):
    out = []
    for m in re.finditer(r'(?:([A-Za-z][A-Za-z ]{0,30}):)?(https?://[^\s,\]\[]+)', value or ''):
        out.append({'label': (m.group(1) or 'Website').strip().title(), 'url': m.group(2)})
    return out


def _period(row, start_key, end_key, current_if_blank=False):
    start_raw, end_raw = row.get(start_key, ''), row.get(end_key, '')
    end = li_date(end_raw)
    if not end_raw and current_if_blank and start_raw:
        end = 'present'
    return {'start': li_date(start_raw), 'end': end, 'start_raw': start_raw, 'end_raw': end_raw}


def parse_linkedin(archive):
    """Structured JSON from a LinkedIn "Get a copy of your data" ZIP, or None if it is not one.

    Only the profile CSVs are read; connections, messages and other people's data are ignored.
    """
    members = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        key = LINKEDIN_FILES.get(info.filename.replace('\\', '/').rsplit('/', 1)[-1].casefold())
        if key and (key not in members or len(info.filename) < len(members[key].filename)):
            members[key] = info
    if not members:
        return None
    warnings = []
    rows = {}
    for key, info in members.items():
        try:
            rows[key] = _csv_rows(_member(archive, info), LINKEDIN_COLUMNS[key], warnings, info.filename.rsplit('/', 1)[-1])
        except ValueError as error:
            warnings.append(str(error))
            rows[key] = []
    first = (rows.get('profile') or [{}])[0]
    profile = {'first_name': first.get('first name', ''), 'last_name': first.get('last name', ''),
               'headline': first.get('headline', ''), 'summary': first.get('summary', ''),
               'industry': first.get('industry', ''), 'location': first.get('geo location', ''),
               'websites': _websites(first.get('websites', ''))}
    positions = [dict({'company': r.get('company name', ''), 'title': r.get('title', ''),
                       'location': r.get('location', ''), 'description': r.get('description', '')},
                      **_period(r, 'started on', 'finished on', current_if_blank=True))
                 for r in rows.get('positions', []) if r.get('company name') or r.get('title')]
    education = [dict({'school': r.get('school name', ''), 'degree': r.get('degree name', ''),
                       'notes': r.get('notes', ''), 'activities': r.get('activities', '')},
                      **_period(r, 'start date', 'end date'))
                 for r in rows.get('education', []) if r.get('school name') or r.get('degree name')]
    certifications = [dict({'name': r.get('name', ''), 'authority': r.get('authority', ''), 'url': r.get('url', '')},
                           **_period(r, 'started on', 'finished on'))
                      for r in rows.get('certifications', []) if r.get('name')]
    projects = [dict({'title': r.get('title', ''), 'description': r.get('description', ''), 'url': r.get('url', '')},
                     **_period(r, 'started on', 'finished on'))
                for r in rows.get('projects', []) if r.get('title')]
    for item in positions + education + certifications + projects:
        for side in ('start', 'end'):
            raw = item[side + '_raw']
            if raw and item[side] is None:
                warnings.append('Could not read the date %r' % raw)
    return {'schema': 'the-jobfather/linkedin-export@1',
            'files': sorted(info.filename.rsplit('/', 1)[-1] for info in members.values()),
            'profile': profile, 'positions': positions, 'education': education,
            'skills': [r.get('name', '') for r in rows.get('skills', []) if r.get('name')],
            'certifications': certifications, 'projects': projects,
            'languages': [{'name': r.get('name', ''), 'proficiency': r.get('proficiency', '')}
                          for r in rows.get('languages', []) if r.get('name')],
            'warnings': warnings}


def _span(item):
    start, end = item.get('start') or item.get('start_raw') or '?', item.get('end') or item.get('end_raw') or ''
    return start + (' to ' + end if end else '')


def linkedin_text(data):
    """A readable summary of the parsed export (what `importer.py text` shows for the ZIP)."""
    p = data['profile']
    lines = ['LinkedIn data export (read: %s)' % ', '.join(data['files']), '']
    name = ' '.join(x for x in (p['first_name'], p['last_name']) if x)
    for label, value in (('Name', name), ('Headline', p['headline']), ('Location', p['location']),
                         ('Industry', p['industry'])):
        if value:
            lines.append('%s: %s' % (label, value))
    for site in p['websites']:
        lines.append('Website (%s): %s' % (site['label'], site['url']))
    if p['summary']:
        lines += ['', 'Summary:', p['summary']]
    sections = (
        ('Positions', data['positions'], lambda x: ', '.join(v for v in (x['title'], x['company'], x['location']) if v),
         'description'),
        ('Education', data['education'], lambda x: ', '.join(v for v in (x['degree'], x['school']) if v), 'notes'),
        ('Certifications', data['certifications'], lambda x: ', '.join(v for v in (x['name'], x['authority']) if v),
         'url'),
        ('Projects', data['projects'], lambda x: x['title'], 'description'))
    for title, items, head, body in sections:
        if items:
            lines += ['', title]
            for item in items:
                lines.append('- %s (%s)' % (head(item), _span(item)))
                for extra in (item.get(body) or '').splitlines():
                    if extra.strip():
                        lines.append('  ' + extra.strip())
    if data['skills']:
        lines += ['', 'Skills: ' + '; '.join(data['skills'])]
    if data['languages']:
        lines += ['', 'Languages: ' + '; '.join(
            l['name'] + (' (%s)' % l['proficiency'] if l['proficiency'] else '') for l in data['languages'])]
    return '\n'.join(lines)


# ------------------------------------------------------------------ draft EXPERIENCE.md entries
NOISE_WORDS = {'inc', 'llc', 'ltd', 'limited', 'pvt', 'private', 'plc', 'gmbh', 'ag', 'sa', 'srl', 'bv', 'corp',
               'corporation', 'co', 'llp', 'lp', 'pte', 'pty', 'the', 'of', 'and', 'for', 'at', 'in', 'a', 'an',
               'de', 'la', 'le', 'du', 'der', 'und'}
PREFIX = re.compile(r'^[A-Z][A-Z0-9]{1,7}$')
MAX_CLAIMS = 15


def suggest_prefix(name, taken=(), avoid_text=''):
    """A 2-8 character evidence prefix from an organisation's initials (LKM for "Lakeshore Metrics").

    Skips reserved prefixes, prefixes already taken, and ones that would turn text such as
    "ISO-9001" in the export into an evidence reference.
    """
    words = re.findall(r'[A-Za-z0-9]+', unicodedata.normalize('NFKD', name or '').encode('ascii', 'ignore').decode())
    core = [w for w in words if w.casefold() not in NOISE_WORDS] or words or ['Entry']
    first = core[0].upper()
    consonants = [c for c in first[1:] if c.isalpha() and c not in 'AEIOU']
    initials = ''.join(w[0] for w in core).upper()
    if len(core) == 1:
        candidates = [first[0] + ''.join(consonants[:2]), first[:3], first[:4]]
    elif len(core) == 2:
        candidates = [initials[0] + (consonants[0] if consonants else first[1:2]) + initials[1], initials]
    else:
        candidates = [initials[:4], initials[:3]]
    candidates += [first[:3], first[:4], first[:5]]
    taken = {t.upper() for t in taken}

    def usable(prefix):
        return (PREFIX.match(prefix) and prefix not in evidence.RESERVED and prefix not in taken and not
                re.search(r'(?<![A-Za-z0-9-])%s-H?\d{2,3}(?![A-Za-z0-9])' % re.escape(prefix), avoid_text))

    cleaned = []
    for candidate in candidates:
        candidate = re.sub(r'[^A-Z0-9]', '', candidate)
        if candidate and candidate[0].isdigit():
            candidate = 'X' + candidate
        cleaned.append(candidate[:8])
    for candidate in cleaned:
        if len(candidate) >= 2 and usable(candidate):
            return candidate
    base = next((c for c in cleaned if len(c) >= 2), 'XX')[:6]
    for number in range(2, 1000):
        if usable(base + str(number)):
            return base + str(number)
    raise ValueError('No free evidence prefix for %r' % name)


def _one_line(value):
    return re.sub(r'\s+', ' ', value or '').strip()


def _entry_value(value):
    return _one_line(value).replace(' # ', ' #')  # " # " starts a comment in jobfather-entry blocks


def _cell(value):
    return _one_line(value).replace('|', '/').replace('<!--', '<! --').replace('-->', '- ->')


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _comment(value):
    return _one_line(value).replace('--', '- -')


def claim_lines(text):
    """Claim candidates copied from the export text: one per line or bullet, long lines split into sentences."""
    out = []
    for raw in re.split(r'\n+|\s[•▪◦·]\s', text or ''):
        line = re.sub(r'^\s*(?:[-*•▪◦·–—]|\d{1,2}[.)])\s*', '', raw).strip()
        pieces = re.split(r'(?<=[.!?])\s+(?=[A-Z])', line) if len(line) > 300 else [line]
        out += [_one_line(p) for p in pieces if len(_one_line(p)) >= 3]
    return out


def draft_entries(linkedin, existing_prefixes=(), source_id=None):
    """Propose EXPERIENCE.md sections from parsed LinkedIn data, for the host to review with the user.

    Entries default to `cv: locked`; claims are copied from the export text only, with their scope
    left for the user to confirm. Nothing is written anywhere.
    """
    taken = set(existing_prefixes)
    blob = '\n'.join(_strings(linkedin))
    blocks, entries, questions = [], [], []
    seen_orgs = {}

    def add(kind, org, title, location, period, claims, origin, scope_note):
        prefix = suggest_prefix(org, taken, blob)
        taken.add(prefix)
        lines = ['## [%s] %s' % (prefix, _one_line(org)), '',
                 '<!-- Draft from the LinkedIn export (%s). Confirm every value with the user. -->' % _comment(origin),
                 '', '```jobfather-entry', 'kind: ' + kind, 'org: ' + _entry_value(org)]
        if title:
            lines.append('title: ' + _entry_value(title))
        if location:
            lines.append('location: ' + _entry_value(location))
        for side in ('start', 'end'):
            if period.get(side):
                lines.append('%s: %s' % (side, period[side]))
        lines += ['cv: locked', '```', '']
        kept = claims[:MAX_CLAIMS]
        if kept:
            lines += ['### Claims', '', '| ID | Claim | Scope / metric definition |', '| --- | --- | --- |']
            lines += ['| %s-%02d | %s | %s |' % (prefix, i, _cell(text), scope_note) for i, text in enumerate(kept, 1)]
            lines.append('')
        if len(claims) > len(kept):
            lines += ['<!-- %d more lines in the export were not drafted as claims. -->' % (len(claims) - len(kept)), '']
        blocks.append('\n'.join(lines))
        entries.append({'prefix': prefix, 'kind': kind, 'org': _one_line(org), 'title': _one_line(title) or None,
                        'start': period.get('start'), 'end': period.get('end'), 'claims': len(kept), 'source': origin})
        return prefix

    def check_dates(label, item):
        for side in ('start', 'end'):
            if item.get(side + '_raw') and not item.get(side):
                questions.append('%s: the %s date %r could not be read; ask for month and year.'
                                 % (label, side, item[side + '_raw']))

    unconfirmed = 'From the LinkedIn export; scope not stated, confirm with the user.'
    for number, item in enumerate(linkedin.get('positions', []), 1):
        org = item.get('company') or item.get('title') or 'Position %d' % number
        title = item.get('title') or '<<ask: job title>>'
        if not item.get('title'):
            questions.append('%s: the export has no job title; ask for the exact title.' % org)
        claims = claim_lines(item.get('description'))
        if not claims:
            questions.append('%s: no description in the export; ask for 2-4 achievements with numbers and how '
                             'each was measured.' % org)
        check_dates(org, item)
        seen_orgs.setdefault(_one_line(org).casefold(), []).append(number)
        add('role', org, title, item.get('location'), item, claims, 'Positions.csv row %d' % number, unconfirmed)
    for org_key, rows in seen_orgs.items():
        if len(rows) > 1:
            questions.append('Rows %s of Positions.csv are the same employer; one section per title was drafted. '
                             'Ask whether to keep them separate or merge them.' % ', '.join(map(str, rows)))
    for number, item in enumerate(linkedin.get('education', []), 1):
        school = item.get('school') or item.get('degree') or 'School %d' % number
        claims = ([item['degree']] if item.get('degree') else []) + claim_lines(item.get('notes')) + \
            claim_lines(item.get('activities'))
        if not item.get('degree'):
            questions.append('%s: the export has no degree title; ask for the exact degree name.' % school)
        check_dates(school, item)
        add('education', school, item.get('degree') or '<<ask: degree title>>', None, item, claims,
            'Education.csv row %d' % number, 'From the LinkedIn export; confirm the degree title and year.')
    for number, item in enumerate(linkedin.get('certifications', []), 1):
        check_dates(item['name'], item)
        add('certification', item.get('authority') or item['name'], item['name'], None,
            {'start': item.get('start')}, [item['name']], 'Certifications.csv row %d' % number,
            _cell('Issuer in the export: %s%s.' % (item.get('authority') or 'not stated',
                                                   '; expiry: %s' % item['end_raw'] if item.get('end_raw') else '')))
    for number, item in enumerate(linkedin.get('projects', []), 1):
        check_dates(item['title'], item)
        add('project', item['title'], None, None, item, claim_lines(item.get('description')),
            'Projects.csv row %d' % number, unconfirmed)
    notes = []
    if linkedin.get('skills'):
        skills = '; '.join(_comment(s) for s in linkedin['skills'])
        blocks.append('<!-- LinkedIn skills, to sort with the user into "CV skills" and "Tools and platforms"\n'
                      '     (tools only if they really used them): %s -->\n' % skills)
        notes.append('Sort the %d LinkedIn skills into CV skills and tools with the user.' % len(linkedin['skills']))
    profile = linkedin.get('profile') or {}
    if profile.get('headline') or profile.get('summary'):
        notes.append('The LinkedIn headline and summary can seed PROFILE.md and the CV summary; the CV summary '
                     'must end with the claim IDs that support it.')
    if entries:
        questions.append('For every number in a claim, ask what was measured, over what period and how much of '
                         'the credit is theirs; put that in the scope column.')
    header = ('<!-- Draft EXPERIENCE.md sections from a LinkedIn data export%s. Nothing has been saved: review each '
              'section with the user, add CV bullets for roles that should be tailored, then write the file. -->\n'
              % (' (%s)' % source_id if source_id else ''))
    markdown = header + '\n' + '\n'.join(blocks)
    try:
        evidence.parse('# Draft\n\n' + markdown)
        check = 'ok'
    except ValueError as error:
        check = str(error)
    return {'markdown': markdown, 'entries': entries, 'questions': questions, 'notes': notes, 'parse_check': check}


# ------------------------------------------------------------------ extraction
def extract(name, data, path=None):
    """{text, linkedin, extraction{method, ok, warnings, chars}} for one source; never runs its content."""
    ext = Path(name).suffix.casefold()
    warnings = []
    if data[:5] == b'%PDF-' or ext == '.pdf':
        return _pdf(path, warnings)
    if data[:4] == b'PK\x03\x04' or ext in ('.docx', '.zip'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
                if 'word/document.xml' in names:
                    return _result(docx_text(archive), 'docx-xml', True, warnings)
                linkedin = parse_linkedin(archive)
                if linkedin is not None:
                    warnings += linkedin['warnings']
                    return _result(linkedin_text(linkedin), 'linkedin-export', True, warnings, linkedin)
        except (zipfile.BadZipFile, ValueError, KeyError, RecursionError) as error:
            return _result(None, 'zip', False, ['Could not read the archive: %s' % error])
        advice = UNSUPPORTED.get(ext, 'This archive is neither a DOCX nor a LinkedIn data export; read the files '
                                      'you need yourself.')
        return _result(None, 'zip', False, [advice])
    if ext in UNSUPPORTED:
        return _result(None, 'none', False, [UNSUPPORTED[ext]])
    if ext in IMAGES:
        return _result(None, 'none', False, ['Images are not converted; read the image yourself.'])
    if b'\x00' in data[:8192]:
        return _result(None, 'none', False, ['Binary file of an unknown type; read the original yourself.'])
    text = decode(data, warnings)
    if ext in ('.tex', '.ltx'):
        if re.search(r'\\(?:input|include)\s*\{', text):
            warnings.append('\\input/\\include files were not followed; import them separately if needed.')
        return _result(tex_text(text), 'tex-strip', True, warnings)
    if ext in ('.html', '.htm', '.xhtml') or (ext not in ('.md', '.markdown', '.txt') and looks_html(text)):
        return _result(html_text(text), 'html-strip', True, warnings)
    return _result(text, 'plain', True, warnings)


def _pdf(path, warnings):
    import pdf_tools
    exe = pdf_tools.resolve().get('pdftotext')
    if not exe:
        warnings.append('pdftotext (Poppler) is not installed, so no text was extracted. Read the PDF yourself, '
                        'or install Poppler (doctor.py) and import the file again.')
        return _result(None, 'pdftotext', False, warnings)
    try:
        proc = pdf_tools.run([exe, '-layout', '-enc', 'UTF-8', str(path), '-'], timeout=60)
    except (RuntimeError, OSError) as error:
        warnings.append('pdftotext did not finish: %s; read the PDF yourself.' % error)
        return _result(None, 'pdftotext', False, warnings)
    if proc.returncode != 0:
        detail = (proc.stderr or '').strip().splitlines()
        warnings.append('pdftotext failed (%s); read the PDF yourself.' % (detail[0][:200] if detail else proc.returncode))
        return _result(None, 'pdftotext', False, warnings)
    if not proc.stdout.strip():
        warnings.append('The PDF has no text layer (a scan?); read it yourself.')
        return _result(None, 'pdftotext', False, warnings)
    return _result(proc.stdout, 'pdftotext', True, warnings)


# ------------------------------------------------------------------ store
def _load_index(ws):
    path = contained(ws, INDEX)
    return read_json(path) if path.is_file() else {'schema_version': 1, 'sources': []}


def _read_source(path):
    """Bytes of a regular file up to MAX_BYTES; links, folders, devices and pipes are refused."""
    try:
        info = os.lstat(str(path))
    except OSError:
        raise ValueError('File not found')
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Not a regular file (folders, links, devices and pipes are refused)')
    if info.st_size > MAX_BYTES:
        raise ValueError('Larger than 20 MB; import a smaller export or the individual files')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
    fd = os.open(str(path), flags)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Not a regular file')
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('Larger than 20 MB; import a smaller export or the individual files')
    return data


def _extract_into(ws, entry, data):
    stored = contained(ws, entry['stored_path'])
    found = extract(entry['original_name'], data, stored)
    entry['extraction'] = found['extraction']
    entry['text_path'] = None
    if found['text']:
        entry['text_path'] = layout.SOURCES + '/' + entry['id'] + '.txt'
        atomic_text(contained(ws, entry['text_path']), found['text'] + '\n')
    if found['linkedin'] is not None:
        entry['linkedin_path'] = layout.SOURCES + '/' + entry['id'] + '.linkedin.json'
        atomic_json(contained(ws, entry['linkedin_path']), found['linkedin'])
    return found


def _private(path):
    if os.name != 'nt':
        os.chmod(str(path), 0o600)


def _add_one(ws, index, path, kind, note):
    data = _read_source(path)
    sha = hashlib.sha256(data).hexdigest()
    entry = next((e for e in index['sources'] if e['sha256'] == sha), None)
    if entry is not None:
        stored = contained(ws, entry['stored_path'])
        if not stored.is_file() or digest(stored) != sha:
            atomic_bytes(stored, data)  # the copy went missing or changed; restore it from the same bytes
            _private(stored)
        result = dict(entry, status='already-imported')
        if not entry['extraction']['ok']:
            _extract_into(ws, entry, data)  # e.g. pdftotext installed since the first import
            result = dict(entry, status='re-extracted')
        if kind and kind != entry['kind']:
            result['note_kind'] = 'Already imported as %s; kind left unchanged.' % entry['kind']
        return result
    entry = {'id': 'SRC-' + sha[:16], 'original_name': Path(path).name, 'kind': kind or 'other', 'sha256': sha,
             'bytes': len(data), 'added_at': now(), 'stored_path': layout.SOURCES + '/' + sha[:16] + '-' +
             safe_name(Path(path).name), 'text_path': None, 'note': (note or None)}
    stored = contained(ws, entry['stored_path'])
    atomic_bytes(stored, data)
    _private(stored)
    found = _extract_into(ws, entry, data)
    if found['linkedin'] is not None and kind is None:
        entry['kind'] = 'linkedin-export'
    elif kind == 'linkedin-export' and found['linkedin'] is None:
        entry['extraction']['warnings'].append('No LinkedIn profile CSVs (Profile.csv, Positions.csv, ...) found.')
    index['sources'].append(entry)
    return dict(entry, status='added')


def add(paths, kind=None, note=None, ws=None):
    """Copy each file into profile/sources (idempotent by content) and extract its text."""
    ws = require_workspace(ws)
    if kind is not None and kind not in KINDS:
        raise ValueError('Kind must be one of: ' + ', '.join(KINDS))
    if note is not None and (not isinstance(note, str) or len(note) > 500):
        raise ValueError('Keep the note under 500 characters')
    results = []
    with shared_lock(contained(ws, LOCK)):
        index = _load_index(ws)
        for raw in paths:
            try:
                results.append(_add_one(ws, index, Path(raw), kind, note))
            except (ValueError, OSError) as error:
                results.append({'file': Path(str(raw)).name, 'status': 'refused', 'reason': str(error)})
                continue
            atomic_json(contained(ws, INDEX), index)
    return results


def sources(ws=None):
    ws = require_workspace(ws)
    return _load_index(ws)['sources']


def find(source_id, ws=None):
    """Entry by full id, or by a unique prefix of at least six characters."""
    ws = require_workspace(ws)
    key = source_id.strip()
    entries = _load_index(ws)['sources']
    exact = [e for e in entries if e['id'] == key]
    if exact:
        return exact[0]
    bare = key[4:] if key.upper().startswith('SRC-') else key
    matches = [e for e in entries if len(bare) >= 6 and e['id'][4:].startswith(bare.casefold())]
    if len(matches) != 1:
        raise ValueError('No single imported source matches %r; run importer.py list' % source_id)
    return matches[0]


def text(source_id, ws=None):
    ws = require_workspace(ws)
    entry = find(source_id, ws)
    if not entry.get('text_path'):
        raise ValueError('No extracted text for %s (%s). Read the original at %s'
                         % (entry['id'], '; '.join(entry['extraction']['warnings']) or 'nothing extracted',
                            contained(ws, entry['stored_path'])))
    return read_text(contained(ws, entry['text_path']))


def linkedin(source_id, ws=None):
    """Parsed export plus draft EXPERIENCE.md entries (prefixes avoid the ones already in use)."""
    ws = require_workspace(ws)
    entry = find(source_id, ws)
    if not entry.get('linkedin_path'):
        raise ValueError('%s is not a LinkedIn data export' % entry['id'])
    data = read_json(contained(ws, entry['linkedin_path']))
    return {'source_id': entry['id'], 'export': data,
            'draft': draft_entries(data, existing_prefixes(ws), entry['id'])}


def existing_prefixes(ws):
    path = Path(ws) / layout.EXPERIENCE
    if not path.is_file():
        return []
    return [m.group(1) for m in (evidence.DECL.match(line) for line in read_text(path).splitlines()) if m]


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    sub = parser.add_subparsers(dest='command', required=True)
    child = sub.add_parser('add', help='copy files into profile/sources and extract their text')
    child.add_argument('files', nargs='+')
    child.add_argument('--kind', choices=KINDS, help='what the file is (default: detected, else other)')
    child.add_argument('--note', help='a short note, e.g. "CV from 2024"')
    sub.add_parser('list', help='imported sources and how their text was extracted')
    sub.add_parser('text', help='print the extracted text of one source').add_argument('source_id')
    child = sub.add_parser('linkedin', help='structured LinkedIn export plus draft EXPERIENCE.md entries')
    child.add_argument('source_id')
    child.add_argument('--markdown', action='store_true', help='print only the draft EXPERIENCE.md sections')
    args = parser.parse_args()
    if args.command == 'add':
        result = add(args.files, args.kind, args.note, args.workspace)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if any(r['status'] == 'refused' for r in result) else 0
    if args.command == 'list':
        print(json.dumps(sources(args.workspace), ensure_ascii=False, indent=2))
    elif args.command == 'text':
        sys.stdout.write(text(args.source_id, args.workspace))
    else:
        result = linkedin(args.source_id, args.workspace)
        if args.markdown:
            sys.stdout.write(result['draft']['markdown'])
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit('Import failed: ' + str(error))
