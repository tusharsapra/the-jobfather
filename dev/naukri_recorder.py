#!/usr/bin/env python3
"""Record how Naukri pages look, passively, for The Jobfather's clean-room Naukri server.

    python dev/naukri_recorder.py search --keywords "data analyst" [--location "Example City"] [--experience 3]
    python dev/naukri_recorder.py job --job 123456789012            (or a https://www.naukri.com URL)
    python dev/naukri_recorder.py similar --job URL
    python dev/naukri_recorder.py recommended | saved | profile     (needs a hand login first)
    python dev/naukri_recorder.py login-markers [--wait-login 900]  (you log in by hand in the window)
    python dev/naukri_recorder.py watch [--url URL]                 (you browse; Enter = snapshot, q = quit)

Real Google Chrome (``channel="chrome"``) with a developer profile at
``~/.cache/jobfather-dev/naukri-profile``; no stealth flags, no cookies or request headers read.
It records, per step: XHR/fetch JSON responses from Naukri hosts (method, URL, status, body),
ARIA snapshots of the main regions, trimmed HTML and a screenshot, into
``~/.cache/jobfather-dev/captures/<timestamp>-<scenario>/`` (outside the repository). It never
clicks Apply or submits anything; ``watch`` only records while you act by hand (for example while
you apply to a job you actually want). Page loads are spaced (at least 10 s, searches 20 s) and
capped per day; any CAPTCHA, verification or block page stops the run.

Turn captures into fixtures with ``dev/sanitize_fixtures.py``. Needs ``playwright==1.64.0``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'packages' / 'jobfather-mcp' / 'src'))

from jobfather_mcp.naukri import site as site_mod  # noqa: E402

DEV_HOME = Path('~/.cache/jobfather-dev').expanduser()
PROFILE = DEV_HOME / 'naukri-profile'
CAPTURES = DEV_HOME / 'captures'
LEDGER = DEV_HOME / 'recorder-ledger.jsonl'
SPACING_S = {'page': 10, 'search': 20, 'job': 10, 'similar': 20, 'recommended': 20, 'saved': 20,
             'profile': 20, 'login-markers': 10, 'watch': 10}
MAX_BODY = 3_000_000
REGIONS = ('main', '[role="main"]', 'header', 'nav', '[role="dialog"]', '#root', 'body')
SCRIPT = re.compile(r'<script\b(?![^>]*application/ld\+json)[^>]*>.*?</script>', re.S | re.I)
LDJSON = re.compile(r'<script\b[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
STATE_VAR = re.compile(r'(window\.[A-Za-z_$][\w$]*|var\s+[A-Za-z_$][\w$]*)\s*=\s*[\[{]')


def log(message):
    print('recorder: ' + message, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ pacing
def _ledger():
    if not LEDGER.is_file():
        return []
    out = []
    for line in LEDGER.read_text(encoding='utf-8').splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def pace(scenario, max_per_day):
    """Sleep until the spacing allows another page load; refuse when today's budget is used."""
    entries = _ledger()
    today = datetime.now().astimezone().date().isoformat()
    if sum(1 for e in entries if e.get('day') == today) >= max_per_day:
        raise SystemExit('recorder: daily page-load budget (%d) used; try tomorrow' % max_per_day)
    now = time.time()
    last_any = max((e['t'] for e in entries), default=0)
    last_same = max((e['t'] for e in entries if e.get('scenario') == scenario), default=0)
    wait = max(last_any + SPACING_S['page'], last_same + SPACING_S.get(scenario, 10)) - now
    if wait > 0:
        log('waiting %.0f s between page loads' % wait)
        time.sleep(wait)
    DEV_HOME.mkdir(parents=True, exist_ok=True)
    with LEDGER.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps({'t': time.time(), 'day': today, 'scenario': scenario}) + '\n')


# ------------------------------------------------------------------ capture
def trim_html(html):
    html = SCRIPT.sub('', html)
    for tag in ('style', 'svg', 'noscript', 'template'):
        html = re.sub(r'<%s\b[^>]*>.*?</%s>' % (tag, tag), '', html, flags=re.S | re.I)
    html = re.sub(r'<!--.*?-->', '', html, flags=re.S)
    html = re.sub(r'\sstyle="[^"]*"', '', html)
    html = re.sub(r'<(?:link|meta)\b[^>]*>', '', html, flags=re.I)
    return html[:2_500_000]


def strip_values(snapshot):
    """Remove typed values from textboxes in an ARIA snapshot (login pages)."""
    return re.sub(r'^(\s*- (?:textbox|searchbox|combobox)\b[^:\n]*):.*$', r'\1', snapshot, flags=re.M)


class Recorder:
    def __init__(self, page, out, site):
        self.page = page
        self.out = out
        self.site = site
        self.responses = []
        self.step = 0
        page.on('response', self._on_response)

    def _on_response(self, response):
        try:
            if response.request.resource_type not in ('xhr', 'fetch'):
                return
            host = re.sub(r':\d+$', '', response.url.split('/')[2]) if '://' in response.url else ''
            if not (host.endswith('naukri.com') or self.site.test_base):
                return
            self.responses.append(response)
        except Exception:
            pass

    def drain(self, folder):
        records = []
        for response in self.responses:
            try:
                ctype = response.header_value('content-type') or ''
                if 'json' not in ctype:
                    continue
                body = response.body()
                if len(body) > MAX_BODY:
                    records.append({'method': response.request.method, 'url': response.url,
                                    'status': response.status, 'skipped': 'body too large'})
                    continue
                records.append({'method': response.request.method, 'url': response.url, 'status': response.status,
                                'resource_type': response.request.resource_type,
                                'body': json.loads(body.decode('utf-8', 'replace'))})
            except Exception as error:  # bodies of redirected or evicted responses
                records.append({'method': response.request.method, 'url': response.url,
                                'status': getattr(response, 'status', None), 'skipped': type(error).__name__})
        self.responses = []
        (folder / 'responses.json').write_text(json.dumps(records, indent=1, ensure_ascii=False), encoding='utf-8')
        return records

    def snapshot(self, name, status=None, login_page=False, screenshot=True):
        if self.site.is_login_url(self.page.url):
            login_page = True  # never keep HTML, screenshots or typed values of a login page
        self.step += 1
        folder = self.out / ('%02d-%s' % (self.step, name))
        folder.mkdir(parents=True, exist_ok=True)
        page = self.page
        try:
            title = page.title()
        except Exception:
            title = ''
        try:
            text = page.inner_text('body', timeout=5000)
        except Exception:
            text = ''
        visible = []
        for rule_id, selector in self.site.checkpoint_rules.element_selectors():
            try:
                if page.locator(selector).filter(visible=True).count():
                    visible.append(rule_id)
            except Exception:
                continue
        found = self.site.checkpoint_rules.detect(url=page.url, status=status, title=title, text=text,
                                                  visible=visible, in_login_flow=login_page)
        meta = {'step': name, 'url': page.url, 'status': status, 'title': title,
                'captured_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'site_profile': self.site.version, 'visible_markers': visible,
                'checkpoint': {'kind': found.kind, 'rule': found.rule} if found else None}
        self.drain(folder)
        for region in REGIONS:
            try:
                locator = page.locator(region).first
                if not locator.count():
                    continue
                snap = locator.aria_snapshot(timeout=10000)
            except Exception:
                continue
            safe = re.sub(r'[^a-z0-9]+', '_', region.lower()).strip('_')
            (folder / ('aria-%s.yml' % safe)).write_text(strip_values(snap) if login_page else snap, encoding='utf-8')
        if not login_page:
            try:
                html = page.content()
                (folder / 'page.html').write_text(trim_html(html), encoding='utf-8')
                blocks = []
                for raw in LDJSON.findall(html):
                    try:
                        blocks.append(json.loads(raw))
                    except ValueError:
                        continue
                (folder / 'ldjson.json').write_text(json.dumps(blocks, indent=1, ensure_ascii=False), encoding='utf-8')
                meta['inline_state_vars'] = sorted(set(STATE_VAR.findall(html)))[:50]
            except Exception as error:
                meta['html_error'] = type(error).__name__
            if screenshot:
                try:
                    page.screenshot(path=str(folder / 'screenshot.png'), full_page=True, timeout=20000)
                except Exception:
                    pass
        (folder / 'meta.json').write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding='utf-8')
        log('saved %s (%s)' % (folder, page.url))
        if found:
            log('STOP: checkpoint detected (%s, rule %s); nothing else will be loaded' % (found.kind, found.rule))
        return found

    def step_rename(self, name):
        """Rename the last step folder (e.g. once we know the protected page showed the login form)."""
        folder = self.out / ('%02d-%s' % (self.step, 'protected'))
        if folder.is_dir():
            folder.rename(self.out / ('%02d-%s' % (self.step, name)))

    def load(self, url, name, scenario, max_per_day, login_page=False, scroll=True):
        if not self.site.allowed(url):
            raise SystemExit('recorder: refusing a non-Naukri URL: %s' % url)
        pace(scenario, max_per_day)
        log('opening %s' % url)
        response = self.page.goto(url, wait_until='domcontentloaded', timeout=45000)
        status = response.status if response else None
        try:
            self.page.wait_for_load_state('networkidle', timeout=15000)
        except Exception:
            pass
        if scroll and not login_page:
            for _ in range(8):  # gentle scrolling lets lazily loaded sections (similar jobs) appear
                self.page.mouse.wheel(0, 900)
                self.page.wait_for_timeout(700)
            try:
                self.page.wait_for_load_state('networkidle', timeout=8000)
            except Exception:
                pass
        return self.snapshot(name, status=status, login_page=login_page)


# ------------------------------------------------------------------ scenarios
def run(args):
    from playwright.sync_api import sync_playwright

    site = site_mod.site()
    stamp = datetime.now().strftime('%Y%m%dT%H%M%S')
    out = Path(args.out).expanduser() if args.out else CAPTURES / ('%s-%s' % (stamp, args.scenario))
    out.mkdir(parents=True, exist_ok=True)
    profile = Path(args.profile).expanduser()
    profile.mkdir(parents=True, exist_ok=True)
    (out / 'run.json').write_text(json.dumps({'scenario': args.scenario, 'started_at': stamp,
                                              'site_profile': site.version}, indent=1), encoding='utf-8')
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(str(profile), channel='chrome', headless=False,
                                                        no_viewport=True, accept_downloads=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            rec = Recorder(page, out, site)
            found = scenario(args, site, rec)
            if found and sys.stdin.isatty():
                input('recorder: a checkpoint is open in the window; press Enter to close the browser ')
        finally:
            context.close()
    log('captures in %s' % out)


def wait_for_login(page, site, seconds):
    """Poll the open tab (no navigation, no field reads) until it leaves the login page."""
    deadline = time.time() + seconds
    log('waiting up to %d s for a hand login in the Chrome window' % seconds)
    while time.time() < deadline:
        try:
            url = page.url
        except Exception:
            return False
        if url and site.allowed(url) and not site.is_login_url(url) and not url.startswith('about:'):
            page.wait_for_timeout(3000)  # let the post-login redirect finish
            return True
        page.wait_for_timeout(2000)
    return False


def _protected_redirect(site, rec):
    return site.is_login_url(rec.page.url)


def scenario(args, site, rec):
    cap = args.max_loads_per_day
    name = args.scenario
    if name == 'search':
        if args.url:
            url = args.url
        else:
            if not args.keywords:
                raise SystemExit('recorder: search needs --keywords or --url')
            url, warnings = site.search_url(args.keywords, args.location, args.experience, args.posted_within,
                                            args.work_mode or (), args.min_salary, args.sort, args.page)
            for warning in warnings:
                log('note: ' + warning)
        return rec.load(url, 'search', 'search', cap)
    if name in ('job', 'similar'):
        if not (args.job or args.url):
            raise SystemExit('recorder: %s needs --job' % name)
        url = args.url or site.job_ref(args.job).url
        return rec.load(url, name, name, cap)
    if name in ('recommended', 'saved', 'profile'):
        url = args.url or site.url(site.section(name)['url'])
        found = rec.load(url, name, name, cap)
        if not found and _protected_redirect(site, rec):
            log('redirected to the login page: run login-markers first and log in by hand')
        return found
    if name == 'login-markers':
        login = site.section('login')
        protected = site.url(login['protected_url'])
        found = rec.load(protected, 'protected', name, cap, scroll=False)
        if found or not site.is_login_url(rec.page.url):
            return found  # already signed in: the 'protected' step shows the logged-in markers
        rec.step_rename('protected-logged-out')
        if args.wait_login:
            if not wait_for_login(rec.page, site, args.wait_login):
                log('no sign-in within %d s; stopping' % args.wait_login)
                return None
        elif sys.stdin.isatty():
            input('recorder: log in by hand in the Chrome window (any method), then press Enter here ')
        else:
            log('logged out; pass --wait-login SECONDS to wait for a hand login')
            return None
        return rec.load(protected, 'protected-logged-in', name, cap, scroll=False)
    if name == 'watch':
        url = args.url or site.base_url + '/'
        found = rec.load(url, 'watch-start', name, cap)
        if found or not sys.stdin.isatty():
            return found
        while True:
            answer = input('recorder: act by hand in the window; Enter = snapshot, q = quit ').strip().lower()
            if answer == 'q':
                return None
            found = rec.snapshot('watch')
            if found:
                return found
    raise SystemExit('recorder: unknown scenario %s' % name)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('scenario', choices=('search', 'job', 'recommended', 'saved', 'similar', 'profile',
                                         'login-markers', 'watch'))
    ap.add_argument('--keywords')
    ap.add_argument('--location')
    ap.add_argument('--experience', type=int)
    ap.add_argument('--posted-within', type=int, choices=(1, 3, 7, 15, 30))
    ap.add_argument('--work-mode', action='append', choices=('office', 'hybrid', 'remote'))
    ap.add_argument('--min-salary', type=float)
    ap.add_argument('--sort', choices=('relevance', 'date'), default='relevance')
    ap.add_argument('--page', type=int, default=1)
    ap.add_argument('--job', help='job id or https://www.naukri.com job URL')
    ap.add_argument('--url', help='a https://www.naukri.com URL to open instead of the built one')
    ap.add_argument('--profile', default=str(PROFILE), help='developer Chrome profile folder')
    ap.add_argument('--out', help='output folder (default: a new folder under ~/.cache/jobfather-dev/captures)')
    ap.add_argument('--max-loads-per-day', type=int, default=20)
    ap.add_argument('--wait-login', type=int, default=0, metavar='SECONDS',
                    help='login-markers: wait this long for a hand login instead of asking on the terminal')
    args = ap.parse_args(argv)
    run(args)
    return 0


if __name__ == '__main__':
    sys.exit(main())
