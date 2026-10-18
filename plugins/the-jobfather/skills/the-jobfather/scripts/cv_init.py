"""Render the user's master CV from identity.json + EXPERIENCE.md, check it fits one page, adopt it.

    cv_init.py render              -> .state/renders/<id>/{master-cv.tex, cv-manifest.json, render.json}
    cv_init.py check <id>          -> compiles the baseline and reports page fit
    cv_init.py adopt <id> --visual-review R.json
        before setup is registered: installs cv/master-cv.tex, cv/cv-manifest.json, cv/baseline.pdf
        afterwards: writes a memory proposal (whole-file write operations) for the user to approve

The renderer is deterministic: the same inputs and template always give the same bytes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import uuid

if __name__ == '__main__':
    sys.modules['cv_init'] = sys.modules[__name__]

import evidence
import layout
from cv_template import escape, markdown_bold, render as render_tex, sha, unsupported_chars
from local import (CODE_ROOT, add_workspace_arg, atomic_json, atomic_text, contained, digest, dumps, now, read_json,
                   read_text, require_workspace)

TEMPLATES = CODE_ROOT / 'templates' / 'cv'
DEFAULT_TEMPLATE = 'classic@1'
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
LETTER_COUNTRIES = ('US', 'CA', 'MX', 'PH', 'CL', 'CO', 'VE')
FILENAME_PATTERN = '{prefix}_{company}_{role}_{application}_v{version:03d}_{run}.pdf'


def template_dir(name):
    path = TEMPLATES / name
    if not (path / 'template.json').is_file():
        raise ValueError('Unknown CV template: ' + name)
    return path


def template_hash(path):
    digest_ = hashlib.sha256()
    for item in sorted(p for p in path.iterdir() if p.is_file()):
        digest_.update(item.name.encode() + b'\0' + item.read_bytes() + b'\0')
    return digest_.hexdigest()


def fmt_date(value, present):
    if not value:
        return ''
    if value == 'present':
        return present
    if len(value) == 7:
        return '%s %s' % (MONTHS[int(value[5:7]) - 1], value[:4])
    return value


def fmt_range(entry, present):
    start, end = fmt_date(entry.get('start'), present), fmt_date(entry.get('end'), present)
    return start + ' – ' + end if start and end else start or end


def _text(text, allow_bold=True):
    bad = unsupported_chars(text)
    if bad:
        raise ValueError('The classic CV template cannot typeset: %s (in %r)' % (' '.join(bad), text[:50]))
    if allow_bold:
        plain, spans = markdown_bold(text)
        return render_tex(plain, spans)
    return escape(text)


def _url(url):
    if not url.startswith(('https://', 'http://', 'mailto:')) or any(c in url for c in ' %{}\\#'):
        raise ValueError('Links on the CV must be simple http(s) URLs: ' + url)
    return url


def _display(url):
    shown = url.split('://', 1)[-1].rstrip('/')
    return shown[4:] if shown.startswith('www.') and shown.count('/') else shown


def contacts(identity):
    data = identity.get('contacts') or {}
    links = {l.get('label', ''): l.get('url', '') for l in data.get('links', [])}
    out = []
    for key in identity.get('cv_contacts') or ['phone', 'email', 'location']:
        if key == 'email' and data.get('email'):
            out.append(r'\href{%s}{%s}' % (_url('mailto:' + data['email']), _text(data['email'], False)))
        elif key in ('phone', 'location') and data.get(key):
            out.append(r'\jfText{%s}' % _text(data[key], False))
        elif key.startswith('link:') and links.get(key[5:]):
            url = _url(links[key[5:]])
            out.append(r'\href{%s}{%s}' % (url, _text(_display(url), False)))
    return out


def _bullets(section):
    return [_text(b['text']) for b in section['bullets']]


def build(identity, cat, options, tpl):
    """Return (source, manifest) for the given inputs. Pure and deterministic."""
    name = identity.get('full_name', '').strip()
    if not name:
        raise ValueError('identity.json needs full_name')
    locale = identity.get('locale') or {}
    present = locale.get('present_label') or 'Present'
    sections_cfg = tpl['sections']
    lines = [r'\begin{document}', '', '%----------HEADER----------', r'{\centering',
             r'  \jfName{%s}\par' % _text(name, False), r'  \vspace{1.5pt}', r'  {\fontsize{10.25}{12}\selectfont']
    parts = contacts(identity)
    for i, part in enumerate(parts):
        if i:
            lines.append(r'    \jfSep')
        lines.append('    ' + part)
    lines += [r'  }\par', '}', '']
    manifest_sections, entries, slots = [], [], {}
    if not cat.cv_summary:
        raise ValueError('Add a "## CV summary" paragraph to EXPERIENCE.md')
    lines += ['%-----------SUMMARY-----------', r'\section{%s}' % sections_cfg['summary'],
              r'{\fontsize{10.55}{13.05}\selectfont', '% @jf:slot summary kind=paragraph', _text(cat.cv_summary),
              '% @jf:end summary', r'\par', '}', '']
    manifest_sections.append({'id': 'summary', 'title': sections_cfg['summary']})
    slots['summary'] = {'kind': 'paragraph', 'emphasis': False, 'baseline_chars': len(markdown_bold(cat.cv_summary)[0])}

    def entry_block(prefix, entry, section_id):
        sec = cat.sections[prefix]
        bullets = sec['bullets']
        editable = entry['cv'] == 'tailor'
        if editable and not bullets:
            raise ValueError('[%s] is marked cv: tailor but has no "### CV bullets"' % prefix)
        block = ['% @jf:entry ' + prefix]
        if entry['kind'] == 'group':
            block.append(r'\resumeGroup{%s}{%s}' % (_text(entry['org'], False), _text(fmt_range(entry, present), False)))
        else:
            block += [r'\resumeEntry', r'  {%s}{%s}' % (_text(entry['org'], False), _text(fmt_range(entry, present), False)),
                      r'  {%s}{%s}' % (_text(entry.get('title', ''), False), _text(entry.get('location', ''), False))]
        slot = prefix.lower()
        if bullets:
            block.append(r'\begin{resumeBullets}')
            if editable:
                block.append('%% @jf:slot %s kind=bullets count=%d' % (slot, len(bullets)))
            block += ['  \\item ' + b for b in _bullets(sec)]
            if editable:
                block.append('% @jf:end ' + slot)
            block.append(r'\end{resumeBullets}')
        block.append('% @jf:end ' + prefix)
        entries.append({'id': prefix, 'section': section_id, 'kind': entry['kind'], 'label': entry['org'],
                        'editable': editable, 'bullets': len(bullets),
                        'tailoring_weight': entry.get('cv_weight', 'locked') if editable else 'locked',
                        'slot': slot if editable else None})
        if editable:
            slots[slot] = {'kind': 'bullets', 'entry': prefix, 'count': len(bullets), 'emphasis': True}
        return block

    groups = (('experience', ('role', 'group'), r'\experienceGap'), ('projects', ('project',), r'\experienceGap'),
              ('education', ('education',), r'\educationGap'), ('certifications', ('certification',), r'\educationGap'))
    for section_id, kinds, gap in groups:
        chosen = [(p, cat.sections[p]['entry']) for p in cat.prefixes
                  if cat.sections[p]['entry']['kind'] in kinds and cat.sections[p]['entry']['cv'] != 'omit']
        if not chosen:
            if section_id in ('experience', 'education'):
                raise ValueError('EXPERIENCE.md needs at least one %s entry for the CV' % section_id)
            continue
        title = sections_cfg[section_id]
        lines += ['%-----------' + section_id.upper() + '-----------', r'\section{%s}' % title, '']
        manifest_sections.append({'id': section_id, 'title': title})
        for i, (prefix, entry) in enumerate(chosen):
            if i:
                lines.append(gap)
            lines += entry_block(prefix, entry, section_id)
            lines.append('')
    lists = cat.cv_lists
    if len(lists.get('core_skills', [])) < 3 or len(lists.get('tools', [])) < 3:
        raise ValueError('Add a "## CV skills" block with at least 3 core_skills and 3 tools to EXPERIENCE.md')
    labels = tpl['labels']
    lines += ['%-----------SKILLS-----------', r'\section{%s}' % sections_cfg['skills'],
              r'{\fontsize{10.4}{12.85}\selectfont', r'\jfLabel{%s}' % _text(labels['core_skills'], False),
              '% @jf:slot core_skills kind=list sep=", "', ', '.join(_text(x, False) for x in lists['core_skills']),
              '% @jf:end core_skills', r'\par', r'\vspace{1.8pt}', r'\jfLabel{%s}' % _text(labels['tools'], False),
              '% @jf:slot tools kind=list sep=", "', ', '.join(_text(x, False) for x in lists['tools']),
              '% @jf:end tools', r'\par', '}', '', r'\end{document}', '']
    manifest_sections.append({'id': 'skills', 'title': sections_cfg['skills']})
    for ident, policy in tpl['list_slots'].items():
        slots[ident] = dict(policy, kind='list', sep=', ', emphasis=False)
    paper = options['paper']
    preamble = read_text(template_dir(options['template']) / 'master-cv.tex')
    source = (preamble.replace('<<paper>>', tpl['paper'][paper]['latex']).replace('<<babel>>', options['babel'])
              .replace('<<body>>', '\n'.join(lines)))
    manifest = {'schema_version': 1,
                'template': {'id': tpl['id'], 'version': tpl['version'], 'name': options['template'],
                             'sha256': template_hash(template_dir(options['template']))},
                'master': {'path': layout.CV_MASTER, 'sha256': sha(source)}, 'engine': tpl['engine'],
                'page': {'size': paper, 'width_pt': tpl['paper'][paper]['width_pt'],
                         'height_pt': tpl['paper'][paper]['height_pt'], 'max_pages': 1, 'tolerance_pt': 0.5},
                'font': dict(tpl['font'], require_embedded=True), 'language': {'babel': options['babel']},
                'sections': manifest_sections, 'entries': entries, 'slots': slots,
                'filename_pattern': FILENAME_PATTERN,
                'file_prefix': identity.get('file_prefix') or name.replace(' ', '_'),
                'render_options': options}
    return source, manifest


def options_for(identity, template=DEFAULT_TEMPLATE, paper=None, babel=None):
    tpl = read_json(template_dir(template) / 'template.json')
    locale = identity.get('locale') or {}
    paper = paper or locale.get('paper') or ('Letter' if (locale.get('country') or '').upper() in LETTER_COUNTRIES else 'A4')
    babel = babel or locale.get('babel') or 'english'
    if paper not in tpl['paper']:
        raise ValueError('Paper must be one of: ' + ', '.join(tpl['paper']))
    if babel not in tpl['babel']:
        raise ValueError('Language must be one of: ' + ', '.join(tpl['babel']))
    return {'template': template, 'paper': paper, 'babel': babel}, tpl


def render_texts(identity_text, experience_text, options=None):
    identity = json.loads(identity_text)
    cat = evidence.parse(experience_text)
    if options is None:
        options, tpl = options_for(identity)
    else:
        tpl = read_json(template_dir(options['template']) / 'template.json')
    return build(identity, cat, options, tpl)


def render(ws, template=DEFAULT_TEMPLATE, paper=None, babel=None):
    ws = require_workspace(ws)
    identity_text = read_text(ws / layout.IDENTITY)
    experience_text = read_text(ws / layout.EXPERIENCE)
    options, _ = options_for(json.loads(identity_text), template, paper, babel)
    source, manifest = render_texts(identity_text, experience_text, options)
    render_id = 'R-' + uuid.uuid4().hex[:12]
    directory = contained(ws, layout.RENDERS + '/' + render_id)
    atomic_text(directory / 'master-cv.tex', source)
    atomic_text(directory / 'cv-manifest.json', dumps(manifest))
    atomic_json(directory / 'render.json', {'render_id': render_id, 'generated_at': now(), 'options': options,
                                            'inputs': {layout.IDENTITY: sha(identity_text),
                                                       layout.EXPERIENCE: sha(experience_text)}})
    return {'render_id': render_id, 'directory': directory.relative_to(ws).as_posix(),
            'entries': [(e['id'], e['label'], 'editable' if e['editable'] else 'locked') for e in manifest['entries']],
            'slots': {k: v.get('count', v['kind']) for k, v in manifest['slots'].items()}, 'page': manifest['page']['size']}


def check(ws, render_id):
    import cv_pdf
    ws = require_workspace(ws)
    directory = contained(ws, layout.RENDERS + '/' + render_id)
    source = read_text(directory / 'master-cv.tex')
    manifest = read_json(directory / 'cv-manifest.json')
    build_dir = directory / 'build'
    if build_dir.exists():
        shutil.rmtree(build_dir)
    try:
        checked = cv_pdf.compile_source(source, manifest, build_dir)
    except RuntimeError as error:
        checked = {'mechanical_ok': False, 'errors': [str(error)], 'pages': None, 'renders': []}
    cat = evidence.parse(read_text(ws / layout.EXPERIENCE))
    bullets = [(len(markdown_bold(b['text'])[0]), prefix, markdown_bold(b['text'])[0][:70])
               for prefix, sec in cat.sections.items() if sec['entry']['cv'] != 'omit' for b in sec['bullets']]
    report = {'render_id': render_id, 'pages': checked.get('pages'), 'mechanical_ok': checked['mechanical_ok'],
              'errors': checked['errors'], 'text_bottom_pt': checked.get('text_bottom_pt'),
              'overfull': checked.get('warnings', {}).get('overfull', []),
              'renders': [Path(p).resolve().relative_to(ws).as_posix() for p in checked.get('renders', [])],
              'longest_bullets': [{'chars': n, 'entry': p, 'start': t} for n, p, t in sorted(bullets, reverse=True)[:5]]}
    if checked.get('pages') and checked['pages'] > 1:
        report['advice'] = ('Shorten CV bullets in EXPERIENCE.md, set an older role to "cv: omit" or make it a '
                            '"kind: group" entry, then render again. The design itself never changes to fit.')
    atomic_json(directory / 'check.json', dict(checked, report=report))
    return report


def adopt(ws, render_id, visual_review, host='unknown'):
    import memory
    import managed
    ws = require_workspace(ws)
    directory = contained(ws, layout.RENDERS + '/' + render_id)
    checked = read_json(directory / 'check.json') if (directory / 'check.json').exists() else None
    if not checked or not checked['mechanical_ok'] or checked.get('pages') != 1:
        raise ValueError('Run check first; only a one-page, mechanically passing render can be adopted')
    renders = {Path(p).resolve().relative_to(ws).as_posix(): digest(p) for p in checked['renders']}
    if not visual_review or visual_review.get('passed') is not True or not visual_review.get('notes') \
            or visual_review.get('render_sha256') != renders:
        raise ValueError('Look at the rendered page and record passed=true, notes and render_sha256 for it')
    source_path, manifest_path = directory / 'master-cv.tex', directory / 'cv-manifest.json'
    pdf = directory / 'build' / 'cv.pdf'
    if digest(pdf) != checked['pdf_sha256']:
        raise ValueError('The checked PDF changed; run check again')
    master, man = ws / layout.CV_MASTER, ws / layout.CV_MANIFEST
    already = master.is_file() and digest(master) == digest(source_path) and man.is_file() and digest(man) == digest(manifest_path)
    if already or not memory.events(ws):
        if not already:
            atomic_text(master, read_text(source_path))
            atomic_text(man, read_text(manifest_path))
        (ws / layout.CV_DIR).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pdf, ws / layout.CV_BASELINE)
        return {'status': 'adopted', 'master': layout.CV_MASTER, 'baseline_pdf': layout.CV_BASELINE}
    revision = memory.current(ws)
    changes = []
    for target, rendered in ((layout.CV_MASTER, source_path), (layout.CV_MANIFEST, manifest_path)):
        current = ws / target
        changes.append({'path': target, 'before_sha256': digest(current) if current.is_file() else 'missing',
                        'write': {'from_render': rendered.relative_to(ws).as_posix(), 'sha256': digest(rendered)}})
    proposal = {'transaction_id': 'TX-cv-render-' + render_id[2:], 'intent': 'explicit-update',
                'user_request': '<<the user\'s words asking to update the CV master>>', 'platform': host,
                'scope': 'global', 'change_type': 'cv-render', 'expected_revision': revision['revision'],
                'summary': {'before': 'Previous master CV.', 'after': '<<what changed on the CV>>'},
                'reason': 'Master CV re-rendered from the current profile and reviewed visually.',
                'review': {'user_authorized': True, 'facts_checked': True, 'scope_checked': True},
                'changes': changes}
    atomic_json(directory / 'proposal.json', proposal)
    return {'status': 'proposal-ready', 'proposal': (directory / 'proposal.json').relative_to(ws).as_posix(),
            'next': 'Fill the << >> fields, apply it with memory.py apply, then run adopt again to install baseline.pdf.'}


def verify_unchanged(ws, dependent, post_texts):
    """Called by memory.py when a change claims the CV master stays the same."""
    manifest = read_json(Path(ws) / layout.CV_MANIFEST)
    source, new_manifest = render_texts(post_texts[layout.IDENTITY], post_texts[layout.EXPERIENCE],
                                        manifest['render_options'])
    expected = source if dependent == layout.CV_MASTER else dumps(new_manifest)
    if expected != read_text(Path(ws) / dependent):
        raise ValueError('This change alters your master CV; render it again (cv_init.py render) and include the '
                         'write operations with dependency_review "updated"')


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    sub = parser.add_subparsers(dest='command', required=True)
    child = sub.add_parser('render')
    child.add_argument('--template', default=DEFAULT_TEMPLATE)
    child.add_argument('--paper', choices=('A4', 'Letter'))
    child.add_argument('--babel')
    sub.add_parser('check').add_argument('render_id')
    child = sub.add_parser('adopt')
    child.add_argument('render_id')
    child.add_argument('--visual-review', type=Path, required=True)
    child.add_argument('--platform', default='unknown')
    args = parser.parse_args()
    if args.command == 'render':
        result = render(args.workspace, args.template, args.paper, args.babel)
    elif args.command == 'check':
        result = check(args.workspace, args.render_id)
    else:
        result = adopt(args.workspace, args.render_id, read_json(args.visual_review), args.platform)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        raise SystemExit('CV setup failed: ' + str(error))
