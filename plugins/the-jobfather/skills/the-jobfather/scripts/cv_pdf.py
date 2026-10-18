"""Compile, inspect and promote a CV only after mechanical and visual validation."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import unicodedata
import uuid
import xml.etree.ElementTree as ET

if __name__ == '__main__':
    sys.modules['cv_pdf'] = sys.modules[__name__]

import layout
import managed
from applications import Store
from cv_template import assemble, check_source, locked_visible_text, template, value
from local import (add_workspace_arg, atomic_json, atomic_text, contained, context_revision, digest, locked, now,
                   platform, read_json, read_text, require_workspace)
from pdf_tools import require, run


def normalize(text):
    text = unicodedata.normalize('NFKC', text).translate(str.maketrans({
        '’': "'", '‘': "'", '“': '"', '”': '"', '–': '-', '—': '-', '−': '-', '\u00ad': None, '•': ' ', '\u00a0': ' '}))
    text = text.replace('---', '-').replace('--', '-')
    return re.sub(r'\s+([,.;:])', r'\1', ' '.join(text.split()))


def ordered_find(text, needles, start=0):
    positions, cursor = [], start
    for needle in needles:
        index = text.find(normalize(needle), cursor)
        positions.append(index)
        if index >= 0:
            cursor = index + 1
    return positions


def inspect_pdf(pdf, directory, manifest=None, expected=(), require_master=True):
    tools = require()
    pdf, directory = Path(pdf), Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    errors = []
    commands = [('info', [tools['pdfinfo'], '-box', pdf]),
                ('text', [tools['pdftotext'], '-enc', 'UTF-8', '-layout', pdf, '-']),
                ('fonts', [tools['pdffonts'], pdf]),
                ('bbox', [tools['pdftotext'], '-enc', 'UTF-8', '-bbox-layout', pdf, directory / 'layout.xhtml'])]
    outputs = {}
    for name, args in commands:
        result = run(args)
        atomic_text(directory / (name + '.txt'), result.stdout + result.stderr)
        if result.returncode:
            errors.append(name + ' inspection failed: ' + result.stderr.strip())
        outputs[name] = result.stdout
    pages_match = re.search(r'^Pages:\s+(\d+)', outputs['info'], re.M)
    pages = int(pages_match.group(1)) if pages_match else None
    size_match = re.search(r'^Page size:\s+([\d.]+) x ([\d.]+)', outputs['info'], re.M)
    size = [float(x) for x in size_match.groups()] if size_match else None
    text = normalize(outputs['text'])
    if not text:
        errors.append('PDF text extraction is empty')
    fonts = []
    for line in outputs['fonts'].splitlines()[2:]:
        cells = line.split()
        if len(cells) >= 8:
            fonts.append({'name': cells[0], 'embedded': cells[-5] == 'yes', 'unicode': cells[-3] == 'yes'})
    if not fonts or not all(x['embedded'] for x in fonts):
        errors.append('Missing or unembedded PDF fonts')
    width, height = 612.0, 792.0
    if require_master and manifest:
        page = manifest['page']
        width, height = page['width_pt'], page['height_pt']
        if pages is None or pages > page.get('max_pages', 1):
            errors.append('CV must fit on %d page(s); observed %s' % (page.get('max_pages', 1), pages))
        if size is None or any(abs(a - b) > page.get('tolerance_pt', 0.5) for a, b in zip(size, (width, height))):
            errors.append('CV page size must be %s (%.1f x %.1f pt)' % (page['size'], width, height))
        patterns = manifest['font']['pdf_name_patterns']
        if not fonts or any(not any(p in f['name'] for p in patterns) for f in fonts):
            errors.append('PDF fonts differ from the required %s' % manifest['font']['family'])
        titles = [s['title'] for s in manifest['sections']]
        positions = ordered_find(text, titles)
        if any(p < 0 for p in positions):
            errors.append('Section reading order is incorrect')
        experience = next((s for s in manifest['sections'] if s['id'] == 'experience'), None)
        if experience:
            begin = text.find(normalize(experience['title']))
            following = [text.find(normalize(s['title']), begin + 1) for s in manifest['sections']]
            end = min([p for p in following if p > begin] or [len(text)])
            labels = [e['label'] for e in manifest['entries'] if e['section'] == 'experience']
            span = text[:end]
            if any(p < 0 for p in ordered_find(span, labels, begin)):
                errors.append('Employment reading order is incorrect')
    elif size is None:
        errors.append('PDF page size could not be read')
    missing = [item for item in expected if normalize(item) not in text]
    if missing:
        errors.append('Expected content missing from extracted text: ' + '; '.join(missing))
    words, lines = [], []
    layout_path = directory / 'layout.xhtml'
    if layout_path.exists():
        for element in ET.parse(layout_path).iter():
            tag = element.tag.rsplit('}', 1)[-1]
            if tag == 'word':
                box = {k: float(element.attrib[k]) for k in ('xMin', 'yMin', 'xMax', 'yMax')}
                words.append(dict(box, text=element.text or ''))
                if box['xMin'] < -0.1 or box['yMin'] < -0.1 or box['xMax'] > width + 0.1 or box['yMax'] > height + 0.1:
                    errors.append('Text extends beyond the page')
            elif tag == 'line':
                lines.append(dict({k: float(element.attrib[k]) for k in ('xMin', 'yMin', 'xMax', 'yMax')},
                                  text=' '.join(e.text or '' for e in element)))
    atomic_json(directory / 'layout.json', {'words': words, 'lines': lines})
    renders = []
    if pages is not None and pages <= 5:
        result = run([tools['pdftoppm'], '-png', '-r', '120', pdf, directory / 'page'])
        if result.returncode:
            errors.append('PDF render failed: ' + result.stderr.strip())
        renders = [str(p) for p in sorted(directory.glob('page-*.png'))]
        if len(renders) != pages:
            errors.append('Rendered page count is incorrect')
    return {'checked_at': now(), 'pdf_sha256': digest(pdf), 'pages': pages, 'page_size_pt': size, 'fonts': fonts,
            'text_nonempty': bool(text), 'expected_content_checks': len(expected), 'missing_content': missing,
            'renders': renders, 'text_bottom_pt': max((w['yMax'] for w in words), default=None),
            'mechanical_ok': not errors, 'errors': sorted(set(errors)), 'visual_review': 'pending',
            'review_scope': 'master_cv' if require_master else 'page/text/fonts/render only'}


def warnings(log):
    patterns = {'overfull': r'Overfull \\[hv]box[^\n]*', 'missing_glyph': r'Missing character:[^\n]*',
                'font_substitution': r'LaTeX Font Warning:[^\n]*|Font shape[^\n]*not available',
                'rerun': r'Rerun to get[^\n]*|Label\(s\) may have changed[^\n]*'}
    return {key: re.findall(pattern, log) for key, pattern in patterns.items()}


def compile_source(source, manifest, directory, expected_extra=()):
    """Compile one source safely and run every mechanical check. Returns (status, validation)."""
    tools = require()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_text(directory / 'cv.tex', source)
    env = dict(os.environ)
    for key in ('TEXINPUTS', 'TEXMFOUTPUT', 'TEXMFCONFIG', 'TEXMFVAR', 'TEXMFHOME'):
        env.pop(key, None)
    env.update({'openin_any': 'p', 'openout_any': 'p'})
    command = [tools['pdflatex'], '-no-shell-escape', '-interaction=nonstopmode', '-halt-on-error',
               '-file-line-error', '-recorder', 'cv.tex']
    result = run(command, directory=directory, env=env, timeout=120)
    atomic_text(directory / 'compiler-output.txt', result.stdout + result.stderr)
    log_path = directory / 'cv.log'
    log = log_path.read_bytes().decode('utf-8', 'replace') if log_path.exists() else result.stdout
    found = warnings(log)
    if result.returncode != 0 or not (directory / 'cv.pdf').is_file():
        raise RuntimeError('pdfLaTeX failed (exit %s); see %s' % (result.returncode, directory / 'compiler-output.txt'))
    if found['rerun']:
        second = run(command, directory=directory, env=env, timeout=120)
        atomic_text(directory / 'compiler-second-pass.txt', second.stdout + second.stderr)
        if second.returncode:
            raise RuntimeError('Required second pdfLaTeX pass failed')
        found = warnings(log_path.read_bytes().decode('utf-8', 'replace'))
    expected = locked_visible_text(source) + list(expected_extra)
    checked = inspect_pdf(directory / 'cv.pdf', directory / 'qa', manifest, expected)
    checked['warnings'] = found
    for key in ('overfull', 'missing_glyph', 'font_substitution'):
        if found[key]:
            checked['errors'].append(key + ': ' + '; '.join(found[key][:5]))
    checked['mechanical_ok'] = not checked['errors']
    return checked


def compile_cv(patch, ws=None, application_id=None, test=False, host='unknown', emphasis=None, review=None,
               retry_of=None):
    platform(host)
    ws = require_workspace(ws)
    if not review or review.get('facts_preserved') is not True or review.get('voice_checked') is not True:
        raise ValueError('Content needs a factual and editorial review before compiling')
    require()
    revision = context_revision(ws)
    if review.get('context_revision'):
        managed.assert_fresh(ws, review['context_revision'], 'cv', 'This CV content')
    store = Store(ws, test=test)
    record = store.load(application_id) if application_id else None
    if record and not store.analysis(application_id)['valid']:
        raise ValueError('The application needs a current JD/evidence analysis before compiling')
    data = template(ws)
    assembled = assemble(patch, emphasis, ws, data=data)
    builds = contained(ws, layout.sandboxed(layout.CV_BUILDS, test))
    retries = 0
    if retry_of:
        previous_dir = Path(retry_of).resolve()
        if builds.resolve() not in previous_dir.parents:
            raise ValueError('Retry must refer to a CV build in this workspace')
        previous = read_json(previous_dir / 'build.json')
        prior = read_json(previous_dir / 'validation.json')
        if previous['status'] != 'validation-failed' or (prior.get('pages') == 1 and not prior.get('warnings', {}).get('overfull')):
            raise ValueError('A targeted retry needs a measured page or box overflow')
        if previous['application_id'] != application_id or previous['fictional'] != test or \
                any(previous['context_revision']['files'].get(p) != revision['files'].get(p) for p in managed.GROUPS['cv']):
            raise ValueError('A retry must keep the same application and profile')
        retries = previous.get('retry_count', 0) + 1
        if retries > 3:
            raise ValueError('Three targeted retries used; report the remaining overflow instead')
    run_id = uuid.uuid4().hex
    directory = builds / (application_id or 'baseline') / run_id
    directory.mkdir(parents=True)
    atomic_json(directory / 'content.json', patch)
    atomic_json(directory / 'assembly.json', {k: v for k, v in assembled.items() if k != 'source'})
    atomic_json(directory / 'emphasis.json', emphasis or {})
    build = {'schema_version': 1, 'run_id': run_id, 'directory': directory.relative_to(ws).as_posix(),
             'application_id': application_id, 'fictional': test, 'platform': host, 'generated_at': now(),
             'context_revision': revision, 'jd_sha256': record['jd_versions'][-1]['sha256'] if record else None,
             'source_sha256': assembled['source_sha256'], 'status': 'compiling', 'review': review,
             'pdf_path': None, 'retry_of': Path(retry_of).resolve().relative_to(ws).as_posix() if retry_of else None,
             'retry_count': retries}
    atomic_json(directory / 'build.json', build)
    manifest = data['manifest']
    slot_values = [value(patch, n, manifest) for n in data['slots']]
    try:
        checked = compile_source(assembled['source'], manifest, directory, slot_values)
        checked['source_checks'] = check_source(read_text(directory / 'cv.tex'), assembled)
        if not checked['source_checks']['ok']:
            checked['errors'].extend(checked['source_checks']['errors'])
            checked['mechanical_ok'] = False
        checked['overflow_feedback'] = {
            'page_count': checked['pages'], 'text_bottom_pt': checked['text_bottom_pt'],
            'overfull_boxes': checked['warnings']['overfull'],
            'region_feedback': region_lines(patch, data, directory / 'qa' / 'layout.json'),
            'action': 'Shorten editable content only; keep the design and locked text. At most three targeted retries.'}
        atomic_json(directory / 'validation.json', checked)
        build['status'] = 'awaiting-visual-review' if checked['mechanical_ok'] else 'validation-failed'
        build['render_paths'] = [Path(p).relative_to(ws).as_posix() for p in checked['renders']]
    except (RuntimeError, OSError, ValueError, ET.ParseError) as error:
        build['status'] = 'compile-failed'
        build['error'] = str(error)
    atomic_json(directory / 'build.json', build)
    return build


def region_lines(patch, data, layout_path):
    if not Path(layout_path).exists():
        return {}
    lines = read_json(layout_path)['lines']
    feedback = {}
    for name in data['slots']:
        content = normalize(value(patch, name, data['manifest']))
        tokens = content.split()
        if not tokens:
            continue
        anchor = normalize(' '.join(tokens[:min(6, len(tokens))]))
        matches = [line for line in lines if anchor in normalize(line['text'])]
        feedback[name] = {'characters': len(value(patch, name, data['manifest'])), 'words': len(tokens),
                          'start_y_pt': matches[0]['yMin'] if matches else None}
    return feedback


def safe_name(text):
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()
    return re.sub(r'[^A-Za-z0-9]+', '_', text).strip('_')[:70] or 'Role'


def filename(manifest, company, role, application, version, run):
    pattern = manifest.get('filename_pattern') or '{prefix}_{company}_{role}_{application}_v{version:03d}_{run}.pdf'
    return pattern.format(prefix=safe_name(manifest.get('file_prefix') or 'CV'), company=safe_name(company),
                          role=safe_name(role), application=safe_name(application), version=version, run=run[:8])


def promote(directory, visual_review, ws=None):
    ws = require_workspace(ws)
    directory = Path(directory).resolve()
    if not any(contained(ws, layout.sandboxed(layout.CV_BUILDS, t)).resolve() in directory.parents for t in (False, True)):
        raise ValueError('The build must live in this workspace\'s CV build storage')
    build = read_json(directory / 'build.json')
    checked = read_json(directory / 'validation.json')
    if build['status'] == 'promoted':
        final = ws / build['pdf_path']
        if not final.is_file() or digest(final) != build['final_pdf_sha256']:
            raise ValueError('Promoted PDF integrity failed')
        return build
    if build['status'] != 'awaiting-visual-review' or not checked['mechanical_ok']:
        raise ValueError('Only a mechanically passing build can be promoted')
    if not visual_review or visual_review.get('passed') is not True or not visual_review.get('reviewed_by') \
            or not visual_review.get('notes'):
        raise ValueError('Look at every render and record reviewer, passed=true and concrete notes')
    renders = {Path(p).resolve().relative_to(ws).as_posix(): digest(p) for p in checked['renders']}
    if visual_review.get('render_sha256') != renders:
        raise ValueError('The visual receipt must fingerprint every current render (workspace-relative paths)')
    pdf = directory / 'cv.pdf'
    if digest(pdf) != checked['pdf_sha256']:
        raise ValueError('PDF changed after validation')
    if not check_source(read_text(directory / 'cv.tex'), read_json(directory / 'assembly.json'))['ok']:
        raise ValueError('Source changed after validation')
    current = context_revision(ws)
    if any(build['context_revision']['files'].get(p) != current['files'].get(p) for p in managed.GROUPS['cv']):
        raise ValueError('Profile or master CV changed; compile a current version')
    manifest = read_json(ws / layout.CV_MANIFEST)
    store = Store(ws, test=build['fictional'])
    app_id = build['application_id']
    out_dir = contained(ws, layout.sandboxed(layout.CV_OUTPUTS, build['fictional']))
    lock_dir = store.directory(app_id) if app_id else out_dir
    with locked(lock_dir):
        record = store.load(app_id) if app_id else None
        build_rel = directory.relative_to(ws).as_posix()
        committed = next((x for x in record['outputs'] if x.get('kind') == 'cv-pdf' and x.get('build_directory') == build_rel), None) if record else None
        if committed:
            final = ws / committed['path']
            if not final.is_file() or digest(final) != committed['sha256']:
                raise ValueError('Previously committed build artifact integrity failed')
            build.update({'status': 'promoted', 'pdf_path': committed['path'], 'final_pdf_sha256': digest(final),
                          'version': committed['version']})
            atomic_json(directory / 'build.json', build)
            return build
        if record and (record['jd_versions'][-1]['sha256'] != build['jd_sha256'] or not store.analysis(app_id)['valid']):
            raise ValueError('JD or evidence changed after compiling')
        version = 1 + sum(x['kind'] == 'cv-pdf' for x in record['outputs']) if record else 1
        name = filename(manifest, record['company'] if record else 'Baseline', record['role'] if record else 'Master CV',
                        app_id or 'BASELINE', version, build['run_id'])
        final = out_dir / name
        if final.exists():
            raise ValueError('Refusing to overwrite an earlier PDF')
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pdf, final)
        if digest(final) != checked['pdf_sha256']:
            final.unlink()
            raise ValueError('Copied PDF does not match the validated artifact; nothing promoted')
        checked['visual_review'] = dict(visual_review, reviewed_at=now())
        checked['validated'] = True
        atomic_json(directory / 'validation.json', checked)
        rel = final.relative_to(ws).as_posix()
        if record:
            record['outputs'].append({'output_id': uuid.uuid4().hex, 'kind': 'cv-pdf', 'version': version, 'path': rel,
                                      'path_root': 'workspace', 'sha256': digest(final),
                                      'generated_at': build['generated_at'], 'platform': build['platform'],
                                      'context_revision': build['context_revision'], 'context_group': 'cv',
                                      'jd_sha256': build['jd_sha256'], 'state': 'generated', 'validation_status': 'passed',
                                      'build_directory': build_rel,
                                      'validation_path': build_rel + '/validation.json'})
            atomic_json(store.directory(app_id) / 'record.json', record)
        build.update({'status': 'promoted', 'pdf_path': rel, 'final_pdf_sha256': digest(final), 'version': version})
        atomic_json(directory / 'build.json', build)
    return build


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__))
    sub = parser.add_subparsers(dest='command', required=True)
    child = sub.add_parser('compile')
    child.add_argument('--patch', type=Path, required=True)
    child.add_argument('--review', type=Path, required=True)
    child.add_argument('--emphasis', type=Path)
    child.add_argument('--application-id')
    child.add_argument('--test', action='store_true')
    child.add_argument('--platform', required=True)
    child.add_argument('--retry-of', type=Path, help='earlier build with measured overflow (max three retries)')
    child = sub.add_parser('promote')
    child.add_argument('directory', type=Path)
    child.add_argument('--visual-review', type=Path, required=True)
    child = sub.add_parser('inspect')
    child.add_argument('pdf', type=Path)
    child.add_argument('--output-dir', type=Path)
    child.add_argument('--external', action='store_true', help='inspect another CV without assuming master fidelity')
    args = parser.parse_args()
    ws = require_workspace(args.workspace)
    if args.command == 'compile':
        review = read_json(args.review)
        if not review.get('context_revision'):
            raise ValueError('Review needs the context_revision from the task read')
        result = compile_cv(read_json(args.patch), ws, args.application_id, args.test, args.platform,
                            read_json(args.emphasis) if args.emphasis else None, review, args.retry_of)
    elif args.command == 'promote':
        result = promote(args.directory, read_json(args.visual_review), ws)
    else:
        directory = args.output_dir or ws / layout.CV_REVIEWS / uuid.uuid4().hex
        manifest = None if args.external else read_json(ws / layout.CV_MANIFEST)
        result = inspect_pdf(args.pdf, directory, manifest, require_master=not args.external)
        atomic_json(Path(directory) / 'inspection.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get('status') in ('compile-failed', 'validation-failed') or result.get('mechanical_ok') is False:
        return 1
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        raise SystemExit('CV PDF operation failed: ' + str(error))
