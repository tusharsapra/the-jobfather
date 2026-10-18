"""Where things are on Naukri: URL templates, locators, response matchers and markers.

Everything site-specific is data in ``site_profile.json`` (versioned by date, with a ``verified``
flag per feature). This module loads it, builds URLs, validates job references and decides which
URLs the server may open. Standard library only.

Test overrides (``JOBFATHER_NAUKRI_BASE``, ``JOBFATHER_NAUKRI_CHANNEL``,
``JOBFATHER_NAUKRI_HEADLESS``) are honoured only when ``JOBFATHER_TEST_MODE=1``, and the base URL
only when it is a loopback address.
"""
from __future__ import annotations

import copy
import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..errors import JFError
from .checkpoints import Rules

PROFILE_FILE = Path(__file__).resolve().parent / 'site_profile.json'
SCHEMA = 'the-jobfather/naukri-site-profile@1'
LOOPBACK = ('127.0.0.1', 'localhost', '::1')
JOB_ID = re.compile(r'^\d{6,20}$')
CHANNELS = ('chrome', 'chromium', 'msedge')


def test_mode() -> bool:
    return os.environ.get('JOBFATHER_TEST_MODE') == '1'


def _loopback_base() -> str | None:
    base = os.environ.get('JOBFATHER_NAUKRI_BASE', '').strip().rstrip('/')
    if not base or not test_mode():
        return None
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in ('http', 'https') or parts.hostname not in LOOPBACK:
        raise JFError('invalid_argument', 'JOBFATHER_NAUKRI_BASE must be a loopback http(s) address', False)
    return base


def browser_channel() -> str:
    """``chrome`` always, except in test mode where ``JOBFATHER_NAUKRI_CHANNEL`` may pick another."""
    if test_mode():
        channel = os.environ.get('JOBFATHER_NAUKRI_CHANNEL', '').strip()
        if channel in CHANNELS:
            return channel
    return 'chrome'


def headless() -> bool:
    """Never headless for users: they must see the window. Tests may opt in."""
    return test_mode() and os.environ.get('JOBFATHER_NAUKRI_HEADLESS') == '1'


@lru_cache(maxsize=1)
def _raw_profile() -> dict[str, Any]:
    data = json.loads(PROFILE_FILE.read_bytes().decode('utf-8'))
    if data.get('schema') != SCHEMA or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(data.get('version', ''))):
        raise ValueError('site_profile.json is not a %s file with a YYYY-MM-DD version' % SCHEMA)
    return data


def slug(text: str) -> str:
    """Lower-case words joined by hyphens, as Naukri's own search paths look."""
    words = re.findall(r'[a-z0-9]+', (text or '').lower().replace('+', ' plus ').replace('#', ' sharp '))
    return '-'.join(words)


@dataclass(frozen=True)
class JobRef:
    job_id: str
    url: str


class Site:
    """The loaded site profile plus the effective base URL."""

    def __init__(self, profile: Mapping[str, Any] | None = None) -> None:
        self.profile: dict[str, Any] = copy.deepcopy(dict(profile if profile is not None else _raw_profile()))
        self.version: str = self.profile['version']
        override = _loopback_base()
        self.test_base = override
        self.base_url: str = (override or self.profile['base_url']).rstrip('/')
        base = urllib.parse.urlsplit(self.base_url)
        self.scheme = base.scheme
        self.netloc = base.netloc
        self.hosts = frozenset([base.hostname or ''] if override else self.profile['hosts'])
        self.checkpoint_rules = Rules(self.profile.get('checkpoints', {}))

    # ------------------------------------------------------------------ data access
    def section(self, name: str) -> dict[str, Any]:
        return self.profile.get(name) or {}

    def verified(self, feature: str) -> bool:
        return bool((self.profile.get('features', {}).get(feature) or {}).get('verified'))

    def features(self) -> dict[str, Any]:
        return copy.deepcopy(self.profile.get('features', {}))

    def url(self, path_or_url: str) -> str:
        """An absolute URL on the effective base for a profile path (or a same-site URL unchanged)."""
        if path_or_url.startswith(('http://', 'https://')):
            parts = urllib.parse.urlsplit(path_or_url)
            if self.test_base and parts.hostname in self.profile['hosts']:
                return self.base_url + urllib.parse.urlunsplit(('', '', parts.path, parts.query, parts.fragment))
            return path_or_url
        return self.base_url + '/' + path_or_url.lstrip('/')

    # ------------------------------------------------------------------ safety
    def allowed(self, url: str) -> bool:
        """True for URLs the server may open: Naukri's own hosts over https (or the loopback test base)."""
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            return False
        if parts.username or parts.password:
            return False
        if self.test_base:
            return parts.scheme == self.scheme and parts.netloc == self.netloc
        return parts.scheme == 'https' and (parts.hostname or '') in self.hosts and parts.port in (None, 443)

    def is_login_url(self, url: str) -> bool:
        return bool(re.search(self.section('login')['login_url_regex'], url or '', re.I))

    # ------------------------------------------------------------------ job references
    def job_ref(self, ref: Any) -> JobRef:
        """Digits or an https://www.naukri.com job URL; anything else is ``invalid_argument``."""
        text = str(ref if ref is not None else '').strip()
        if JOB_ID.fullmatch(text):
            return JobRef(text, self.job_url(text))
        try:
            parts = urllib.parse.urlsplit(text)
        except ValueError:
            parts = None
        if parts is None or parts.scheme not in ('https', 'http') or not parts.netloc:
            raise JFError('invalid_argument', 'A job is its Naukri job id (digits) or its https://www.naukri.com '
                          'URL.', False)
        own_host = (parts.hostname or '').lower() in ('www.naukri.com', 'naukri.com')
        test_host = bool(self.test_base) and parts.netloc == self.netloc
        if parts.username or parts.password or not (own_host or test_host) or (own_host and parts.scheme != 'https'):
            raise JFError('invalid_argument', 'Only https://www.naukri.com job links are accepted; the server '
                          'never opens other sites.', False)
        if own_host and parts.port not in (None, 443):
            raise JFError('invalid_argument', 'Only https://www.naukri.com job links are accepted.', False)
        match = re.search(self.section('job')['url_job_id_regex'], parts.path)
        if not match:
            raise JFError('invalid_argument', 'That Naukri link is not a job page (expected a job-listings link '
                          'ending in the job id).', False)
        path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe='/-_.~')
        return JobRef(match.group('id'), self.base_url + path)

    def job_url(self, job_id: str) -> str:
        if not JOB_ID.fullmatch(job_id or ''):
            raise JFError('invalid_argument', 'Invalid Naukri job id', False)
        return self.url(self.section('job')['url_from_id'].format(job_id=job_id))

    def canonical_job_url(self, job_id: str, path: str | None) -> str:
        """A job URL from a site-relative path seen on a page, or the id-only form."""
        if path:
            absolute = self.url(path) if not path.startswith(('http://', 'https://')) else path
            if self.allowed(absolute):
                parts = urllib.parse.urlsplit(absolute)
                return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
        return self.job_url(job_id)

    # ------------------------------------------------------------------ search
    def search_url(self, keywords: str, location: str | None = None, experience_years: int | None = None,
                   posted_within_days: int | None = None, work_modes: Iterable[str] = (),
                   min_salary_lpa: float | None = None, sort: str = 'relevance', page: int = 1) -> tuple[str, list[str]]:
        """(url, warnings) for a search, built from the profile's ``search.url`` section."""
        spec = self.section('search')['url']
        warnings: list[str] = []
        kw_slug = slug(keywords) or 'all'
        loc_slug = slug(location) if location else ''
        path = spec['path'].format(keywords_slug=kw_slug)
        if loc_slug:
            path += spec['location_suffix'].format(location_slug=loc_slug)
        if page > 1:
            path += spec['page_suffix'].format(page=page)
        names = spec['params']
        query: list[tuple[str, str]] = [(names['keywords'], keywords.strip())]
        if location:
            query.append((names['location'], location.strip()))
        if experience_years is not None:
            query.append((names['experience_years'], str(int(experience_years))))
        if posted_within_days is not None:
            query.append((names['posted_within_days'], str(int(posted_within_days))))
        modes = spec.get('work_modes') or {}
        for mode in dict.fromkeys(work_modes or ()):
            value = (modes.get('values') or {}).get(mode)
            if value is None:
                warnings.append('work mode filter %r is not supported by the site profile; check each job' % mode)
                continue
            query.append((modes['param'], str(value)))
        if work_modes and not modes.get('verified'):
            warnings.append('the work-mode filter is not verified against the live site; check each job')
        if min_salary_lpa is not None:
            salary = spec.get('salary') or {}
            buckets = [b for b in salary.get('buckets', ()) if float(b['max_lpa']) > float(min_salary_lpa)]
            if float(min_salary_lpa) <= 0:
                buckets = []
            elif not buckets:
                warnings.append('no salary range reaches %s LPA; salary filter not applied' % min_salary_lpa)
            else:
                for bucket in buckets:
                    query.append((salary['param'], str(bucket['value'])))
                if not salary.get('verified'):
                    warnings.append('the salary filter is not verified against the live site; check each job')
                if float(buckets[0]['min_lpa']) < float(min_salary_lpa):
                    warnings.append('Naukri filters by salary ranges; the %s-%s LPA range can include jobs paying less '
                                    'than %s LPA' % (buckets[0]['min_lpa'], buckets[0]['max_lpa'], min_salary_lpa))
                warnings.append('jobs that do not disclose a salary are kept by the site filter')
        if sort and sort != 'relevance':
            order = spec.get('sort') or {}
            value = (order.get('values') or {}).get(sort)
            if value is None:
                warnings.append('sort %r is not supported by the site profile; results use relevance' % sort)
            else:
                query.append((order['param'], str(value)))
                if not order.get('verified'):
                    warnings.append('date sorting is not verified against the live site')
        url = self.url(path) + '?' + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
        return url, warnings


_SITE: Site | None = None


def site() -> Site:
    """The process-wide Site (rebuilt when test overrides change)."""
    global _SITE
    override = _loopback_base()
    if _SITE is None or _SITE.test_base != override:
        _SITE = Site()
    return _SITE


def reset() -> None:
    global _SITE
    _SITE = None
    _raw_profile.cache_clear()
