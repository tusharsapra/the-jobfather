"""The user's job-market model (profile/market.json): schema, taxonomy, ranking, queries.

market.json is written by the market-map workflow and changed through memory.py
(change_type "market"). Every item carries a source: user-confirmed,
inferred-from-JDs or researched.
"""
import argparse
import json
from pathlib import Path
import re

import layout
from local import add_workspace_arg, read_json, require_workspace, sha_json

SOURCES = ('user-confirmed', 'inferred-from-JDs', 'researched')
STATUSES = ('core', 'adjacent', 'stretch', 'excluded')
EXCLUSION_KINDS = ('seniority', 'sector', 'skill', 'pay', 'notice', 'shift', 'relocation', 'company', 'title',
                   'contract', 'other')
GENERIC_TAXONOMY = {
    'employer_type': {'direct_employer': 'Hiring for its own team',
                      'agency': 'Serves clients (agency, studio, consultancy)',
                      'recruitment_agency': 'Recruiter hiring for a client',
                      'consultancy': 'Advisory or professional-services firm'},
    'role_track': {'core': 'Matches the main target role', 'adjacent': 'Neighbouring role',
                   'stretch': 'Above or beside current experience'},
}
DEFAULT_ATS_SENDERS = ['greenhouse.io', 'lever.co', 'ashbyhq.com', 'myworkday.com', 'myworkdayjobs.com',
                       'icims.com', 'smartrecruiters.com', 'workable.com', 'recruitee.com', 'jobvite.com',
                       'bamboohr.com', 'keka.com', 'darwinbox.in', 'zohorecruit.com', 'teamtailor.com']
DEFAULT_WEIGHTS = {'title': 3, 'must_skills': 3, 'seniority': 2, 'location': 2, 'nice_skills': 1, 'freshness': 1}
DEFAULT_TIE_BREAKS = ['priority', 'closing_soon', 'location_rank', 'network', 'published_contact', 'job_id']
SLUG = re.compile(r'^[a-z][a-z0-9_]{1,39}$')


def _require(cond, message):
    if not cond:
        raise ValueError('market.json: ' + message)


def _source_ok(item, where):
    if isinstance(item, dict) and 'source' in item:
        _require(item['source'] in SOURCES, '%s source must be one of %s' % (where, ', '.join(SOURCES)))


def validate(value):
    """Lenient on completeness (a map grows over time), strict on shapes and enums."""
    _require(isinstance(value, dict) and value.get('schema_version') == 1, 'needs schema_version 1')
    taxonomy = value.get('taxonomy')
    if taxonomy is not None:
        _require(isinstance(taxonomy, dict), 'taxonomy must be an object')
        for dim in ('employer_type', 'role_track'):
            if dim in taxonomy:
                values = taxonomy[dim].get('values') if isinstance(taxonomy[dim], dict) else None
                _require(isinstance(values, dict) and 1 <= len(values) <= 12, '%s needs 1-12 values' % dim)
                for key in values:
                    _require(SLUG.match(key) and key != 'unclear', 'bad %s value %r' % (dim, key))
    for i, family in enumerate(value.get('role_families', [])):
        _require(isinstance(family, dict) and family.get('id') and family.get('name'), 'role_families[%d] needs id and name' % i)
        _require(family.get('status', 'core') in STATUSES, 'role_families[%d] status' % i)
        for title in family.get('titles', []):
            _require(isinstance(title, dict) and title.get('title'), 'role_families[%d] titles need title' % i)
            _require(title.get('status', 'core') in STATUSES, 'title status for %r' % title.get('title'))
            _source_ok(title, 'title %r' % title.get('title'))
    seniority = value.get('seniority', {})
    _require(isinstance(seniority, dict), 'seniority must be an object')
    skills = value.get('skills', {})
    for bucket in ('must', 'nice', 'never_done'):
        for item in skills.get(bucket, []):
            _require(isinstance(item, dict) and item.get('name'), 'skills.%s items need name' % bucket)
            _source_ok(item, 'skill %r' % item.get('name'))
    for company in value.get('target_companies', []):
        _require(isinstance(company, dict) and company.get('name'), 'target_companies need name')
        if company.get('careers_url'):
            _require(re.match(r'^https?://', company['careers_url']), 'careers_url must be http(s)')
        _source_ok(company, 'company %r' % company.get('name'))
    locations = value.get('geography', {}).get('locations', [])
    for loc in locations:
        _require(isinstance(loc, dict) and loc.get('name') and isinstance(loc.get('rank', 1), int),
                 'geography.locations need name and integer rank')
    for item in value.get('exclusions', []):
        _require(isinstance(item, dict) and item.get('kind') in EXCLUSION_KINDS and item.get('value'),
                 'exclusions need kind (%s) and value' % ', '.join(EXCLUSION_KINDS))
        _source_ok(item, 'exclusion %r' % item.get('value'))
    for query in value.get('queries', []):
        _require(isinstance(query, dict) and query.get('platform') and query.get('query'), 'queries need platform and query')
    weights = value.get('scoring', {}).get('weights', {})
    _require(all(isinstance(v, (int, float)) and v >= 0 for v in weights.values()), 'scoring weights must be >= 0')
    return value


def load(ws):
    path = Path(ws) / layout.MARKET_JSON
    return validate(read_json(path)) if path.is_file() else None


def taxonomy(ws):
    """Fit-analysis dimensions: the user's own, or the generic set when no market map exists."""
    value = load(ws)
    dims = (value or {}).get('taxonomy') or {}
    result = {'source': 'market' if dims else 'generic', 'dimensions': {}}
    for dim, generic in GENERIC_TAXONOMY.items():
        values = (dims.get(dim) or {}).get('values') or generic
        result['dimensions'][dim] = sorted(values)
    result['sha256'] = sha_json(result['dimensions'])
    return result


def location_rank(ws, location_text, market_value=None):
    """Lower is better; unknown locations rank last."""
    value = market_value if market_value is not None else (load(ws) or {})
    text = (location_text or '').casefold()
    best = 10 ** 6
    for loc in value.get('geography', {}).get('locations', []):
        keys = [loc['name']] + list(loc.get('match', []))
        if any(k and k.casefold() in text for k in keys):
            best = min(best, int(loc.get('rank', 999)))
    return best


def ats_senders(ws):
    value = load(ws) or {}
    return value.get('ats_senders') or list(DEFAULT_ATS_SENDERS)


def scoring(ws):
    value = (load(ws) or {}).get('scoring', {})
    return {'weights': dict(DEFAULT_WEIGHTS, **value.get('weights', {})),
            'tie_breaks': value.get('tie_breaks') or list(DEFAULT_TIE_BREAKS)}


# ------------------------------------------------------------------ query building
def _quote(term):
    return '"%s"' % term.replace('"', '') if ' ' in term else term


def titles(value, statuses=('core', 'adjacent')):
    out = []
    for family in value.get('role_families', []):
        if family.get('status', 'core') not in statuses:
            continue
        for title in family.get('titles', []):
            if title.get('status', 'core') in statuses:
                out.append(title['title'])
        out += [s for s in family.get('synonyms', []) if family.get('status', 'core') in statuses]
    return list(dict.fromkeys(out))


def excluded_terms(value):
    terms = []
    for family in value.get('role_families', []):
        for title in family.get('titles', []):
            if title.get('status') == 'excluded':
                terms.append(title['title'])
    terms += [e['value'] for e in value.get('exclusions', []) if e['kind'] in ('title', 'company', 'sector')]
    return list(dict.fromkeys(terms))


def build_queries(value, platform, location=None, limit_terms=6):
    """Simple first-page queries per platform from the title graph (the host refines them)."""
    core = titles(value, ('core',))[:limit_terms] or titles(value)[:limit_terms]
    negative = excluded_terms(value)[:4]
    if platform == 'linkedin':
        query = ' OR '.join(_quote(t) for t in core)
        query = '(%s)' % query if len(core) > 1 else query
        if negative:
            query += ' NOT (%s)' % ' OR '.join(_quote(t) for t in negative)
        return [{'platform': 'linkedin', 'query': query, 'location': location}]
    if platform == 'naukri':
        return [{'platform': 'naukri', 'query': t, 'location': location} for t in core[:3]]
    if platform == 'hiring-posts':
        return [{'platform': 'linkedin-posts', 'query': '(%s) (hiring OR vacancy OR openings)' % ' OR '.join(_quote(t) for t in core[:3]),
                 'location': location}]
    return [{'platform': platform, 'query': ' OR '.join(_quote(t) for t in core), 'location': location}]


def main():
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('validate')
    sub.add_parser('taxonomy')
    child = sub.add_parser('queries')
    child.add_argument('--platform', required=True)
    child.add_argument('--location')
    child = sub.add_parser('rank-location')
    child.add_argument('text')
    args = parser.parse_args()
    ws = require_workspace(args.workspace)
    if args.command == 'validate':
        result = {'ok': load(ws) is not None}
    elif args.command == 'taxonomy':
        result = taxonomy(ws)
    elif args.command == 'queries':
        result = build_queries(load(ws) or {'schema_version': 1}, args.platform, args.location)
    else:
        result = {'rank': location_rank(ws, args.text)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit('Market operation failed: ' + str(error))
