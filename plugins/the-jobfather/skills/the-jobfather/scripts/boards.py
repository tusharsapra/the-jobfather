"""List openings from public job-board APIs (Greenhouse, Lever, Ashby) in job-intake format.

No login and no scraping: these endpoints exist for publishing job listings.

    boards.py list --ats greenhouse --board examplecorp [--match "data analyst"] [--location toronto]
    boards.py targets [--per-company 10]     # every target company in market.json with a known board
Output is a job-intake batch ({"jobs": [...]}) for `job_intake.py ingest`.
"""
import argparse
from datetime import datetime, timedelta, timezone
import html
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import market
from local import add_workspace_arg, atomic_json, require_workspace

USER_AGENT = 'the-jobfather/0.1 (+https://github.com/tusharsapra/the-jobfather)'
ENDPOINTS = {
    'greenhouse': 'https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true',
    'lever': 'https://api.lever.co/v0/postings/{board}?mode=json',
    'ashby': 'https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true',
}
BOARD = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')


def fetch(ats, board, opener=urllib.request.urlopen, timeout=20):
    if ats not in ENDPOINTS:
        raise ValueError('Supported boards: ' + ', '.join(ENDPOINTS))
    if not BOARD.match(board or ''):
        raise ValueError('Invalid board token: %r' % board)
    url = ENDPOINTS[ats].format(board=urllib.parse.quote(board))
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    try:
        with opener(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ValueError('%s board %r not found (the company may have moved to another system)' % (ats, board))
        raise ValueError('%s board %r returned HTTP %s' % (ats, board, error.code))
    except urllib.error.URLError as error:
        raise ValueError('Could not reach %s: %s' % (ats, error.reason))


def html_text(markup):
    """Readable text from listing HTML (Greenhouse escapes its HTML once more)."""
    text = html.unescape(markup or '')
    if '&lt;' in (markup or '') and '<' not in (markup or ''):
        text = html.unescape(text)
    text = re.sub(r'(?is)<(script|style).*?</\1>', ' ', text)
    text = re.sub(r'(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>', '\n', text)
    text = re.sub(r'(?i)<li[^>]*>', '- ', text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'[ \t ]+', ' ', text)
    return re.sub(r'\n\s*\n+', '\n\n', text).strip()


def _job(company, ats, board, role, location, url, full_jd, posted=None, extra=None):
    full_jd = full_jd or ''
    role = (role or '').strip() or None
    job = {'company': company, 'role': role, 'location': (location or '').strip() or None, 'application_url': url,
           'application_method': '%s job board' % ats.capitalize(),
           'jd_summary': (full_jd[:280].rsplit(' ', 1)[0] + '...') if len(full_jd) > 280 else (full_jd or None),
           'full_jd': full_jd or None, 'posted_date': posted,
           'sources': [{'type': 'url', 'label': '%s board: %s' % (ats.capitalize(), board), 'url': url,
                        'text': full_jd or role, 'access': 'readable' if full_jd else 'partial'}]}
    job.update({k: v for k, v in (extra or {}).items() if v})
    return job


def normalize(ats, board, data, company=None):
    company = company or board
    jobs = []
    if ats == 'greenhouse':
        for item in data.get('jobs', []):
            jobs.append(_job(company, ats, board, item.get('title'), (item.get('location') or {}).get('name'),
                             item.get('absolute_url'), html_text(item.get('content')),
                             (item.get('updated_at') or '')[:10] or None,
                             {'requisition_id': item.get('requisition_id')}))
    elif ats == 'lever':
        for item in data if isinstance(data, list) else []:
            parts = [item.get('descriptionPlain') or html_text(item.get('description'))]
            for block in item.get('lists', []):
                parts.append((block.get('text') or '') + '\n' + html_text(block.get('content')))
            parts.append(item.get('additionalPlain') or html_text(item.get('additional')))
            cats = item.get('categories') or {}
            created = item.get('createdAt')
            posted = datetime.fromtimestamp(created / 1000, timezone.utc).date().isoformat() if created else None
            jobs.append(_job(company, ats, board, item.get('text'), cats.get('location'), item.get('hostedUrl'),
                             '\n\n'.join(p for p in parts if p).strip(), posted,
                             {'employment_type': cats.get('commitment'), 'work_mode': item.get('workplaceType')}))
    elif ats == 'ashby':
        for item in data.get('jobs', []):
            comp = (item.get('compensation') or {}).get('compensationTierSummary')
            jobs.append(_job(company, ats, board, item.get('title'), item.get('location'), item.get('jobUrl'),
                             item.get('descriptionPlain') or html_text(item.get('descriptionHtml')),
                             (item.get('publishedAt') or '')[:10] or None,
                             {'employment_type': item.get('employmentType'), 'salary': comp,
                              'work_mode': 'Remote' if item.get('isRemote') else item.get('workplaceType')}))
    return [j for j in jobs if j['role'] and j['application_url']]


def filter_jobs(jobs, match=None, location=None, posted_within_days=None, today=None):
    terms = [t.strip().casefold() for t in (match or []) if t.strip()]
    places = [p.strip().casefold() for p in (location or []) if p.strip()]
    cutoff = None
    if posted_within_days:
        cutoff = ((today or datetime.now(timezone.utc).date()) - timedelta(days=posted_within_days)).isoformat()
    kept = []
    for job in jobs:
        title = (job['role'] or '').casefold()
        if terms and not any(t in title for t in terms):
            continue
        if places and not any(p in (job.get('location') or '').casefold() for p in places):
            continue
        if cutoff and job.get('posted_date') and job['posted_date'] < cutoff:
            continue
        kept.append(job)
    return kept


def targets(ws, per_company=10, pause=1.0, opener=urllib.request.urlopen):
    value = market.load(ws) or {}
    titles = market.titles(value) or None
    places = [loc['name'] for loc in value.get('geography', {}).get('locations', [])]
    places += [m for loc in value.get('geography', {}).get('locations', []) for m in loc.get('match', [])]
    batch, report = [], []
    for company in value.get('target_companies', []):
        ats, board = company.get('ats'), company.get('board_token')
        if ats not in ENDPOINTS or not board:
            report.append({'company': company.get('name'), 'skipped': 'no public board known'})
            continue
        try:
            jobs = normalize(ats, board, fetch(ats, board, opener), company.get('name'))
        except ValueError as error:
            report.append({'company': company.get('name'), 'error': str(error)})
            continue
        kept = filter_jobs(jobs, titles, places or None)[:per_company]
        batch += kept
        report.append({'company': company.get('name'), 'listed': len(jobs), 'matched': len(kept)})
        time.sleep(pause)
    return {'jobs': batch}, report


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    sub = parser.add_subparsers(dest='command', required=True)
    child = sub.add_parser('list')
    child.add_argument('--ats', choices=sorted(ENDPOINTS), required=True)
    child.add_argument('--board', required=True)
    child.add_argument('--company')
    child.add_argument('--match', action='append', help='title words (repeatable); default: titles from market.json')
    child.add_argument('--location', action='append')
    child.add_argument('--posted-within-days', type=int)
    child.add_argument('--limit', type=int, default=25)
    child.add_argument('--out', type=Path)
    child = sub.add_parser('targets')
    child.add_argument('--per-company', type=int, default=10)
    child.add_argument('--out', type=Path)
    args = parser.parse_args()
    if args.command == 'list':
        jobs = normalize(args.ats, args.board, fetch(args.ats, args.board), args.company)
        match = args.match
        if match is None:
            try:
                match = market.titles(market.load(require_workspace(args.workspace)) or {}) or None
            except Exception:
                match = None
        result, report = {'jobs': filter_jobs(jobs, match, args.location, args.posted_within_days)[:args.limit]}, None
    else:
        result, report = targets(require_workspace(args.workspace), args.per_company)
    if args.out:
        atomic_json(args.out, result)
        print(json.dumps({'written': str(args.out), 'jobs': len(result['jobs']), 'report': report}, indent=2))
    else:
        print(json.dumps(dict(result, report=report) if report else result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except ValueError as error:
        raise SystemExit('Board lookup failed: ' + str(error))
