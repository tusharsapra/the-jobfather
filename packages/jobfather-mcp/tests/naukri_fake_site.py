"""A loopback stand-in for Naukri used by the end-to-end tests (fictional content only).

Pages load their data with fetch() like the real site, so the server's passive JSON capture is
exercised. It has a fake login, protected pages, a fake Apply (with and without a questionnaire),
a company-site job, an already-applied job, an expired-job redirect and a 403 block page. It
records submitted applications so tests can check exactly what was sent.
"""
import copy
import html
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from naukri_support import fixture, fixture_job_id

PLAIN, CANCEL, QUESTIONS, EXTERNAL, BLOCKED, APPLIED, SPARE = (
    fixture_job_id(), '010203040506', '020304050607', '030405060708', '040506070809', '050607080912', '060708091011')
QUESTION_FORM = '''
<div role="dialog" id="qdlg"><p class="question-text">Please answer the employer's questions</p>
  <fieldset><legend>Are you willing to relocate?</legend>
    <input type="radio" name="reloc" id="reloc-yes" value="yes"><label for="reloc-yes">Yes</label>
    <input type="radio" name="reloc" id="reloc-no" value="no"><label for="reloc-no">No</label></fieldset>
  <label for="notice">Notice period in days</label><input type="number" id="notice" name="notice" min="0" max="180" required>
  <button type="button" id="qsubmit">Submit</button></div>'''
PROFILE_JSON = {'user': {'name': 'Demo Person', 'email': 'demo.person@example.com', 'mobile': '555-0142'},
                'noticePeriod': [], 'profile': [{
                    'cvInfo': {'fileName': 'demo_resume.pdf', 'uploadDate': '2026-10-02 10:00:00', 'isAvailable': True},
                    'lastModTime': '2026-10-18 09:30:00.0', 'noticePeriod': {'id': '1', 'value': '30 Days'},
                    'locationPrefId': ['1', '2']}]}
DASHBOARD_JSON = {'dashBoard': {'pc': 88, 'mvn': '555-0142', 'username': 'demo.person@example.com',
                                'name': 'Demo Person'}}
PAGE = '''<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head><body>
<header>{header}</header><main>{main}</main><script>{script}</script></body></html>'''


def _header(logged_in):
    if logged_in:
        return '<nav><a href="/mnjuser/profile">View profile</a> <a href="/logout">Logout</a></nav>'
    return '<nav><a href="https://login.naukri.com/nLogin/Login.php">Login</a> ' \
           '<a href="/registration/createAccount">Register</a></nav>'


class FakeNaukri:
    def __init__(self):
        responses, _, _ = fixture('search')
        self.search_body = responses[0]['body']
        jobs, _, _ = fixture('job')
        self.job_body = next(r['body'] for r in jobs if '/jobapi/v4/job/' in r['url'])
        self.similar_body = next(r['body'] for r in jobs if '/simjobs/' in r['url'])
        self.applications = []
        self.lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        self.base = 'http://127.0.0.1:%d' % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    # ------------------------------------------------------------------ data
    def job_json(self, job_id, logged_in):
        body = copy.deepcopy(self.job_body)
        detail = body['jobDetails']
        detail['jobId'] = job_id
        detail['staticUrl'] = '%s/job-listings-fictional-role-%s' % (self.base, job_id)
        detail['title'] = 'Fictional Role %s' % job_id[-4:]
        detail.pop('applyRedirectUrl', None)
        if job_id == EXTERNAL:
            detail['applyRedirectUrl'] = 'https://careers.example.com/apply/123'
        body['loggedIn'] = logged_in
        return body

    # ------------------------------------------------------------------ http
    def _handler(self):
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def logged_in(self):
                return 'fake_auth=1' in (self.headers.get('Cookie') or '')

            def send(self, status, body, ctype='text/html; charset=utf-8', headers=()):
                data = body.encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                for key, value in headers:
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def redirect(self, where, headers=()):
                self.send_response(302)
                self.send_header('Location', where)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def page(self, title, main, script=''):
                self.send(200, PAGE.format(title=html.escape(title), header=_header(self.logged_in()), main=main,
                                           script=script))

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                payload = self.rfile.read(length).decode('utf-8') if length else ''
                if self.path.startswith('/jobapi/v2/search/recom-jobs'):
                    jobs = site.search_body['jobDetails'][:5]
                    return self.send(200, json.dumps({'noOfJobs': len(jobs), 'jobDetails': jobs, 'isLoggedIn': True}),
                                     'application/json')
                if self.path == '/fake-login':
                    self.redirect('/mnjuser/homepage', headers=[('Set-Cookie', 'fake_auth=1; Path=/')])
                elif self.path == '/fake-apply':
                    if not self.logged_in():
                        return self.send(401, '{}', 'application/json')
                    with site.lock:
                        site.applications.append(json.loads(payload or '{}'))
                    self.send(200, '{"ok": true}', 'application/json')
                else:
                    self.send(404, 'not found')

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                path, query = parts.path, urllib.parse.parse_qs(parts.query)
                if path == '/jobapi/v3/search':
                    body = copy.deepcopy(site.search_body)
                    body['isLoggedIn'] = self.logged_in()
                    return self.send(200, json.dumps(body), 'application/json')
                match = re.fullmatch(r'/jobapi/v4/job/(\d+)', path)
                if match:
                    return self.send(200, json.dumps(site.job_json(match.group(1), self.logged_in())), 'application/json')
                if re.fullmatch(r'/jobapi/v2/search/simjobs/\d+', path):
                    return self.send(200, json.dumps(site.similar_body), 'application/json')
                if path == '/jobapi/v3/user/savedJobs/detail':
                    return self.send(200, json.dumps({'totaljobs': 0, 'list': []}), 'application/json')
                if path == '/cloudgateway-mynaukri/resman-aggregator-services/v2/users/self':
                    return self.send(200, json.dumps(PROFILE_JSON), 'application/json')
                if path == '/cloudgateway-mynaukri/resman-aggregator-services/v0/users/self/dashboard':
                    return self.send(200, json.dumps(DASHBOARD_JSON), 'application/json')
                if path == '/nLogin/Login.php':
                    return self.page('Login', '<form method="post" action="/fake-login"><label>Email ID '
                                     '<input type="text" name="u"></label><label>Password <input type="password" '
                                     'name="p"></label><button type="submit">Sign in (test)</button></form>')
                if path.startswith('/mnjuser/'):
                    if not self.logged_in():
                        return self.redirect('/nLogin/Login.php')
                    if path == '/mnjuser/homepage':
                        return self.page('Home', '<h1>Welcome back</h1>')
                    if path == '/mnjuser/profile':
                        return self.page('Profile', '<h1>Demo Person</h1><p>demo.person@example.com</p>'
                                         '<p>555-0142</p><p>Preferred work location</p><p>Northvale, Eastport</p>',
                                         "fetch('/cloudgateway-mynaukri/resman-aggregator-services/v2/users/self');"
                                         "fetch('/cloudgateway-mynaukri/resman-aggregator-services/v0/users/self/"
                                         "dashboard');")
                    if path == '/mnjuser/recommendedjobs':
                        return self.page('Recommended', '<div id="list"></div>',
                                         "fetch('/jobapi/v2/search/recom-jobs', {method: 'POST', body: '{}'})"
                                         ".then(r => r.json()).then(d => {document.getElementById('list').textContent"
                                         " = d.jobDetails.length})")
                    if path == '/mnjuser/savedjobs':
                        return self.page('Saved', '<p>No saved jobs!</p>',
                                         "fetch('/jobapi/v3/user/savedJobs/detail');")
                if path == '/jobs-in-somewhere':
                    return self.page('Jobs', '<p>Jobs</p>')
                match = re.fullmatch(r'/job-listings-(?:[^/]*-)?(\d{6,20})', path)
                if match:
                    return self.job_page(match.group(1))
                match = re.fullmatch(r'/([a-z0-9-]+)-jobs(?:-in-[a-z0-9-]+)?(?:-(\d+))?', path)
                if match:
                    return self.search_page(query)
                return self.send(404, '<title>Not found</title>')

            def search_page(self, query):
                script = ("const q = new URLSearchParams(location.search); const page = (location.pathname.match(/-(\\d+)$/) "
                          "|| [0, 1])[1]; fetch('/jobapi/v3/search?noOfResults=20&pageNo=' + page + '&keyword=' + "
                          "encodeURIComponent(q.get('k') || '')).then(r => r.json()).then(d => { "
                          "document.getElementById('list').innerHTML = d.jobDetails.map(j => "
                          "'<div class=\"srp-jobtuple-wrapper\" data-job-id=\"' + j.jobId + '\"><a class=\"title\" "
                          "href=\"' + j.jdURL + '\">' + j.title + '</a></div>').join(''); });")
                self.page('Search', '<div id="list"></div>', script)

            def job_page(self, job_id):
                if job_id == BLOCKED:
                    return self.send(403, '<html><head><title>Access Denied</title></head><body><h1>Access Denied'
                                     '</h1><p>You don\'t have permission to access this server.</p></body></html>')
                if job_id not in (PLAIN, CANCEL, QUESTIONS, EXTERNAL, APPLIED, SPARE):
                    return self.redirect('/jobs-in-somewhere?expJD=true')
                if not self.logged_in():
                    button = '<button id="apply-button">Apply</button>'
                elif job_id == APPLIED:
                    button = '<span id="already-applied">Applied</span>'
                elif job_id == EXTERNAL:
                    button = '<button id="company-site-button">Apply on company site</button>'
                else:
                    button = '<button id="apply-button">Apply</button>'
                questions = 'true' if job_id == QUESTIONS else 'false'
                script = '''
fetch('/jobapi/v4/job/%(id)s?microsite=y').then(r => r.json()).then(d => {
  document.querySelector('h1').textContent = d.jobDetails.title; });
fetch('/jobapi/v2/search/simjobs/%(id)s?noOfResults=6').then(r => r.json());
const ask = %(questions)s;
function done(answers) {
  fetch('/fake-apply', {method: 'POST', body: JSON.stringify({job_id: '%(id)s', answers: answers})}).then(r => {
    const note = document.createElement('div');
    note.className = 'apply-message';
    note.textContent = r.ok ? 'You have successfully applied to this job.' : 'Please login to apply';
    document.body.appendChild(note);
    const box = document.getElementById('qdlg'); if (box) box.remove();
  });
}
const button = document.getElementById('apply-button');
if (button) button.addEventListener('click', () => {
  if (!ask) return done({});
  document.getElementById('slot').innerHTML = %(form)s;
  document.getElementById('qsubmit').addEventListener('click', () => {
    const picked = document.querySelector('input[name=reloc]:checked');
    done({reloc: picked ? picked.value : null, notice: document.getElementById('notice').value});
  });
});''' % {'id': job_id, 'questions': questions, 'form': json.dumps(QUESTION_FORM)}
                main = ('<section id="job_header"><h1>Loading</h1>%s</section><section class="job-desc-container">'
                        '<p>Fictional description.</p></section><div id="slot"></div>' % button)
                self.page('Fictional Role', main, script)

        return Handler
