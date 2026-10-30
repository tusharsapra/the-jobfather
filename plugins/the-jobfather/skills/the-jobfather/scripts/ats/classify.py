"""Name the applicant tracking system (ATS) behind an application link, with its board and job ids.

    classify.py --url URL [--resolve]
    classify.py --file LINKS.txt [--resolve]     # one link per line; blank lines and # comments skipped

Offline by default. --resolve follows short links (grnh.se, lnkd.in, bit.ly ...) with HEAD
requests only. Output (JSON): {url, ats, board, job_id, apply_url, supported_autofill}; with
--file, a list of those. Only Greenhouse forms are filled by a script in v1; every other system
gets guided filling by the host under the same rules (references/ATS.md).
"""
import argparse
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from boards import USER_AGENT  # noqa: E402

SUPPORTED_AUTOFILL = frozenset({'greenhouse'})
# Checked in order; a host matches a suffix exactly or as a subdomain.
HOSTS = (
    ('greenhouse', ('greenhouse.io', 'grnh.se')),
    ('lever', ('lever.co',)),
    ('ashby', ('ashbyhq.com',)),
    ('workday', ('myworkdayjobs.com', 'myworkdaysite.com')),
    ('smartrecruiters', ('smartrecruiters.com',)),
    ('icims', ('icims.com',)),
    ('keka', ('keka.com',)),
    ('darwinbox', ('darwinbox.in', 'darwinbox.com')),
    ('workable', ('workable.com',)),
    ('google_forms', ('forms.gle', 'forms.google.com')),
    ('linkedin', ('linkedin.com', 'lnkd.in')),
    ('naukri', ('naukri.com',)),
    ('iimjobs', ('iimjobs.com',)),
    ('internshala', ('internshala.com',)),
    ('indeed', ('indeed.com', 'indeed.co.uk', 'indeed.co.in', 'indeed.ca', 'indeed.com.au', 'indeed.de',
                'indeed.fr')),
)
FAMILIES = tuple(name for name, _ in HOSTS) + ('email', 'company_site')
SHORT_LINKS = ('grnh.se', 'lnkd.in', 'forms.gle', 'bit.ly', 't.co', 'tinyurl.com', 'ow.ly', 'buff.ly', 'rb.gy',
               'is.gd', 'cutt.ly', 'shorturl.at')
# Job-record keys read when no link is passed (job-intake records use application_url).
URL_KEYS = ('application_url', 'apply_url', 'url')
EMAIL_KEYS = ('application_email', 'apply_email', 'contact_email', 'email')
BOARD = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')
DIGITS = re.compile(r'^\d{1,20}$')
INDEED_JOB_KEY = re.compile(r'^[0-9a-f]{16}$')
UUID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
EMAIL = re.compile(r'^[^@\s<>:/]+@[^@\s<>:/]+\.[A-Za-z]{2,}$')
REDIRECTS = (301, 302, 303, 307, 308)


def _host_is(host, suffix):
    return host == suffix or host.endswith('.' + suffix)


def _with_scheme(text):
    text = (text or '').strip().strip('<>"\'').strip()
    if text and not re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://', text) and not text.lower().startswith('mailto:'):
        text = 'https://' + text
    return text


def _result(ats, board, job_id, apply_url, eu=False):
    # EU-hosted Greenhouse boards publish no verified question API yet: guided filling only.
    return {'ats': ats, 'board': board, 'job_id': job_id, 'apply_url': apply_url,
            'supported_autofill': ats in SUPPORTED_AUTOFILL and not eu}


def greenhouse_apply_url(board, job_id, eu=False):
    """The embeddable application form: it renders the form itself, even for boards that redirect
    their hosted job pages to a company careers site."""
    query = urllib.parse.urlencode({'for': board, 'token': job_id})
    return 'https://job-boards%s.greenhouse.io/embed/job_app?%s' % ('.eu' if eu else '', query)


def _greenhouse(parts, query):
    host = parts.hostname.lower()
    segments = [urllib.parse.unquote(s) for s in parts.path.split('/') if s]
    board = job = None
    eu = _host_is(host, 'eu.greenhouse.io')
    if _host_is(host, 'greenhouse.io'):
        label = host.split('.')[0]
        if label == 'boards-api' and segments[:2] == ['v1', 'boards'] and len(segments) >= 3:
            board = segments[2]
            if len(segments) >= 5 and segments[3] == 'jobs':
                job = segments[4]
        elif segments[:1] == ['embed']:
            board = (query.get('for') or [None])[0]
            job = (query.get('token') or [None])[0] if segments[1:2] == ['job_app'] else None
        elif label in ('boards', 'job-boards') and segments:
            board = segments[0]
            if len(segments) >= 3 and segments[1] == 'jobs':
                job = segments[2]
    if not job and query.get('gh_jid'):
        job = query['gh_jid'][0]
    board = board if board and BOARD.match(board) else None
    job = job if job and DIGITS.match(job) else None
    return board, job, eu


def classify(url, record=None):
    """{ats, board, job_id, apply_url, supported_autofill} for one link (offline).

    With no link, the record's application_url/apply_url/url is used; a record with only an
    email address is an email application. board/job_id are None when the link does not carry them.
    """
    record = record or {}
    if not url:
        url = next((record[k] for k in URL_KEYS if isinstance(record.get(k), str) and record[k].strip()), None)
    if not url:
        email = next((record[k].strip() for k in EMAIL_KEYS if isinstance(record.get(k), str) and record[k].strip()), '')
        if EMAIL.match(email):
            return _result('email', None, None, 'mailto:' + email)
        raise ValueError('Need an application link, or a job record with an application link or email')
    text = _with_scheme(url)
    if text.lower().startswith('mailto:'):
        return _result('email', None, None, text)
    if EMAIL.match(url.strip()):
        return _result('email', None, None, 'mailto:' + url.strip())
    parts = urllib.parse.urlsplit(text)
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
        raise ValueError('Not a web or mailto link')
    host = parts.hostname.lower()
    query = urllib.parse.parse_qs(parts.query)
    ats = next((name for name, suffixes in HOSTS if any(_host_is(host, s) for s in suffixes)), None)
    if ats is None and host == 'docs.google.com' and parts.path.startswith('/forms'):
        ats = 'google_forms'
    if ats is None and query.get('gh_jid'):
        ats = 'greenhouse'  # a company careers page embedding a Greenhouse form
    if ats is None and query.get('ashby_jid'):
        ats = 'ashby'
    if ats == 'greenhouse':
        board, job, eu = _greenhouse(parts, query)
        return _result(ats, board, job, greenhouse_apply_url(board, job, eu) if board and job else text, eu)
    segments = [urllib.parse.unquote(s) for s in parts.path.split('/') if s]
    if ats == 'lever' and host.split('.')[0] == 'jobs' and len(segments) >= 2 \
            and BOARD.match(segments[0]) and UUID.match(segments[1]):
        board, job = segments[0], segments[1].lower()
        return _result(ats, board, job, 'https://%s/%s/%s/apply' % (host, board, job))
    if ats == 'ashby':
        if host == 'jobs.ashbyhq.com' and len(segments) >= 2 and UUID.match(segments[1]) and len(segments[0]) <= 100:
            board, job = segments[0], segments[1].lower()
            return _result(ats, board, job, 'https://jobs.ashbyhq.com/%s/%s/application'
                           % (urllib.parse.quote(board), job))
        jid = (query.get('ashby_jid') or [''])[0]
        return _result(ats, None, jid.lower() if UUID.match(jid) else None, text)
    if ats == 'indeed':
        # jk (or vjk on a search page) is Indeed's job key; viewjob shows the full ad and its apply button
        key = next((v[0].strip().lower() for k, v in query.items()
                    if k.lower() in ('jk', 'vjk') and INDEED_JOB_KEY.match(v[0].strip().lower())), None)
        return _result(ats, None, key, 'https://%s/viewjob?jk=%s' % (host, key) if key else text)
    return _result(ats or 'company_site', None, None, text)


# ------------------------------------------------------------------ short links
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surface each hop so only HEAD requests are ever sent


def is_short_link(url):
    host = (urllib.parse.urlsplit(_with_scheme(url)).hostname or '').lower()
    return any(_host_is(host, s) for s in SHORT_LINKS)


def resolve(url, opener=None, hops=8, timeout=10):
    """Follow redirects with HEAD requests only. Returns (final_url, error or None)."""
    open_ = (opener or urllib.request.build_opener(_NoRedirect)).open
    current = _with_scheme(url)
    for _ in range(hops):
        request = urllib.request.Request(current, method='HEAD', headers={'User-Agent': USER_AGENT})
        try:
            open_(request, timeout=timeout).close()
            return current, None
        except urllib.error.HTTPError as error:
            location = error.headers.get('Location') if error.code in REDIRECTS and error.headers else None
            if not location:
                return current, None  # a final page that refuses HEAD is still the final page
            following = urllib.parse.urljoin(current, location)
            if urllib.parse.urlsplit(following).scheme not in ('http', 'https'):
                return current, 'redirects to a non-web link'
            current = following
        except (urllib.error.URLError, OSError, ValueError) as error:
            return current, 'could not reach the link (%s)' % getattr(error, 'reason', error)
    return current, 'too many redirects'


def describe(link, follow=False, opener=None):
    """CLI record for one link: the classification plus what --resolve did."""
    extra = {}
    target = link
    if follow and is_short_link(link):
        target, error = resolve(link, opener)
        if target != _with_scheme(link):
            extra['resolved_url'] = target
        if error:
            extra['resolve_error'] = error
    try:
        result = classify(target)
    except ValueError as error:
        return dict({'url': link, 'error': str(error)}, **extra)
    if not follow and is_short_link(link) and not result['job_id']:
        extra['note'] = 'short link: pass --resolve to follow it'
    return dict({'url': link}, **result, **extra)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--url', help='one application link (or mailto:)')
    source.add_argument('--file', type=Path, help='text file with one link per line')
    parser.add_argument('--resolve', action='store_true', help='follow short links with HEAD requests (network)')
    args = parser.parse_args(argv)
    if args.url:
        result = describe(args.url, args.resolve)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if 'error' in result else 0
    lines = args.file.read_bytes().decode('utf-8').splitlines()
    links = [line.strip() for line in lines if line.strip() and not line.strip().startswith('#')]
    print(json.dumps([describe(link, args.resolve) for link in links], ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
