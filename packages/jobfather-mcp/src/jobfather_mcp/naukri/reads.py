"""Reading Naukri pages: the page's own JSON first, the rendered DOM as a fallback.

Each function opens one page through the session (spaced and counted), parses it with the pure
parsers and returns plain dicts for the tool models. A page that loads but matches neither the
JSON nor the DOM expectations raises ``site_changed`` with the site-profile version.
"""
from __future__ import annotations

import asyncio
import re
import time
import urllib.parse
from typing import Any

from ..errors import JFError
from . import parse
from .auth import require_auth
from .session import Loaded, Session
from .site import JobRef

PROFILE_CACHE_S = 1800


def provenance(session: Session, loaded: Loaded, method: str) -> dict[str, Any]:
    return {'captured_at': loaded.captured_at, 'method': method, 'site_profile': session.site.version,
            'untrusted_content': True}


def changed(session: Session, what: str) -> JFError:
    return JFError('site_changed', 'Naukri\'s %s page no longer matches what the server expects (site profile %s).'
                   % (what, session.site.version), False,
                   user_action='Tell the user the Naukri server needs an update; use the browser or pasted links '
                               'meanwhile.')


def _visible_text(markup: str) -> str:
    return parse.parse_html(markup).text(separator='\n') if markup else ''


# ------------------------------------------------------------------ search
async def search(session: Session, url: str, page_no: int, limit: int) -> dict[str, Any]:
    spec = session.site.section('search')
    loaded = await session.load(url, actions=['search'], expect={'search': spec['response']['url_regex']},
                                required=('search',))
    warnings: list[str] = []
    method = 'page_json'
    result = None
    served_page = None
    item = loaded.first('search')
    if item is not None:
        response_url, body = item
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(response_url).query))
        served = query.get(spec['response']['page_param'])
        served_page = int(served) if served and served.isdigit() else None
        try:
            result = parse.search_from_json(body, session.site)
        except (ValueError, TypeError, KeyError):
            result = None
    if result is None:
        method = 'dom'
        result = parse.jobs_from_dom(loaded.html, session.site, 'search')
        result['empty'] = not result['jobs'] and bool(re.search(spec['empty_text_regex'], _visible_text(loaded.html),
                                                                re.I))
        if not result['jobs'] and not result['empty']:
            raise changed(session, 'search results')
        if served_page is None:
            match = re.search(r'-(\d+)$', urllib.parse.urlsplit(loaded.url).path)
            served_page = int(match.group(1)) if match else 1
    if page_no > 1 and served_page is not None and served_page != page_no:
        warnings.append('Naukri showed page %d instead of page %d (paging may need a login); no jobs returned for '
                        'this page' % (served_page, page_no))
        result['jobs'] = []
        result['partial'] = True
    partial = bool(result.get('failed')) or bool(result.get('partial'))
    jobs = result['jobs'][:limit]
    return {'jobs': jobs, 'total': result.get('total'), 'partial': partial, 'warnings': warnings,
            'provenance': provenance(session, loaded, method)}


# ------------------------------------------------------------------ job pages
class JobGone(JFError):
    """The job page redirected away (expired or removed)."""


def _expired(session: Session, loaded: Loaded, ref: JobRef) -> bool:
    job = session.site.section('job')
    if re.search(job['expired_url_regex'], loaded.url):
        return True
    path = urllib.parse.urlsplit(loaded.url).path
    match = re.search(job['url_job_id_regex'], path)
    return match is None or match.group('id') != ref.job_id


async def read_job(session: Session, ref: JobRef, actions: list[str], with_similar: bool = False,
                   page: Any = None) -> tuple[Loaded, dict[str, Any], str]:
    """(loaded, JobDetail dict, method) for one job page."""
    site = session.site
    expect = {'job': site.section('job')['response']['url_regex'],
              'similar': site.section('similar')['response']['url_regex']}
    required = ('job', 'similar') if with_similar else ('job',)
    loaded = await session.load(ref.url, actions=actions, expect=expect, required=required,
                                ready=site.section('job')['dom']['ready'], page=page)
    if _expired(session, loaded, ref):
        raise JobGone('not_found', 'Naukri no longer shows job %s (it sent the browser to a search page; the job '
                      'expired or was removed).' % ref.job_id, False)
    detail = None
    method = 'page_json'
    for response_url, body in loaded.responses.get('job', []):
        match = re.search(site.section('job')['response']['url_regex'], response_url)
        if match and match.group('id') != ref.job_id:
            continue
        try:
            detail = parse.detail_from_json(body, site)
        except (ValueError, TypeError, KeyError):
            continue
        detail = parse.merge_dom_into_detail(detail, loaded.html)
        break
    if detail is None:
        method = 'dom'
        try:
            detail = parse.detail_from_dom(loaded.html, site, job_id=ref.job_id)
        except (ValueError, TypeError, KeyError):
            raise changed(session, 'job') from None
    if detail['job_id'] != ref.job_id:
        raise JFError('not_found', 'Naukri showed a different job than %s.' % ref.job_id, False)
    return loaded, detail, method


def logged_in_on_job_page(loaded: Loaded, site: Any) -> bool | None:
    key = site.section('job')['response'].get('logged_in_key')
    for _, body in loaded.responses.get('job', []):
        value = body.get(key) if isinstance(body, dict) and key else None
        if isinstance(value, bool):
            return value
    if loaded.logged_in_marker:
        return True
    if loaded.logged_out_marker or loaded.login_form:
        return False
    return None


async def similar(session: Session, ref: JobRef, limit: int) -> dict[str, Any]:
    loaded, _, _ = await read_job(session, ref, actions=['similar'], with_similar=True)
    jobs: list[dict[str, Any]] = []
    found = False
    for _, body in loaded.responses.get('similar', []):
        try:
            result = parse.similar_from_json(body, session.site)
        except (ValueError, TypeError, KeyError):
            continue
        found = True
        jobs = [j for j in result['jobs'] if j['job_id'] != ref.job_id]
        break
    if not found:
        raise changed(session, 'similar jobs')
    return {'source_job_id': ref.job_id, 'jobs': jobs[:limit], 'provenance': provenance(session, loaded, 'page_json')}


# ------------------------------------------------------------------ logged-in lists
async def job_list(session: Session, feature: str) -> dict[str, Any]:
    """Recommended or saved jobs (logged in). JSON job lists first, then DOM tuples."""
    site = session.site
    spec = site.section(feature)
    loaded = await session.load(site.url(spec['url']), actions=[feature],
                                expect={feature: spec['response']['url_regex']}, required=(feature,), wait_s=12)
    require_auth(loaded, session)
    jobs: list[dict[str, Any]] = []
    failed = 0
    seen: set[str] = set()
    method = 'page_json'
    reported_empty = False
    total_key = spec['response'].get('total_key')
    for _, body in loaded.responses.get(feature, []):
        if total_key and isinstance(body, dict) and body.get(total_key) == 0:
            reported_empty = True
        for items in parse.find_job_lists(body):
            more, bad = parse.jobs_from_json_list(items, site, seen=seen)
            jobs += more
            failed += bad
    if not jobs and reported_empty:
        return {'jobs': [], 'partial': False, 'provenance': provenance(session, loaded, method)}
    if not jobs:
        method = 'dom'
        result = parse.jobs_from_dom(loaded.html, site, feature)
        jobs, failed = result['jobs'], result['failed']
        if not jobs:
            empty = re.search(spec['empty_text_regex'], _visible_text(loaded.html), re.I)
            if not empty:
                raise changed(session, feature)
    return {'jobs': jobs, 'partial': failed > 0, 'provenance': provenance(session, loaded, method)}


# ------------------------------------------------------------------ profile
async def _settle_text(loaded: Loaded, pattern: str | None, tries: int = 10) -> None:
    """Some sections render after the page's JSON arrives: re-read the page briefly until ``pattern`` shows."""
    if not pattern or loaded.page is None or not hasattr(loaded.page, 'content'):
        return
    for _ in range(tries):
        if re.search(pattern, _visible_text(loaded.html), re.I):
            return
        try:
            await loaded.page.mouse.wheel(0, 900)  # lower sections render when scrolled into view, as for a reader
            await asyncio.sleep(0.5)
            loaded.html = await loaded.page.content()
        except Exception:
            return
async def profile(session: Session, use_cache: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """(allow-listed profile summary, provenance). Never returns contact fields."""
    if use_cache and session.profile_cache and time.monotonic() - session.profile_cache[0] < PROFILE_CACHE_S:
        return session.profile_cache[1]
    site = session.site
    spec = site.section('profile')
    parts = spec['response'].get('parts') or {'profile': spec['response']['url_regex']}
    loaded = await session.load(site.url(spec['url']), actions=[], expect=parts, required=tuple(parts), wait_s=12)
    require_auth(loaded, session)
    await _settle_text(loaded, spec.get('text_patterns', {}).get('preferred_locations'))
    bodies = [body for name in parts for _, body in loaded.responses.get(name, [])]
    summary = parse.profile_summary(bodies, loaded.html, site)
    if not summary.pop('found'):
        raise changed(session, 'profile')
    method = 'page_json' if bodies and site.section('profile').get('json_paths') else 'dom'
    result = (summary, provenance(session, loaded, method))
    session.profile_cache = (time.monotonic(), result)
    return result
