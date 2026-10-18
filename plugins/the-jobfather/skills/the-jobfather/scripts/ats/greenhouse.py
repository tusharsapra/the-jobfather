# /// script
# requires-python = ">=3.10"
# dependencies = ["playwright==1.64.0"]
# ///
"""Fill a Greenhouse application form from an approved answer plan; the user clicks Submit.

    greenhouse.py fetch --url URL [--board TOKEN]                 the job's questions (no workspace needed)
    greenhouse.py plan-template --url URL --application-id APP-…  answer-plan skeleton, prefilled from the profile
    greenhouse.py validate --plan PLAN                            check a plan without a browser
    greenhouse.py preview --plan PLAN [--profile ats]             fill, read back, screenshot; never submits
    greenhouse.py submit --plan PLAN --approval APV-ID            refill; the user reviews and clicks Submit

Stdout carries field names and statuses only, never answer values. Exit codes: 0 ok, 2 needs
input or stopped, 3 approval invalid, 4 job closed, 5 timed out waiting for the user, 1 error.
fetch, plan-template and validate use the standard library only. preview and submit need
Playwright and Google Chrome: `uv run` installs Playwright from the header above. There is no
mode that clicks Submit: the user always does. See references/ATS.md.
"""
import argparse
import contextlib
from datetime import date, datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import approvals  # noqa: E402
import layout  # noqa: E402
from applications import Store  # noqa: E402
from ats.classify import BOARD, classify, greenhouse_apply_url  # noqa: E402
from boards import USER_AGENT, html_text  # noqa: E402
from local import (add_workspace_arg, atomic_json, contained, digest, now, read_json, read_text,  # noqa: E402
                   require_workspace, sha_json, sha_text, shared_lock, tz)

PLAN_SCHEMA = 'the-jobfather/ats-answer-plan@1'
QUESTIONS_SCHEMA = 'the-jobfather/ats-questions@1'
REPORT_SCHEMA = 'the-jobfather/ats-fill-report@1'
STATUS_SCHEMA = 'the-jobfather/ats-status@1'
RECEIPT_SCHEMA = 'the-jobfather/ats-receipt@1'
SUBMISSION_SCHEMA = 'the-jobfather/submission@1'
API_ROOT = 'https://boards-api.greenhouse.io'
EXIT_OK, EXIT_ERROR, EXIT_STOP, EXIT_APPROVAL, EXIT_CLOSED, EXIT_TIMEOUT = 0, 1, 2, 3, 4, 5

TYPES = ('input_text', 'input_file', 'input_hidden', 'textarea', 'multi_value_single_select',
         'multi_value_multi_select')
SELECTS = ('multi_value_single_select', 'multi_value_multi_select')
GROUPS = ('main', 'location', 'compliance', 'demographic')
KINDS = ('identity', 'contact', 'resume', 'cover_letter', 'links', 'work_auth', 'sponsorship', 'relocation',
         'salary', 'notice', 'consent', 'eeo', 'demographic', 'custom')
SENSITIVE = ('work_auth', 'sponsorship', 'relocation', 'salary', 'notice')
SELF_ID = ('eeo', 'demographic')
TEXT_LIMITS = {'input_text': 255, 'textarea': 20000}
FILE_FIELDS = ('resume', 'cover_letter')
MIN_GAP_SECONDS = 60
DEFAULT_DAILY_CAP = 15
WAIT_SECONDS = 15 * 60
HEARTBEAT_SECONDS = 10
LEDGER = layout.STATE + '/ats/ledger.jsonl'
LEDGER_LOCK = layout.LOCKS + '/ats-ledger.lock'
SUBMIT_LOCK = layout.LOCKS + '/ats-submit.lock'
BROWSER_PROFILES = {'ats': layout.STATE + '/browser/ats'}
COUNTED = ('submitted', 'unconfirmed')  # toward the daily cap: an unconfirmed run may have been submitted
APP_ID = re.compile(r'^APP-[0-9a-f]{12}$')

# Field names Greenhouse uses for its standard questions.
FIELD_KINDS = {'first_name': 'identity', 'last_name': 'identity', 'preferred_name': 'identity', 'email': 'contact',
               'phone': 'contact', 'country': 'contact', 'location': 'contact', 'resume': 'resume',
               'resume_text': 'resume', 'cover_letter': 'cover_letter', 'cover_letter_text': 'cover_letter',
               'gender': 'eeo', 'race': 'eeo', 'hispanic_ethnicity': 'eeo', 'veteran_status': 'eeo',
               'disability_status': 'eeo'}
TEXT_ALTERNATIVES = {'resume_text': 'resume', 'cover_letter_text': 'cover_letter'}  # "enter manually" boxes
DOM_IDS = {'location': 'candidate-location'}

LABEL_KINDS = (  # first match wins; consent also reads the description and options, the rest the label only
    ('eeo', re.compile(r'\b(?:gender|sex|race|racial|ethnicity|ethnic origin|hispanic|latin[oaxe]|veteran|'
                       r'disabilit(?:y|ies)|disabled)\b', re.I)),
    ('demographic', re.compile(r'\b(?:sexual orientation|transgender|lgbt\w*|pronouns?|neurodiver\w*|caste|'
                               r'religio\w*|first[- ]generation|socio-?economic|age range|marital status)\b', re.I)),
    ('consent', re.compile(r'\b(?:consent\w*|privacy|acknowledg\w*|i agree|agree to|attest\w*|terms (?:and|&) '
                           r'conditions|opt[- ]?in|gdpr|data protection|retain my|data retention|certify|'
                           r'declaration)\b', re.I)),
    ('sponsorship', re.compile(r'\bsponsor', re.I)),
    ('work_auth', re.compile(r'authori[sz]ed to work|authori[sz]ation to work|work authori[sz]ation|right to work|'
                             r'eligib\w* to work|legally (?:able|allowed|entitled|permitted|authori[sz]ed)|'
                             r'work permit|\bvisa\b|citizenship|permanent resident', re.I)),
    ('relocation', re.compile(r'\breloca', re.I)),
    ('salary', re.compile(r'salary|compensation|pay expectation|expected pay|desired pay|\bctc\b|remuneration|'
                          r'\bwages?\b', re.I)),
    ('notice', re.compile(r'notice period|start date|when can you (?:start|join)|available to start|'
                          r'availability to start|how soon|earliest (?:possible )?(?:start|joining|date)|'
                          r'joining date|date of joining', re.I)),
    ('links', re.compile(r'linkedin|github|portfolio|website|personal site|\bblog\b|\burl\b|twitter|behance|'
                         r'dribbble', re.I)),
    ('identity', re.compile(r'\b(?:first|last|full|legal|preferred|given|family) name\b|\bsurname\b|'
                            r"name you.{0,3}d prefer", re.I)),
    ('contact', re.compile(r"\be-?mail\b|\bphone\b|\bmobile\b|\baddress\b|\bcity\b|postal|zip code|"
                           r'country of residence|where (?:are you|do you) (?:currently )?(?:located|based|live|'
                           r'reside)|current location|currently (?:reside|located|based)|\breside\b', re.I)),
)
# Mapping guards for answers that state legal or personal facts.
AUTH_ASK = re.compile(r'\b(?:authori[sz]ed|eligible|entitled|permitted|allowed|able)\s+to\s+(?:legally\s+)?work\b|'
                      r'\bright to work\b|\bwork authori[sz]ation\b', re.I)
NEEDS_SPONSOR = re.compile(r'\b(?:require|need)s?\b.{0,80}?\bsponsor', re.I)
INVERTED = re.compile(r"\bwithout\b|\bnot\b|n['’]t\b|\bunable\b|\bno longer\b", re.I)
NEGATIVE = re.compile(r'\b(?:not|no|none|without|requires?|required|need|needs|needed|pending|expired|expiring|'
                      r'ineligible|unauthori[sz]ed)\b', re.I)
WILL_RELOCATE = re.compile(r'\b(?:willing|open|able|prepared|happy)\b.{0,40}\breloca|\bconsider reloca', re.I)
COMPOUND = re.compile(r'\b(?:based|located|live|living|reside|resident|currently|already)\b', re.I)
DECLINE = re.compile(r"^(?:i\s+)?(?:decline|prefer not|(?:would )?rather not|choose not|do not wish|don['’]?t wish|"
                     r"do not want|don['’]?t want|not (?:to )?(?:disclose|answer|say)|wish not)")
LOCATION_LABELS = {'location', 'current location', 'location (city)', 'city', 'current city', 'where are you located',
                   'where are you currently located', 'where are you based', 'where are you currently based'}
CLOSED = re.compile(r'no longer accepting|no longer open|job (?:posting )?(?:is )?closed|position has been filled|'
                    r'job (?:has )?expired|no longer available|not currently accepting|page not found', re.I)
CONFIRM = re.compile(r"thank you for applying|thanks for applying|application (?:has been )?(?:submitted|received)|"
                     r"we(?:'ve|’ve| have) received your application", re.I)

# ISO 3166 code -> (name as Greenhouse's country lists show it, other words naming the country)
COUNTRIES = {
    'AE': ('United Arab Emirates', ('emirates', 'emirati')), 'AR': ('Argentina', ()), 'AT': ('Austria', ('austrian',)),
    'AU': ('Australia', ('australian',)), 'BD': ('Bangladesh', ()), 'BE': ('Belgium', ('belgian',)),
    'BG': ('Bulgaria', ()), 'BR': ('Brazil', ('brazilian',)), 'CA': ('Canada', ('canadian',)),
    'CH': ('Switzerland', ('swiss',)), 'CL': ('Chile', ()), 'CN': ('China', ('chinese',)), 'CO': ('Colombia', ()),
    'CR': ('Costa Rica', ()), 'CZ': ('Czechia', ('czech republic',)), 'DE': ('Germany', ('german',)),
    'DK': ('Denmark', ('danish',)), 'EE': ('Estonia', ()), 'EG': ('Egypt', ()), 'ES': ('Spain', ('spanish',)),
    'FI': ('Finland', ('finnish',)), 'FR': ('France', ('french',)), 'GB': ('United Kingdom', ('britain', 'british',
                                                                                             'england', 'scotland',
                                                                                             'wales')),
    'GH': ('Ghana', ()), 'GR': ('Greece', ('greek',)), 'HK': ('Hong Kong SAR China', ('hong kong',)),
    'HR': ('Croatia', ()), 'HU': ('Hungary', ()), 'ID': ('Indonesia', ()), 'IE': ('Ireland', ('irish',)),
    'IL': ('Israel', ('israeli',)), 'IN': ('India', ('indian',)), 'IS': ('Iceland', ()), 'IT': ('Italy', ('italian',)),
    'JP': ('Japan', ('japanese',)), 'KE': ('Kenya', ()), 'KR': ('South Korea', ('korea', 'korean')),
    'LK': ('Sri Lanka', ()), 'LT': ('Lithuania', ()), 'LU': ('Luxembourg', ()), 'LV': ('Latvia', ()),
    'MA': ('Morocco', ()), 'MX': ('Mexico', ('mexican',)), 'MY': ('Malaysia', ()), 'NG': ('Nigeria', ()),
    'NL': ('Netherlands', ('dutch', 'holland')), 'NO': ('Norway', ('norwegian',)), 'NP': ('Nepal', ()),
    'NZ': ('New Zealand', ()), 'PE': ('Peru', ()), 'PH': ('Philippines', ('filipino',)), 'PK': ('Pakistan', ()),
    'PL': ('Poland', ('polish',)), 'PT': ('Portugal', ('portuguese',)), 'QA': ('Qatar', ()), 'RO': ('Romania', ()),
    'RS': ('Serbia', ()), 'SA': ('Saudi Arabia', ()), 'SE': ('Sweden', ('swedish',)), 'SG': ('Singapore', ()),
    'SI': ('Slovenia', ()), 'SK': ('Slovakia', ()), 'TH': ('Thailand', ()), 'TR': ('Turkey', ('türkiye',)),
    'TW': ('Taiwan', ()), 'UA': ('Ukraine', ()), 'US': ('United States', ('united states of america', 'america',
                                                                          'american')),
    'UY': ('Uruguay', ()), 'VN': ('Vietnam', ()), 'ZA': ('South Africa', ()),
}
ABBREVIATIONS = {'US': r'(?<![A-Za-z])U\.?S\.?(?:A\.?)?(?![A-Za-z])', 'GB': r'(?<![A-Za-z])U\.?K\.?(?![A-Za-z])',
                 'AE': r'(?<![A-Za-z])UAE(?![A-Za-z])', 'EU': r'(?<![A-Za-z])(?:EU|EEA)(?![A-Za-z])|european union'}


class Stop(Exception):
    """A deliberate stop with a status and exit code. Messages never carry answer values."""

    def __init__(self, status, message, code=EXIT_STOP, fields=None, **extra):
        super().__init__(message)
        self.status, self.message, self.code, self.fields, self.extra = status, message, code, list(fields or []), extra

    def as_dict(self):
        out = {'status': self.status, 'message': self.message}
        if self.fields:
            out['fields'] = self.fields
        out.update(self.extra)
        return out


# ------------------------------------------------------------------ text helpers
def option_key(text):
    """How option labels compare: Unicode-normalised, casefolded, whitespace collapsed. Nothing else."""
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', str(text or ''))).strip().casefold()


def question_key(text):
    """How question texts compare when looking up saved answers (looser: punctuation and quotes)."""
    text = option_key(text).replace('’', "'").replace('‘', "'").replace('“', '"').replace('”', '"')
    return re.sub(r'[\s*?.:!]+$', '', text)


def same_option(option, value, dial_code=False):
    a, b = option_key(option), option_key(value)
    if a == b and a:
        return True
    # The phone "Country" list shows "Canada +1"; the country name must still match exactly.
    return bool(dial_code and b and re.sub(r'\s*\+\d[\d\s-]*$', '', a) == b)


def _slug(text):
    return re.sub(r'[^a-z0-9]+', '-', str(text or '').casefold()).strip('-')[:80]


def _digits(text):
    return re.sub(r'\D', '', str(text or ''))


def _clean(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def consent_text(question):
    """The exact consent wording the user agrees to: label, description and option labels."""
    parts = [_clean(question['label']), _clean(question.get('description')), ' | '.join(question.get('options') or [])]
    return '\n'.join(p for p in parts if p)


def mentions(text):
    """ISO codes of countries a text names (EU for the EU/EEA)."""
    found = set()
    low = (text or '').casefold()
    for code, (name, words) in COUNTRIES.items():
        for word in (name.casefold(),) + words:
            if re.search(r'(?<![a-z])' + re.escape(word) + r'(?![a-z])', low):
                found.add(code)
                break
    for code, pattern in ABBREVIATIONS.items():
        if re.search(pattern, text or ''):
            found.add(code)
    return found


def test_mode():
    return os.environ.get('JOBFATHER_TEST_MODE') == '1'


def local_base():
    """Test-only stand-in for Greenhouse: a loopback server, honoured only when JOBFATHER_TEST_MODE=1."""
    base = os.environ.get('JOBFATHER_GREENHOUSE_BASE', '').rstrip('/')
    if not base or not test_mode():
        return None
    if urllib.parse.urlsplit(base).hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('JOBFATHER_GREENHOUSE_BASE must be a loopback address')
    return base


# ------------------------------------------------------------------ questions
def classify_kind(field, label, group, description='', options=()):
    if group == 'compliance':
        return 'eeo'
    if group == 'demographic':
        return 'demographic'
    if field in FIELD_KINDS:
        return FIELD_KINDS[field]
    for kind, pattern in LABEL_KINDS:
        text = '\n'.join([label, description] + list(options)) if kind == 'consent' else label
        if pattern.search(text):
            return kind
    return 'custom'


def _from_question(raw, group):
    label = _clean(raw.get('label'))
    description = html_text(raw.get('description')) if raw.get('description') else ''
    out = []
    for field in raw.get('fields') or []:
        if not isinstance(field, dict) or not field.get('name'):
            continue
        name = str(field['name'])
        values = [v for v in field.get('values') or [] if isinstance(v, dict)]
        options = [_clean(v.get('label')) for v in values]
        item = {'qkey': name, 'label': label, 'required': bool(raw.get('required')), 'type': str(field.get('type')),
                'options': options, 'group': group,
                'kind': classify_kind(name, label, group, description, options)}
        if description:
            item['description'] = description
        if item['type'] == 'multi_value_multi_select' and values:
            item['option_ids'] = [str(v.get('value')) for v in values]
        if name in TEXT_ALTERNATIVES:
            item.update(required=False, alternative_to=TEXT_ALTERNATIVES[name])
        if name in DOM_IDS:
            item['dom_id'] = DOM_IDS[name]
        if name == 'location':
            item['async_options'] = True
        out.append(item)
    return out


def _dom_only(qkey, label, required, kind, group, **extra):
    """A control the hosted form shows that the question API does not list; options come from the page."""
    return dict({'qkey': qkey, 'label': label, 'required': required, 'type': 'multi_value_single_select',
                 'options': [], 'group': group, 'kind': kind, 'options_from': 'form', 'dom_only': True}, **extra)


def normalize_questions(data):
    """Greenhouse's question API -> one entry per fillable control, in form order."""
    main = []
    for raw in data.get('questions') or []:
        main += _from_question(raw, 'main')
    phone = next((q for q in main if q['qkey'] == 'phone'), None)
    if phone:  # the hosted form pairs the phone box with a country picker
        main.insert(main.index(phone), _dom_only('country', 'Country', phone['required'], 'contact', 'main',
                                                 match='dial_code'))
    if data.get('education') in ('education_required', 'education_optional'):
        required = data['education'] == 'education_required'
        main += [_dom_only('school--0', 'School', required, 'custom', 'main', async_options=True),
                 _dom_only('degree--0', 'Degree', required, 'custom', 'main')]
    location = []
    for raw in data.get('location_questions') or []:
        location += _from_question(raw, 'location')
    compliance = []
    for section in data.get('compliance') or []:
        for raw in section.get('questions') or []:
            items = _from_question(raw, 'compliance')
            for item in items:
                if item['qkey'] == 'race':  # shown only after "Are you Hispanic/Latino?" is answered "No"
                    compliance.append(_dom_only('hispanic_ethnicity', 'Are you Hispanic/Latino?', item['required'],
                                                'eeo', 'compliance'))
                    item['conditional'] = True
                compliance.append(item)
    demographic = []
    for raw in (data.get('demographic_questions') or {}).get('questions') or []:
        options = [o for o in raw.get('answer_options') or [] if isinstance(o, dict)]
        item = {'qkey': 'demographic_%s' % raw.get('id'), 'label': _clean(raw.get('label')),
                'required': bool(raw.get('required')), 'type': str(raw.get('type')),
                'options': [_clean(o.get('label')) for o in options], 'group': 'demographic', 'kind': 'demographic',
                'dom_id': str(raw.get('id'))}
        declines = [_clean(o.get('label')) for o in options if o.get('decline_to_answer')]
        if declines:
            item['decline_options'] = declines
        free = [_clean(o.get('label')) for o in options if o.get('free_form')]
        if free:
            item['free_form_options'] = free
        demographic.append(item)
    consent = []  # GDPR boxes: their wording exists only on the page, so only the user ticks them
    for item in data.get('data_compliance') or []:
        if isinstance(item, dict) and item.get('type') == 'gdpr':
            if item.get('requires_processing_consent'):
                consent.append(_dom_only('gdpr_processing_consent_given', 'Consent to processing of my data (GDPR)',
                                         True, 'consent', 'compliance', control='checkbox',
                                         type='multi_value_multi_select'))
            if item.get('requires_retention_consent'):
                consent.append(_dom_only('gdpr_retention_consent_given', 'Consent to keep my data for future '
                                         'openings (GDPR)', False, 'consent', 'compliance', control='checkbox',
                                         type='multi_value_multi_select'))
    return main + location + compliance + demographic + consent


def _api_get(path, opener=None, timeout=20):
    url = (local_base() or API_ROOT) + path
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise ValueError('Greenhouse returned HTTP %s' % error.code)
    except urllib.error.URLError as error:
        raise ValueError('Could not reach Greenhouse (%s)' % error.reason)


def _past(deadline):
    try:
        if len(deadline) == 10:
            return date.fromisoformat(deadline) < datetime.now(timezone.utc).date()
        moment = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
        return moment.tzinfo is not None and moment < datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False


def fetch(url, board=None, opener=None):
    """The job's normalised questions and their hash. Stops with `closed` when the job is gone."""
    info = classify(url)
    if info['ats'] != 'greenhouse':
        raise Stop('not_greenhouse', 'Not a Greenhouse form (%s); use guided filling (references/ATS.md)' % info['ats'])
    if not info['supported_autofill']:
        raise Stop('unsupported_board', 'EU-hosted Greenhouse boards are not filled by script yet; use guided filling')
    board = board or info['board']
    if not info['job_id']:
        raise Stop('needs_input', 'The link carries no Greenhouse job id; open it and copy the job link '
                   '(short links: classify.py --resolve)')
    if not board:
        raise Stop('needs_input', "The link does not name the Greenhouse board; pass --board with the company's "
                   'board token')
    if not BOARD.match(board):
        raise ValueError('Invalid board token')
    quoted = urllib.parse.quote(board)
    data = _api_get('/v1/boards/%s/jobs/%s?questions=true' % (quoted, info['job_id']), opener)
    if data is None:
        if _api_get('/v1/boards/%s' % quoted, opener) is None:
            raise ValueError('Greenhouse board not found: %s' % board)
        raise Stop('closed', 'This job is no longer on the board', EXIT_CLOSED)
    if data.get('application_deadline') and _past(str(data['application_deadline'])):
        raise Stop('closed', 'The application deadline has passed', EXIT_CLOSED)
    if str(data.get('id', info['job_id'])) != info['job_id']:
        raise ValueError('Greenhouse returned a different job than requested')
    company = data.get('company_name') or (_api_get('/v1/boards/%s' % quoted, opener) or {}).get('name') or board
    questions = normalize_questions(data)
    return {'schema': QUESTIONS_SCHEMA, 'ats': 'greenhouse', 'board': board, 'job_id': info['job_id'],
            'url': greenhouse_apply_url(board, info['job_id']), 'title': _clean(data.get('title')),
            'company': _clean(company), 'location': _clean((data.get('location') or {}).get('name')),
            'updated_at': data.get('updated_at'), 'questions_sha256': sha_json(questions), 'questions': questions}


# ------------------------------------------------------------------ profile values
def profile_context(ws, job_location=''):
    """Identity, HR details and saved form answers, read after the workspace recovery barrier."""
    from memory import read_context
    content = read_context([layout.IDENTITY, layout.HR, layout.FORM_ANSWERS], ws)['content']

    def parse(path):
        value = json.loads(content[path]) if content.get(path) else {}
        return value if isinstance(value, dict) else {}
    answers = parse(layout.FORM_ANSWERS).get('answers')
    return {'ws': Path(ws), 'identity': parse(layout.IDENTITY), 'hr': parse(layout.HR),
            'answers': answers if isinstance(answers, dict) else {}, 'job_location': job_location or ''}


def _raw(ctx, source):
    """The confirmed profile value behind a source path, or None when unknown or unconfirmed."""
    parts = source.split('.')
    if len(parts) < 3 or parts[0] != 'profile':
        return None
    ident = ctx['identity']
    contacts = ident.get('contacts') if isinstance(ident.get('contacts'), dict) else {}
    if parts[1] == 'identity':
        key = parts[2]
        if key in ('first_name', 'last_name') and len(parts) == 3:
            explicit = ident.get('given_name' if key == 'first_name' else 'family_name') or ident.get(key)
            if isinstance(explicit, str) and explicit.strip():
                return explicit.strip()
            tokens = str(ident.get('full_name') or '').split()
            return (tokens[0] if key == 'first_name' else tokens[1]) if len(tokens) == 2 else None
        if key in ('full_name', 'preferred_name') and len(parts) == 3:
            return ident.get(key) or None
        if key in ('email', 'phone', 'location') and len(parts) == 3:
            return contacts.get(key) or None
        if key == 'country' and len(parts) == 3:
            code = str((ident.get('locale') or {}).get('country') or '').upper()
            return COUNTRIES[code][0] if code in COUNTRIES else None
        if key == 'links' and len(parts) == 4:
            for link in contacts.get('links') or []:
                if isinstance(link, dict) and _slug(link.get('label')) == parts[3]:
                    return link.get('url') or None
        return None
    if parts[1] == 'hr':
        hr = ctx['hr']
        if parts[2] == 'custom':
            item = (hr.get('custom') or {}).get(parts[3]) if len(parts) == 4 else None
        else:
            item = hr.get(parts[2]) if len(parts) == 3 and parts[2] != 'self_identification' else None
        if isinstance(item, dict) and item.get('value') is not None and item.get('confirmed_at'):
            return item
        return None
    if parts[1] == 'form_answers':
        item = ctx['answers'].get('.'.join(parts[2:]))
        if isinstance(item, dict) and item.get('scope', 'global') == 'global' and item.get('answer') not in (None, ''):
            return item['answer']
        return None
    if parts[1] == 'experience' and len(parts) == 3 and parts[2] in ('current_employer', 'current_job_title'):
        import facts
        return facts.field(parts[2], ctx['ws']).get('value')
    return None


def _as_text(value):
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, (int, float)):
        return '%d' % value if float(value).is_integer() else str(value)
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return ', '.join(v.strip() for v in value)
    return None


def _money(item):
    amount = None if isinstance(item.get('value'), bool) else _as_text(item.get('value'))
    if not amount:
        return None
    text = ' '.join([amount] + ([str(item['currency'])] if item.get('currency') else []))
    return text + (' per ' + str(item['period']) if item.get('period') else '')


def _fit(question, text):
    """Shape a profile text into this question's answer: exact option labels only."""
    if text is None:
        return None
    kind = question['type']
    if kind in TEXT_LIMITS:
        return text if len(text) <= TEXT_LIMITS[kind] and (kind == 'textarea' or '\n' not in text) else None
    if kind in SELECTS and question.get('options_from') == 'form':
        return text if kind == 'multi_value_single_select' else None
    matches = [o for o in question.get('options') or [] if same_option(o, text)]
    if len(matches) != 1:
        return None
    return matches[0] if kind == 'multi_value_single_select' else ([matches[0]] if kind in SELECTS else None)


def _jurisdiction_ok(question, ctx, jurisdiction, strict):
    """A legal answer is mapped only for the country the user's answer was confirmed for."""
    code = str(jurisdiction or '').strip().upper()
    if not code:
        return False
    asked = mentions(question['label'] + '\n' + question.get('description', ''))
    if asked:
        return asked == {code}
    if not strict:
        return True
    return mentions(ctx['job_location']) == {code}


def derive(question, source, ctx):
    """The answer a profile source gives for this question, or None when it does not fit exactly."""
    if not isinstance(source, str) or not source.startswith('profile.'):
        return None
    raw = _raw(ctx, source)
    if raw is None:
        return None
    kind, label = question['kind'], question['label']
    selectable = question['type'] in SELECTS
    if not source.startswith('profile.hr.'):
        return _fit(question, _as_text(raw))
    value = raw.get('value')
    if kind == 'work_auth':
        if not AUTH_ASK.search(label) or INVERTED.search(label) \
                or not _jurisdiction_ok(question, ctx, raw.get('jurisdiction'), selectable):
            return None
        if isinstance(value, bool):
            text = 'Yes' if value else 'No'
        elif isinstance(value, str) and selectable:
            text = None if NEGATIVE.search(value) else 'Yes'
        else:
            text = _as_text(value)
    elif kind == 'sponsorship':
        if not NEEDS_SPONSOR.search(label) or INVERTED.search(label) or not isinstance(value, bool) \
                or not _jurisdiction_ok(question, ctx, raw.get('jurisdiction'), True):
            return None
        text = 'Yes' if value else 'No'
    elif kind == 'relocation':
        if not WILL_RELOCATE.search(label) or COMPOUND.search(label) or not isinstance(value, bool):
            return None
        text = 'Yes' if value else 'No'
    elif source.split('.')[2] in ('expected_compensation', 'current_compensation', 'minimum_compensation'):
        text = None if selectable else _money(raw)
    else:
        text = _as_text(value)
    return _fit(question, text)


def _form_answer_key(question, answers):
    want, slug = question_key(question['label']), _slug(question['label'])
    for key in sorted(answers):
        item = answers[key]
        if not isinstance(item, dict) or item.get('scope', 'global') != 'global':
            continue
        if key == slug or question_key(item.get('question')) == want:
            return key
    return None


def suggest_source(question, ctx):
    """Where a prefilled answer would come from; None for consent, self-identification and unknowns."""
    kind, field, label = question['kind'], question['qkey'], question_key(question['label'])
    if kind in ('consent',) + SELF_ID:
        return None
    if field in FILE_FIELDS:
        return 'files.' + field
    by_field = {'first_name': 'profile.identity.first_name', 'last_name': 'profile.identity.last_name',
                'preferred_name': 'profile.identity.preferred_name', 'email': 'profile.identity.email',
                'phone': 'profile.identity.phone', 'country': 'profile.identity.country',
                'location': 'profile.identity.location'}
    if field in by_field:
        return by_field[field]
    key = _form_answer_key(question, ctx['answers'])
    if key:
        return 'profile.form_answers.' + key
    if kind == 'identity':
        if 'prefer' in label:
            return 'profile.identity.preferred_name'
        if re.search(r'\b(?:first|given) name\b', label):
            return 'profile.identity.first_name'
        if re.search(r'\b(?:last|family) name\b|\bsurname\b', label):
            return 'profile.identity.last_name'
        return 'profile.identity.full_name' if re.search(r'\bfull name\b', label) else None
    if kind == 'contact':
        if re.search(r'\be-?mail\b', label):
            return 'profile.identity.email'
        if re.search(r'\b(?:phone|mobile)\b', label):
            return 'profile.identity.phone'
        if label in LOCATION_LABELS:
            return 'profile.identity.location'
        if 'country' in label and re.search(r'resid|live|located|based', label):
            return 'profile.identity.country'
        return None
    if kind == 'links':
        links = {_slug(l.get('label')) for l in (ctx['identity'].get('contacts') or {}).get('links') or []
                 if isinstance(l, dict)}
        if 'linkedin' in label:
            wanted = ['linkedin']
        elif 'github' in label:
            wanted = ['github']
        elif re.search(r'portfolio|website|personal site|blog', label):
            wanted = ['portfolio', 'website', 'personal-website', 'blog']
        else:
            return None
        return next(('profile.identity.links.' + name for name in wanted if name in links), None)
    if kind == 'salary':
        return 'profile.hr.%s_compensation' % ('current' if re.search(r'\bcurrent\b', label) else 'expected')
    if kind == 'notice':
        return 'profile.hr.earliest_start_date' if 'date' in label else 'profile.hr.notice_period'
    return {'work_auth': 'profile.hr.work_authorization', 'sponsorship': 'profile.hr.sponsorship_required',
            'relocation': 'profile.hr.relocation_willingness'}.get(kind)


# ------------------------------------------------------------------ files and plans
def workspace_pdf(ws, relative):
    """A PDF inside the workspace (no traversal, no symlinks) or a ValueError naming the problem code."""
    if not isinstance(relative, str) or not relative.strip() or Path(relative).is_absolute() \
            or relative.startswith('~') or re.match(r'^[A-Za-z]:[\\/]', relative):
        raise ValueError('file_outside_workspace')
    try:
        path = contained(ws, relative)
    except ValueError:
        raise ValueError('file_outside_workspace')
    if path.suffix.lower() != '.pdf':
        raise ValueError('file_not_pdf')
    if not path.is_file():
        raise ValueError('file_missing')
    with path.open('rb') as handle:
        if handle.read(5) != b'%PDF-':
            raise ValueError('file_not_pdf')
    return path


def _relative_in(ws, given):
    path = Path(given).expanduser()
    if not path.is_absolute():
        return path.as_posix()
    resolved, root = path.resolve(), Path(ws).resolve()
    if root not in resolved.parents:
        raise ValueError('Files to upload must be inside the workspace')
    return resolved.relative_to(root).as_posix()


def file_entry(ws, given, record, key):
    """{path, sha256[, output_id]} for an upload; the resume defaults to the application's latest validated CV."""
    cvs = [o for o in (record or {}).get('outputs', []) if o.get('kind') == 'cv-pdf'
           and o.get('validation_status') == 'passed' and o.get('path_root') == 'workspace']
    output_id = None
    if given:
        relative = _relative_in(ws, given)
    elif key == 'resume' and cvs:
        latest = max(cvs, key=lambda o: o.get('version', 0))
        relative, output_id = latest['path'], latest['output_id']
    else:
        return None
    try:
        path = workspace_pdf(ws, relative)
    except ValueError as error:
        raise ValueError('%s: %s' % (key, str(error).replace('_', ' ')))
    entry = {'path': relative, 'sha256': digest(path)}
    output_id = output_id or next((o['output_id'] for o in cvs if o.get('sha256') == entry['sha256']), None)
    if output_id and key == 'resume':
        entry['output_id'] = output_id
    return entry


def plan_hash(plan):
    return sha_json({k: v for k, v in plan.items() if k != 'plan_sha256'})


def approval_payload(plan):
    """What an ats_submit approval binds: this exact plan, resume and form."""
    resume = (plan.get('files') or {}).get('resume') or {}
    return {'plan_sha256': plan_hash(plan), 'resume_sha256': resume.get('sha256'), 'url': plan['job']['url']}


def application_dir(ws, plan_or_id, sandbox=None):
    if isinstance(plan_or_id, dict):
        app_id, sandbox = plan_or_id['application_id'], plan_or_id.get('sandbox') is True
    else:
        app_id = plan_or_id
    if not APP_ID.match(app_id or ''):
        raise ValueError('Invalid application ID')
    return contained(ws, layout.sandboxed(layout.APPLICATIONS, bool(sandbox)) + '/' + app_id)


def plan_template(url, application_id, ws=None, test=False, resume=None, cover_letter=None, board=None, out=None,
                  force=False, opener=None):
    """Write an answer-plan skeleton: profile-backed answers prefilled with their source, the rest null."""
    ws = require_workspace(ws)
    record = Store(ws, test=test).load(application_id)
    fetched = fetch(url, board, opener)
    ctx = profile_context(ws, fetched['location'])
    files = {'resume': file_entry(ws, resume, record, 'resume'),
             'cover_letter': file_entry(ws, cover_letter, record, 'cover_letter')}
    answers, self_id, unanswered = [], [], []
    for q in fetched['questions']:
        if q['type'] == 'input_hidden' or q.get('alternative_to'):
            continue
        if q['kind'] in SELF_ID:
            self_id.append({k: q[k] for k in ('qkey', 'label', 'required', 'options', 'options_from') if k in q})
            continue
        entry = {'field': q['qkey'], 'label': q['label'], 'type': q['type'], 'kind': q['kind'], 'group': q['group'],
                 'required': q['required'], 'options': q['options'], 'value': None, 'source': None, 'approved': False}
        if q.get('options_from'):
            entry['options_from'] = q['options_from']
        if q['kind'] == 'consent' and not q.get('dom_only'):
            entry['consent_text'] = consent_text(q)
            entry['consent_text_sha256'] = sha_text(entry['consent_text'])
        source = suggest_source(q, ctx)
        if source in ('files.resume', 'files.cover_letter'):
            if files[source.split('.')[1]]:
                entry.update(value=source, source=source)
        elif source:
            value = derive(q, source, ctx)
            if value is not None:
                entry.update(value=value, source=source)
        if entry['value'] is None and not q['required']:
            unanswered.append(q['qkey'])
        answers.append(entry)
    job = {k: fetched[k] for k in ('board', 'job_id', 'url', 'title', 'company', 'questions_sha256')}
    plan = {'schema': PLAN_SCHEMA, 'ats': 'greenhouse', 'job': job, 'application_id': application_id,
            'sandbox': bool(test), 'files': files, 'answers': answers,
            'self_identification': {'mode': 'decline', 'answers': [], 'questions': self_id},
            'unanswered_optional': unanswered, 'created_at': now()}
    plan['plan_sha256'] = plan_hash(plan)
    if out:
        path = Path(out).expanduser()
        path = path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
        if Path(ws).resolve() not in path.parents:
            raise ValueError('Answer plans hold personal data and stay inside the workspace')
    else:
        path = application_dir(ws, application_id, test) / 'ats' / ('plan-greenhouse-%s-%s.json'
                                                                    % (job['board'], job['job_id']))
    if path.exists() and not force:
        raise ValueError('A plan already exists at %s; edit it, or pass --force to start again' % path)
    atomic_json(path, plan)
    return plan, path


def load_plan(ws, plan_path):
    """Read a plan from inside the workspace and check it belongs to an existing application."""
    path = Path(plan_path).expanduser()
    path = path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
    if Path(ws).resolve() not in path.parents:
        raise ValueError('Answer plans hold personal data and must live inside the workspace')
    plan = read_json(path)
    _check_shape(plan)
    Store(ws, test=plan.get('sandbox') is True).load(plan['application_id'])  # raises for an unknown application
    return plan, path


def _check_shape(plan):
    if not isinstance(plan, dict) or plan.get('schema') != PLAN_SCHEMA or plan.get('ats') != 'greenhouse':
        raise Stop('invalid_plan', 'Not a Greenhouse answer plan (%s)' % PLAN_SCHEMA)
    job = plan.get('job')
    if not isinstance(job, dict) or not all(isinstance(job.get(k), str) and job[k] for k in
                                            ('board', 'job_id', 'url', 'questions_sha256')):
        raise Stop('invalid_plan', 'The plan needs job.board, job.job_id, job.url and job.questions_sha256')
    if not APP_ID.match(str(plan.get('application_id') or '')):
        raise Stop('invalid_plan', 'The plan needs a valid application_id')
    if not isinstance(plan.get('answers'), list) or not all(isinstance(a, dict) for a in plan['answers']):
        raise Stop('invalid_plan', 'answers must be a list of objects')
    files = plan.get('files')
    if not isinstance(files, dict) or set(files) - set(FILE_FIELDS) \
            or any(v is not None and not isinstance(v, dict) for v in files.values()):
        raise Stop('invalid_plan', 'files holds resume and cover_letter (an object or null each)')
    if not isinstance(plan.get('self_identification'), dict):
        raise Stop('invalid_plan', 'self_identification must be an object')
    if not isinstance(plan.get('sandbox', False), bool):
        raise Stop('invalid_plan', 'sandbox must be true or false')


def stamp(plan, path):
    """Record the hash of the plan as checked; the approval binds the same hash."""
    digest_now = plan_hash(plan)
    if plan.get('plan_sha256') != digest_now:
        plan['plan_sha256'] = digest_now
        atomic_json(path, plan)
    return digest_now


# ------------------------------------------------------------------ validation
SOURCE = re.compile(r'^(?:profile\.(?:identity|hr|form_answers|experience)\.[A-Za-z0-9_.-]+|files\.(?:resume|cover_letter)|'
                    r'user:\S.*|draft:\S.*)$')


def _source_allowed(kind, source):
    if source.startswith('files.'):
        return False  # only file questions take files.*, checked separately
    if kind == 'consent':
        return source.startswith('user:')
    if kind in SENSITIVE or kind in ('identity', 'contact', 'links'):
        return source.startswith(('profile.', 'user:'))
    return True  # custom: profile, user or an approved draft


def _same_value(question, value, expected):
    if question['type'] in TEXT_LIMITS:
        if question['qkey'] == 'phone' or (question['kind'] == 'contact' and re.search(r'phone|mobile', question['label'], re.I)):
            return bool(_digits(value)) and _digits(value) == _digits(expected)
        return value.strip() == expected.strip()
    if question['type'] == 'multi_value_single_select':
        return same_option(expected, value, question.get('match') == 'dial_code')
    if isinstance(value, list) and isinstance(expected, list):
        return sorted(map(option_key, value)) == sorted(map(option_key, expected))
    return False


def _canonical(question, value, problem):
    """The exact option labels (or text) to put on the form, or None after recording a problem."""
    field, kind = question['qkey'], question['type']
    if kind in TEXT_LIMITS:
        if not isinstance(value, str) or not value.strip():
            problem(field, 'bad_value_type')
            return None
        if len(value) > TEXT_LIMITS[kind] or (kind == 'input_text' and '\n' in value):
            problem(field, 'value_too_long' if len(value) > TEXT_LIMITS[kind] else 'bad_value_type')
            return None
        return value
    if kind not in SELECTS:
        problem(field, 'unsupported_question_type')
        return None
    values = value if kind == 'multi_value_multi_select' else [value]
    if kind == 'multi_value_multi_select' and question.get('options_from') == 'form':
        problem(field, 'consent_manual_only' if question['kind'] == 'consent' else 'unsupported_question_type')
        return None
    if not isinstance(values, list) or not values or not all(isinstance(v, str) and v.strip() for v in values):
        problem(field, 'bad_value_type')
        return None
    if question.get('options_from') == 'form':
        return values[0]  # matched exactly against the page's options while filling
    chosen = []
    for item in values:
        hits = [o for o in question['options'] if same_option(o, item)]
        if len(hits) != 1:
            problem(field, 'option_not_found' if not hits else 'ambiguous_option')
            return None
        if hits[0] in chosen:
            problem(field, 'duplicate_option')
            return None
        chosen.append(hits[0])
    return chosen if kind == 'multi_value_multi_select' else chosen[0]


def _control(question):
    if question['type'] in ('input_text', 'textarea'):
        return 'text'
    if question['type'] == 'input_file':
        return 'file'
    if question.get('control') == 'checkbox':
        return 'checkbox'
    if question['type'] == 'multi_value_multi_select':
        return 'multi_select' if question['group'] == 'demographic' else 'checkboxes'
    return 'select'


def _action(question, value, **extra):
    action = {'field': question['qkey'], 'dom_id': question.get('dom_id') or question['qkey'],
              'type': question['type'], 'kind': question['kind'], 'control': _control(question),
              'required': question['required'], 'value': value}
    for key in ('match', 'async_options', 'dom_only', 'conditional'):
        if question.get(key):
            action[key] = question[key]
    if action['control'] == 'checkboxes':
        wanted = {option_key(v) for v in value or []}
        action['checkboxes'] = [{'id': '%s_%s' % (question['qkey'], oid), 'label': label,
                                 'checked': option_key(label) in wanted}
                                for label, oid in zip(question['options'], question.get('option_ids') or [])]
    action.update(extra)
    return action


def _check_answer(entry, question, ctx, files, mode, problem):
    field, kind = question['qkey'], question['kind']
    value, source, approved = entry.get('value'), entry.get('source'), entry.get('approved') is True
    if entry.get('manual') is True:  # the user fills this one by hand in the visible window
        if value not in (None, '', []):
            problem(field, 'manual_with_value')
        elif mode == 'submit' and not approved:
            problem(field, 'not_approved')
        else:
            return _action(question, None, manual=True)
        return None
    if value in (None, '', []):
        if question['required']:
            problem(field, 'required_unanswered')
        elif kind == 'consent' and _control(question) in ('checkboxes', 'checkbox'):
            return _action(question, [], untick=True)  # a box the page pre-ticks is cleared, never left on
        return None
    if not isinstance(source, str) or not source.strip():
        problem(field, 'source_missing')
        return None
    if question['type'] == 'input_file':
        if field not in FILE_FIELDS:
            problem(field, 'unsupported_file_question')
        elif value != 'files.' + field or source != value:
            problem(field, 'source_not_allowed')
        elif field not in files:
            problem(field, 'file_not_planned')
        elif files[field] is None:
            pass  # the file problem is already reported
        elif mode == 'submit' and not approved:
            problem(field, 'not_approved')
        else:
            return _action(question, files[field]['path'], file_name=files[field]['name'],
                           file_size=files[field]['size'])
        return None
    if not SOURCE.match(source) or not _source_allowed(kind, source):
        problem(field, 'source_not_allowed')
        return None
    canonical = _canonical(question, value, problem)
    if canonical is None:
        return None
    if source.startswith('profile.'):
        expected = derive(question, source, ctx)
        if expected is None or not _same_value(question, value, expected):
            problem(field, 'profile_mismatch')
            return None
    if kind == 'consent':
        if not approved:
            problem(field, 'consent_not_approved')
            return None
        if entry.get('consent_text_sha256') != sha_text(consent_text(question)):
            problem(field, 'consent_text_changed')
            return None
    if mode == 'submit' and not approved:
        problem(field, 'not_approved')
        return None
    return _action(question, canonical)


def _decline_options(question):
    flagged = question.get('decline_options') or []
    return flagged if flagged else [o for o in question['options'] if DECLINE.match(option_key(o))]


def _self_identification(plan, questions, mode, problem):
    """EEO and demographic questions: the decline option unless the user gave an answer (user:*)."""
    block = plan['self_identification']
    choice = block.get('mode', 'decline')
    if choice not in ('decline', 'answers'):
        problem('self_identification', 'bad_mode')
        return []
    given = {}
    for item in block.get('answers') or []:
        if not isinstance(item, dict) or not isinstance(item.get('field'), str):
            problem('self_identification', 'bad_answer')
            continue
        given[item['field']] = item
    if choice == 'decline' and given:
        problem('self_identification', 'answers_in_decline_mode')
    known = {q['qkey'] for q in questions if q['kind'] in SELF_ID}
    for field in sorted(set(given) - known):
        problem(field, 'unknown_field')
    actions = []
    for q in questions:
        if q['kind'] not in SELF_ID:
            continue
        item = given.get(q['qkey']) if choice == 'answers' else None
        if item is not None:
            if item.get('manual') is True:
                if mode == 'submit' and item.get('approved') is not True:
                    problem(q['qkey'], 'not_approved')
                else:
                    actions.append(_action(q, None, manual=True, self_id=True))
                continue
            source = item.get('source')
            if not isinstance(source, str) or not re.match(r'^user:\S', source):
                problem(q['qkey'], 'self_id_source')
                continue
            if mode == 'submit' and item.get('approved') is not True:
                problem(q['qkey'], 'not_approved')
                continue
            canonical = _canonical(q, item.get('value'), problem)
            chosen = canonical if isinstance(canonical, list) else [canonical]
            if canonical is not None and set(chosen) & set(q.get('free_form_options') or []):
                problem(q['qkey'], 'free_form_not_supported')  # needs typed text: the user does it by hand
            elif canonical is not None:
                actions.append(_action(q, canonical, self_id=True))
            continue
        if q.get('options_from') == 'form':
            actions.append(_action(q, None, decline=True, self_id=True))
            continue
        declines = _decline_options(q)
        if len(declines) == 1:
            value = [declines[0]] if q['type'] == 'multi_value_multi_select' else declines[0]
            actions.append(_action(q, value, decline=True, self_id=True))
        elif q['required']:
            problem(q['qkey'], 'no_decline_option' if not declines else 'ambiguous_decline_option')
    return actions


def _check_files(plan, ws, problem):
    """Uploads must be PDFs inside the workspace whose bytes still match the planned sha256."""
    checked = {}
    for key in FILE_FIELDS:
        entry = plan['files'].get(key)
        if entry is None:
            continue
        checked[key] = None
        try:
            path = workspace_pdf(ws, entry.get('path'))
        except ValueError as error:
            problem(key, str(error))
            continue
        if digest(path) != entry.get('sha256'):
            problem(key, 'file_hash_mismatch')
            continue
        checked[key] = {'path': str(path), 'name': path.name, 'size': path.stat().st_size}
    return checked


def validate_plan(plan, fetched, ws, mode='validate', ctx=None):
    """Every safety rule that can be checked without a browser. Returns {ok, problems, actions}.

    problems are [{field, code}] (never values); actions are what the browser layer fills.
    Stops when the plan belongs to another job or the form's questions changed.
    """
    _check_shape(plan)
    job = plan['job']
    if (job['board'], job['job_id'], job['url']) != (fetched['board'], fetched['job_id'], fetched['url']):
        raise Stop('job_mismatch', 'The plan is for a different job or form than the one fetched')
    if job['questions_sha256'] != fetched['questions_sha256']:
        raise Stop('questions_changed', 'The form questions changed since the plan was made; run plan-template '
                   'again (--force) and redo the answers')
    problems = []

    def problem(field, code):
        problems.append({'field': field, 'code': code})
    ctx = ctx or profile_context(ws, fetched['location'])
    files = _check_files(plan, ws, problem)
    questions = {q['qkey']: q for q in fetched['questions']}
    order = {q['qkey']: i for i, q in enumerate(fetched['questions'])}
    seen, actions = set(), []
    for entry in plan['answers']:
        field = entry.get('field')
        question = questions.get(field) if isinstance(field, str) else None
        if question is None:
            problem(field, 'unknown_field')
            continue
        if field in seen:
            problem(field, 'duplicate_field')
            continue
        seen.add(field)
        if question['kind'] in SELF_ID:
            problem(field, 'self_identification_in_answers')
            continue
        if question['type'] == 'input_hidden' or question.get('alternative_to'):
            problem(field, 'not_fillable')
            continue
        action = _check_answer(entry, question, ctx, files, mode, problem)
        if action:
            actions.append(action)
    for q in fetched['questions']:
        if q['kind'] not in SELF_ID and q['type'] != 'input_hidden' and not q.get('alternative_to') \
                and q['qkey'] not in seen:
            problem(q['qkey'], 'missing_field')
    actions += _self_identification(plan, fetched['questions'], mode, problem)
    actions.sort(key=lambda a: order.get(a['field'], len(order)))
    return {'ok': not problems, 'problems': problems, 'actions': actions}


# ------------------------------------------------------------------ settings, pacing and the ledger
def ats_settings(ws):
    path = Path(ws) / layout.PLATFORMS
    data = read_json(path) if path.is_file() else {}
    settings = ((data.get('ats') or {}).get('greenhouse') or {}) if isinstance(data, dict) else {}
    cap = settings.get('daily_cap', DEFAULT_DAILY_CAP)
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 100:
        raise ValueError('config/platforms.json ats.greenhouse.daily_cap must be a whole number from 1 to 100')
    return {'enabled': settings.get('enabled', True) is True, 'submit_mode': settings.get('submit_mode', 'user_click'),
            'daily_cap': cap}


def read_ledger(ws):
    path = contained(ws, LEDGER)
    if not path.is_file():
        return []
    entries = []
    for line in read_text(path).splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue  # a torn last line after a crash
    return entries


def append_ledger(ws, entry):
    path = contained(ws, LEDGER)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('ab') as handle:
        handle.write((json.dumps(entry, sort_keys=True, ensure_ascii=False) + '\n').encode('utf-8'))
        handle.flush()
        os.fsync(handle.fileno())


def _moment(text):
    return datetime.fromisoformat(str(text).replace('Z', '+00:00'))


def check_pacing(ws, settings, clock=None):
    """At least a minute between submissions (any ATS) and the daily Greenhouse cap."""
    clock = clock or datetime.now(timezone.utc)
    entries = read_ledger(ws)
    starts = [_moment(e['at']) for e in entries if e.get('event') in ('submit_started', 'submitted') and e.get('at')]
    if starts:
        gap = (clock - max(starts)).total_seconds()
        if gap < MIN_GAP_SECONDS:
            raise Stop('paced', 'Submissions are at least a minute apart; try again in %d seconds'
                       % math.ceil(MIN_GAP_SECONDS - gap), retry_after_s=math.ceil(MIN_GAP_SECONDS - gap))
    zone = tz(ws)[0]
    today = clock.astimezone(zone).date()
    count = sum(1 for e in entries if e.get('ats') == 'greenhouse' and e.get('event') in COUNTED and e.get('at')
                and _moment(e['at']).astimezone(zone).date() == today)
    if count >= settings['daily_cap']:
        raise Stop('daily_cap', 'Daily cap reached (%d Greenhouse submissions today); continue tomorrow'
                   % settings['daily_cap'])
    return {'today': count, 'daily_cap': settings['daily_cap']}


def check_unused(ws, approval_id):
    """An approval may start another run only after earlier runs ended before the Submit stage.

    A run that started and never recorded an abort (say, the process was killed while the user had
    the form) may have been submitted, so its approval cannot fill the form again.
    """
    entries = read_ledger(ws)
    started = {e.get('run_id') for e in entries if e.get('approval_id') == approval_id
               and e.get('event') == 'submit_started'}
    aborted = {e.get('run_id') for e in entries if e.get('event') == 'submit_aborted'}
    if started - aborted:
        raise Stop('approval_invalid', 'This approval already started a run that may have reached the Submit button; '
                   'ask the user whether they submitted, and get a new approval to try again', EXIT_APPROVAL)


def _ledger_event(ws, plan, event, run_id, approval_id, **extra):
    entry = {'at': now(), 'event': event, 'ats': 'greenhouse', 'board': plan['job']['board'],
             'job_id': plan['job']['job_id'], 'application_id': plan['application_id'], 'run_id': run_id,
             'approval_id': approval_id}
    entry.update(extra)
    append_ledger(ws, entry)


# ------------------------------------------------------------------ runs and reports
def new_run(ws, plan, mode):
    run_id = '%s-%s-%s' % (mode, datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'), secrets.token_hex(3))
    directory = application_dir(ws, plan) / 'ats' / run_id
    directory.mkdir(parents=True, exist_ok=False)
    return run_id, directory


def _rel(ws, path):
    return Path(path).resolve().relative_to(Path(ws).resolve()).as_posix()


def form_url(plan):
    url = plan['job']['url']
    base = local_base()
    if base:
        parts, target = urllib.parse.urlsplit(url), urllib.parse.urlsplit(base)
        url = urllib.parse.urlunsplit((target.scheme, target.netloc, parts.path, parts.query, ''))
    return url


def sandbox_guard(plan):
    """Fictional (test) applications never touch a real form."""
    if plan.get('sandbox') and not local_base():
        raise Stop('sandbox', 'This plan belongs to a fictional test application; it is filled only against the '
                   'local test form')


def previewed(ws, plan):
    """A preview of this exact plan that ended ready (the user saw its screenshot)."""
    base = application_dir(ws, plan) / 'ats'
    for report in sorted(base.glob('preview-*/fill-report.json')) if base.is_dir() else []:
        try:
            data = read_json(report)
        except ValueError:
            continue
        if data.get('plan_sha256') == plan['plan_sha256'] and data.get('status') == 'ready':
            return _rel(ws, report)
    return None


def check_approval(ws, plan, approval_id):
    if approvals.policy_mode(ws) != 'submit-with-approval':
        raise Stop('approval_invalid', 'Policy mode is not submit-with-approval; nothing can be submitted',
                   EXIT_APPROVAL)
    try:
        return approvals.verify(ws, approval_id, 'ats_submit', approval_payload(plan))
    except (ValueError, OSError) as error:
        raise Stop('approval_invalid', 'Approval check failed: %s' % error, EXIT_APPROVAL)


def _summary(results):
    return [{k: r[k] for k in ('field', 'status', 'readback') if k in r} for r in results]


BLOCKING_OK = {'filled', 'manual', 'skipped_direct_upload', 'absent_optional'}


def blocking(results):
    return [r['field'] for r in results
            if r['status'] not in BLOCKING_OK or (r['status'] == 'filled' and r.get('readback') != 'match')]


# ------------------------------------------------------------------ browser (Playwright, imported lazily)
def _css_id(value):
    return '[id="%s"]' % str(value).replace('\\', '\\\\').replace('"', '\\"')


def _playwright():
    try:
        from playwright.sync_api import Error, TimeoutError as Timeout, sync_playwright
    except ImportError:
        raise Stop('needs_setup', 'Playwright is not installed: run `uv run SKILL/scripts/ats/greenhouse.py ...` '
                   '(installs it from the script header) or `pip install playwright==1.64.0`; Google Chrome must '
                   'be installed', EXIT_ERROR)
    return sync_playwright, Error, Timeout


@contextlib.contextmanager
def browser_page(ws, profile=None):
    """Real Google Chrome, visible. Ephemeral by default; --profile ats keeps logins in the workspace."""
    sync_playwright, _, _ = _playwright()
    headless = test_mode() and os.environ.get('JOBFATHER_ATS_HEADLESS') == '1'
    viewport = {'width': 1280, 'height': 900}
    with sync_playwright() as pw:
        browser = None
        if profile:
            if profile not in BROWSER_PROFILES:
                raise ValueError('Unknown browser profile')
            directory = contained(ws, BROWSER_PROFILES[profile])
            directory.mkdir(parents=True, exist_ok=True)
            context = pw.chromium.launch_persistent_context(str(directory), channel='chrome', headless=headless,
                                                            viewport=viewport)
        else:
            browser = pw.chromium.launch(channel='chrome', headless=headless)
            context = browser.new_context(viewport=viewport)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(15000)
            yield page
        finally:
            for closer in (context.close, browser.close if browser else None):
                try:
                    if closer:
                        closer()
                except Exception:
                    pass


def _body_text(page):
    try:
        return page.inner_text('body', timeout=5000)
    except Exception:
        return ''


def open_form(page, url):
    _, _, Timeout = _playwright()
    page.goto(url, wait_until='domcontentloaded', timeout=45000)
    try:
        page.wait_for_selector('#application-form', timeout=20000)
    except Timeout:
        if 'error=true' in page.url or CLOSED.search(_body_text(page)):
            raise Stop('closed', 'The job page says the job is closed', EXIT_CLOSED)
        raise Stop('form_not_found', 'No Greenhouse application form on the page; fill it by hand (guided filling)')
    if 'error=true' in page.url:
        raise Stop('closed', 'The job page says the job is closed', EXIT_CLOSED)


def _fill_text(page, action, mode):
    box = page.locator(_css_id(action['dom_id']))
    if not box.count():
        return 'absent', {}
    if not box.first.is_visible():
        return 'hidden', {}
    if (box.first.get_attribute('role') or '') == 'combobox':
        # e.g. the API's text "location" is a search-as-you-type list on the hosted form: pick an exact option
        action['control'] = 'select'
        return _fill_select(page, action, mode)
    box.first.fill(action['value'])
    return 'filled', {}


def _read_text(page, action):
    shown = page.locator(_css_id(action['dom_id'])).first.input_value()
    if action['field'] == 'phone':
        return _digits(shown) == _digits(action['value'])
    return shown == action['value']


def _menu(page, action):
    return page.locator(_css_id('react-select-%s-listbox' % action['dom_id'])).locator('[role="option"]')


def _pick(page, box, action, label):
    """Type the label, then click the one option whose text matches it exactly. Never Arrow/Enter."""
    _, _, Timeout = _playwright()
    box.click()
    box.fill('')
    if label is not None:
        box.press_sequentially(label, delay=15)
    options = _menu(page, action)
    try:
        options.first.wait_for(state='visible', timeout=8000 if action.get('async_options') else 4000)
    except Timeout:
        box.press('Escape')
        return 'option_not_found', [], None
    if action.get('async_options'):
        page.wait_for_timeout(1200)  # let search-as-you-type results settle on the full text
    texts = [_clean(t) for t in options.all_inner_texts()]
    if label is None:  # decline, with options only the page knows
        hits = [i for i, t in enumerate(texts) if DECLINE.match(option_key(t))]
        missing = 'no_decline_option'
    else:
        hits = [i for i, t in enumerate(texts) if same_option(t, label, action.get('match') == 'dial_code')]
        missing = 'option_not_found'
    if len(hits) != 1:
        box.press('Escape')
        return (missing if not hits else 'ambiguous_option'), texts[:60], None
    chosen = texts[hits[0]]
    options.nth(hits[0]).click()
    return 'filled', [], chosen


def _fill_select(page, action, mode):
    box = page.locator(_css_id(action['dom_id']))
    if not box.count():
        return 'absent', {}
    box = box.first
    if not box.is_visible():
        return 'hidden', {}  # e.g. a follow-up question the page shows only after another answer
    if (box.get_attribute('role') or '') != 'combobox':
        if action['type'] in TEXT_LIMITS:
            action['control'] = 'text'
            box.fill(action['value'])
            return 'filled', {}
        return 'wrong_control', {}
    labels = action['value'] if isinstance(action['value'], list) else [action['value']]
    picked = []
    for label in labels:
        status, options, chosen = _pick(page, box, action, label)
        if status != 'filled':
            return status, ({'available_options': options} if options else {})
        picked.append(chosen)
    action['shown'] = picked
    return 'filled', {}


def _read_select(page, action):
    """The control must now show exactly the planned option(s) (or the one decline option picked)."""
    control = page.locator(_css_id(action['dom_id'])).first.locator(
        'xpath=ancestor::*[contains(concat(" ", normalize-space(@class), " "), " select__control ")][1]')
    shown = [_clean(t) for t in control.locator('.select__single-value, .select__multi-value__label').all_inner_texts()]
    if action['value'] is None:
        wanted = action.get('shown') or []
        ok = all(DECLINE.match(option_key(w)) for w in wanted)
    else:
        wanted = action['value'] if isinstance(action['value'], list) else [action['value']]
        ok = True
    dial = action.get('match') == 'dial_code'
    return ok and len(shown) == len(wanted) and all(any(same_option(s, w, dial) for s in shown) for w in wanted)


def _set_checked(page, box, element_id, wanted):
    try:
        box.set_checked(wanted, timeout=3000)
    except Exception:
        page.locator('label[for="%s"]' % element_id.replace('"', '\\"')).first.click()
    return box.is_checked() == wanted


def _fill_checkboxes(page, action, mode):
    found = 0
    for spec in action['checkboxes']:
        box = page.locator(_css_id(spec['id']))
        if not box.count():
            continue
        found += 1
        label = page.locator('label[for="%s"]' % spec['id'].replace('"', '\\"'))
        if not label.count() or option_key(label.first.inner_text()) != option_key(spec['label']):
            return 'label_mismatch', {}
        if box.first.is_checked() != spec['checked'] and not _set_checked(page, box.first, spec['id'], spec['checked']):
            return 'not_set', {}
    if not found:
        return 'absent', {}
    return ('filled', {}) if found == len(action['checkboxes']) else ('options_changed', {})


def _read_checkboxes(page, action):
    return all(page.locator(_css_id(s['id'])).first.is_checked() == s['checked'] for s in action['checkboxes'])


def _fill_file(page, action, mode):
    box = page.locator('input[type="file"]' + _css_id(action['dom_id']))
    if not box.count():
        return 'absent', {}
    group = page.locator('[aria-labelledby="upload-label-%s"]' % action['dom_id'])
    if mode == 'preview' and group.count() and (group.first.get_attribute('data-allow-s3') or '').lower() == 'true':
        return 'skipped_direct_upload', {}  # this board uploads on selection; attach only when submitting
    box.first.set_input_files(action['value'])
    page.wait_for_timeout(300)
    return 'filled', {}


def _read_file(page, action):
    box = page.locator('input[type="file"]' + _css_id(action['dom_id']))
    info = box.first.evaluate('e => e.files && e.files.length ? [e.files[0].name, e.files[0].size] : null') \
        if box.count() else None
    if info:
        return info[0] == action['file_name'] and info[1] == action['file_size']
    group = page.locator('[aria-labelledby="upload-label-%s"]' % action['dom_id'])
    return bool(group.count()) and action['file_name'] in group.first.inner_text()


def _single_boxes(page, action):
    return page.locator('input[type="checkbox"][name="%s"]' % action['field'].replace('"', '\\"'))


def _fill_untick(page, action, mode):
    """A GDPR box the user did not tick by hand stays (or becomes) unticked."""
    boxes = _single_boxes(page, action)
    if not boxes.count():
        return 'absent', {}
    for index in range(boxes.count()):
        box = boxes.nth(index)
        if box.is_checked() and not _set_checked(page, box, box.get_attribute('id') or '', False):
            return 'not_set', {}
    return 'filled', {}


def _read_untick(page, action):
    boxes = _single_boxes(page, action)
    return not any(boxes.nth(i).is_checked() for i in range(boxes.count()))


FILLERS = {'text': (_fill_text, _read_text), 'select': (_fill_select, _read_select),
           'multi_select': (_fill_select, _read_select), 'checkboxes': (_fill_checkboxes, _read_checkboxes),
           'file': (_fill_file, _read_file), 'checkbox': (_fill_untick, _read_untick)}

REQUIRED_SCAN = r"""
(covered) => {
  const form = document.querySelector('#application-form');
  if (!form) return [];
  const seen = new Set(), out = [];
  const visible = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  for (const e of form.querySelectorAll('input, textarea, select')) {
    if (e.type === 'hidden' || e.type === 'file' || e.getAttribute('aria-hidden') === 'true') continue;
    if (!(e.required || e.getAttribute('aria-required') === 'true')) continue;
    const key = (e.type === 'checkbox' || e.type === 'radio') ? e.name : e.id;
    if (!key || covered.includes(key) || seen.has(key)) continue;
    const box = (e.type === 'checkbox' || e.type === 'radio') ? null : e;
    if (box && !visible(box)) continue;
    let filled;
    if (e.getAttribute('role') === 'combobox') {
      const control = e.closest('.select__control');
      filled = !!(control && control.querySelector('.select__single-value, .select__multi-value'));
    } else if (e.type === 'checkbox' || e.type === 'radio') {
      filled = !!form.querySelector('input[name="' + CSS.escape(e.name) + '"]:checked');
    } else {
      filled = !!e.value;
    }
    seen.add(key);
    if (!filled) {
      const label = e.id ? document.querySelector('label[for="' + CSS.escape(e.id) + '"]') : null;
      out.push({dom_id: key, label: label ? label.innerText.trim().slice(0, 160) : ''});
    }
  }
  return out;
}
"""


def fill_form(page, actions, mode):
    """Fill every planned control, then read each back. Returns per-field results (no values)."""
    _, Error, _ = _playwright()
    results = []
    for action in actions:
        result = {'field': action['field']}
        if action.get('manual'):
            result['status'] = 'manual'  # the user fills it by hand in the visible window
        elif action['control'] == 'checkbox' and not action.get('untick'):
            result['status'] = 'manual'  # GDPR consent boxes: only the user ticks them
        else:
            filler, _ = FILLERS[action['control']]
            try:
                status, extra = filler(page, action, mode)
            except Error as error:
                status, extra = 'error', {'error': str(error).splitlines()[0][:200]}
            if status in ('absent', 'hidden') and (action.get('dom_only') or action.get('conditional')
                                                   or action.get('untick')):
                status = 'absent_optional'
            if status == 'no_decline_option' and not action['required']:
                status = 'absent_optional'
            result['status'] = status
            result.update(extra)
        result['control'] = action['control']
        results.append(result)
    page.wait_for_timeout(300)
    for action, result in zip(actions, results):
        if result['status'] == 'filled':
            _, reader = FILLERS[action['control']]
            try:
                result['readback'] = 'match' if reader(page, action) else 'mismatch'
            except Error:
                result['readback'] = 'mismatch'
    return results


def covered_ids(actions):
    ids = []
    for action in actions:
        ids.append(action['dom_id'])
        if action['control'] in ('checkboxes', 'checkbox'):
            ids.append(action['field'])
    return ids


def unplanned_required(page, actions):
    """Required controls the plan does not cover. A scan that fails blocks, it never passes."""
    try:
        found = page.evaluate(REQUIRED_SCAN, covered_ids(actions))
    except Exception:
        found = None
    if not isinstance(found, list):
        return [{'dom_id': 'required-field-scan', 'label': 'The form could not be checked for other required fields'}]
    return found


def _write_status(path, state, started, deadline=None, **extra):
    value = {'schema': STATUS_SCHEMA, 'state': state, 'heartbeat_at': now(), 'started_at': started,
             'pid': os.getpid()}
    if deadline is not None:
        value['seconds_left'] = max(0, int(deadline - time.monotonic()))
    value.update(extra)
    atomic_json(path, value)


def _confirmation_line(text):
    for line in (text or '').splitlines():
        if CONFIRM.search(line):
            return _clean(line)[:300]
    match = CONFIRM.search(text or '')
    return match.group(0) if match else ''


def await_user_submit(page, status_path, started, wait_seconds, simulate_user=None):
    """Poll for Greenhouse's confirmation while the user reviews and clicks Submit. Never clicks."""
    deadline = time.monotonic() + wait_seconds
    beat = 0.0
    hook_done = False
    while True:
        clock = time.monotonic()
        try:
            if page.is_closed():
                return 'browser_closed', None
            url = page.url
            form_gone = page.locator('#application-form button[type="submit"]').count() == 0
            on_confirmation = bool(re.search(r'/confirmation(?:/|$)', urllib.parse.urlsplit(url).path))
            text = _body_text(page) if form_gone or on_confirmation else ''
            if on_confirmation or (form_gone and CONFIRM.search(text)):
                return 'confirmed', {'url': url, 'text': _confirmation_line(text)}
            if clock - beat >= HEARTBEAT_SECONDS:
                code = page.locator(_css_id('security-input-0')).count() > 0
                _write_status(status_path, 'awaiting_security_code' if code else 'awaiting_user_submit', started,
                              deadline)
                beat = clock
        except Exception:
            if page.is_closed():
                return 'browser_closed', None
        if simulate_user and not hook_done:  # tests only: the test plays the user
            hook_done = True
            simulate_user(page)
        if clock >= deadline:
            return 'timeout', None
        try:
            page.wait_for_timeout(1500)
        except Exception:
            if page.is_closed():
                return 'browser_closed', None


# ------------------------------------------------------------------ commands
def questions(url, board=None, opener=None):
    return fetch(url, board, opener)


def validate(plan_path, ws=None, opener=None):
    ws = require_workspace(ws)
    plan, path = load_plan(ws, plan_path)
    fetched = fetch(plan['job']['url'], plan['job']['board'], opener)
    result = validate_plan(plan, fetched, ws, 'validate')
    digest_now = stamp(plan, path)
    status = 'ok' if result['ok'] else 'needs_input'
    return {'status': status, 'plan': str(path), 'plan_sha256': digest_now, 'problems': result['problems'],
            'fields': len(result['actions'])}


def preview(plan_path, ws=None, profile=None, opener=None):
    """Fill the form in a visible window, read every field back, screenshot it and close. Never submits."""
    ws = require_workspace(ws)
    plan, path = load_plan(ws, plan_path)
    sandbox_guard(plan)
    if not ats_settings(ws)['enabled']:
        raise Stop('disabled', 'Greenhouse filling is turned off in config/platforms.json')
    fetched = fetch(plan['job']['url'], plan['job']['board'], opener)
    result = validate_plan(plan, fetched, ws, 'preview')
    plan_sha = stamp(plan, path)
    if result['problems']:
        raise Stop('needs_input', 'The plan needs answers or fixes before filling',
                   fields=sorted({p['field'] for p in result['problems'] if p['field']}), problems=result['problems'])
    run_id, run = new_run(ws, plan, 'preview')
    report = {'schema': REPORT_SCHEMA, 'mode': 'preview', 'run_id': run_id, 'plan_sha256': plan_sha,
              'questions_sha256': fetched['questions_sha256'], 'started_at': now()}
    try:
        with browser_page(ws, profile) as page:
            open_form(page, form_url(plan))
            results = fill_form(page, result['actions'], 'preview')
            unplanned = unplanned_required(page, result['actions'])
            page.screenshot(path=str(run / 'review.png'), full_page=True)
    except Stop as stop:
        report.update(status=stop.status, finished_at=now())
        atomic_json(run / 'fill-report.json', report)
        raise
    blocked = blocking(results)
    report.update(status='ready' if not blocked and not unplanned else 'needs_input', fields=results,
                  unplanned_required=unplanned, screenshot=_rel(ws, run / 'review.png'), finished_at=now())
    if report['status'] == 'ready':
        report['approval_payload'] = approval_payload(plan)
        atomic_json(run / 'approval-payload.json', report['approval_payload'])
    atomic_json(run / 'fill-report.json', report)
    summary = {'status': report['status'], 'run_id': run_id, 'report': _rel(ws, run / 'fill-report.json'),
               'screenshot': report['screenshot'], 'fields': _summary(results),
               'unplanned_required': [u['dom_id'] for u in unplanned], 'plan_sha256': plan_sha}
    if report['status'] == 'ready':
        summary['approval_payload_file'] = _rel(ws, run / 'approval-payload.json')
    else:
        summary['blocking'] = blocked + [u['dom_id'] for u in unplanned]
    return summary


def _cv_output_id(ws, plan):
    resume = plan['files'].get('resume') or {}
    if resume.get('output_id'):
        return resume['output_id']
    try:
        record = Store(ws, test=plan.get('sandbox') is True).load(plan['application_id'])
    except (ValueError, OSError):
        return None
    return next((o['output_id'] for o in record.get('outputs', []) if o.get('kind') == 'cv-pdf'
                 and o.get('sha256') == resume.get('sha256')), None)


def submit(plan_path, approval_id, ws=None, profile=None, opener=None, wait_seconds=None, simulate_user=None):
    """Refill the approved plan in a visible window and wait for the user's own Submit click.

    simulate_user is a test seam (in-process only, JOBFATHER_TEST_MODE=1 and a loopback form).
    """
    if simulate_user is not None and not local_base():
        raise ValueError('simulate_user exists only for tests against the local form')
    ws = require_workspace(ws)
    plan, path = load_plan(ws, plan_path)
    started = now()
    if plan.get('plan_sha256') != plan_hash(plan):
        raise Stop('plan_changed', 'The plan changed after its preview; run preview again and ask for a new approval')
    sandbox_guard(plan)
    settings = ats_settings(ws)
    if not settings['enabled']:
        raise Stop('disabled', 'Greenhouse filling is turned off in config/platforms.json')
    if settings['submit_mode'] != 'user_click':
        raise Stop('unsupported_submit_mode', 'Only submit_mode "user_click" exists: the user clicks Submit')
    if not previewed(ws, plan):
        raise Stop('needs_preview', 'Run preview for this exact plan and show the user the screenshot first')
    record = check_approval(ws, plan, approval_id)
    fetched = fetch(plan['job']['url'], plan['job']['board'], opener)
    result = validate_plan(plan, fetched, ws, 'submit')
    if result['problems']:
        raise Stop('needs_input', 'The plan needs answers or approvals before submitting',
                   fields=sorted({p['field'] for p in result['problems'] if p['field']}), problems=result['problems'])
    if wait_seconds is None:
        wait_seconds = WAIT_SECONDS
        if test_mode() and os.environ.get('JOBFATHER_ATS_WAIT_SECONDS'):
            wait_seconds = int(os.environ['JOBFATHER_ATS_WAIT_SECONDS'])
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(shared_lock(contained(ws, SUBMIT_LOCK), timeout=0.5))
    except RuntimeError:
        raise Stop('busy', 'Another submission is in progress; one form at a time')
    with stack:
        with shared_lock(contained(ws, LEDGER_LOCK)):
            check_unused(ws, approval_id)
            check_pacing(ws, settings)
            run_id, run = new_run(ws, plan, 'submit')
            _ledger_event(ws, plan, 'submit_started', run_id, approval_id)
        status_path = run / 'status.json'
        _write_status(status_path, 'filling', started)
        outcome, seen, results, unplanned, stage = 'error', None, [], [], 'filling'
        try:
            with browser_page(ws, profile) as page:
                open_form(page, form_url(plan))
                results = fill_form(page, result['actions'], 'submit')
                unplanned = unplanned_required(page, result['actions'])
                if blocking(results) or unplanned:
                    outcome = 'aborted'
                    page.screenshot(path=str(run / 'mismatch.png'), full_page=True)
                    raise Stop('readback_mismatch', 'The filled form does not match the approved plan; nothing was '
                               'submitted', fields=blocking(results) + [u['dom_id'] for u in unplanned])
                page.screenshot(path=str(run / 'before-submit.png'), full_page=True)
                _write_status(status_path, 'awaiting_user_submit', started)
                waiting = ('%d minutes' % (wait_seconds // 60)) if wait_seconds >= 120 else ('%d seconds' % wait_seconds)
                sys.stderr.write('The form is filled in the Chrome window. Review it, complete any security code or '
                                 'CAPTCHA, and click Submit yourself. Waiting up to %s; nothing is submitted '
                                 'automatically.\n' % waiting)
                sys.stderr.flush()
                hook = simulate_user if simulate_user is not None and local_base() else None
                stage = 'awaiting'  # from here the user may have clicked Submit
                outcome, seen = await_user_submit(page, status_path, started, wait_seconds, hook)
                if outcome == 'confirmed':
                    page.screenshot(path=str(run / 'confirmation.png'), full_page=True)
                stage = 'done'
        except Stop as stop:
            if stop.status == 'closed':
                outcome = 'closed'
            report = {'schema': REPORT_SCHEMA, 'mode': 'submit', 'run_id': run_id, 'plan_sha256': plan['plan_sha256'],
                      'status': stop.status, 'fields': results, 'unplanned_required': unplanned,
                      'started_at': started, 'finished_at': now()}
            atomic_json(run / 'fill-report.json', report)
            _write_status(status_path, stop.status, started)
            _ledger_event(ws, plan, 'submit_aborted', run_id, approval_id, outcome=stop.status)
            raise
        except BaseException:
            _write_status(status_path, 'error', started)
            if stage == 'awaiting':  # interrupted while the user had the form: treat as possibly submitted
                with contextlib.suppress(Exception):
                    approvals.mark(ws, approval_id, 'cancelled', {'outcome': 'interrupted', 'run_id': run_id})
                _ledger_event(ws, plan, 'unconfirmed', run_id, approval_id, outcome='interrupted')
            else:
                _ledger_event(ws, plan, 'submit_aborted', run_id, approval_id, outcome='error')
            raise
        finished = now()
        atomic_json(run / 'fill-report.json', {'schema': REPORT_SCHEMA, 'mode': 'submit', 'run_id': run_id,
                                               'plan_sha256': plan['plan_sha256'], 'status': outcome,
                                               'fields': results, 'started_at': started, 'finished_at': finished})
        _write_status(status_path, outcome, started)
        if outcome == 'timeout':
            approvals.mark(ws, approval_id, 'expired', {'outcome': 'timeout', 'run_id': run_id})
            _ledger_event(ws, plan, 'unconfirmed', run_id, approval_id, outcome='timeout')
            raise Stop('timeout', 'No confirmation within the waiting time. Ask the user whether they submitted; if '
                       'they did, record it as user-reported. This approval is used up.', EXIT_TIMEOUT, run_id=run_id)
        if outcome == 'browser_closed':
            approvals.mark(ws, approval_id, 'cancelled', {'outcome': 'browser_closed', 'run_id': run_id})
            _ledger_event(ws, plan, 'unconfirmed', run_id, approval_id, outcome='browser_closed')
            raise Stop('browser_closed', 'The window closed before a confirmation appeared. Ask the user whether '
                       'they submitted; if they did, record it as user-reported. This approval is used up.',
                       run_id=run_id)
        confirmation = run / 'confirmation.png'
        approvals.mark(ws, approval_id, 'consumed', {'outcome': 'submitted', 'run_id': run_id})
        submission = {'schema': SUBMISSION_SCHEMA, 'channel': 'greenhouse', 'submitted_at': finished,
                      'authorized_by_user': True, 'user_request': record['user_statement'],
                      'evidence': {'kind': 'confirmation-page', 'url': seen['url'], 'quote': seen['text'],
                                   'capture_sha256': digest(confirmation)}}
        cv_output_id = _cv_output_id(ws, plan)
        if cv_output_id:
            submission['cv_output_id'] = cv_output_id
        receipt = {'schema': RECEIPT_SCHEMA, 'ats': 'greenhouse',
                   'job': {k: plan['job'].get(k) for k in ('board', 'job_id', 'url', 'title', 'company')},
                   'application_id': plan['application_id'], 'approval_id': approval_id,
                   'plan_sha256': plan['plan_sha256'], 'run_id': run_id, 'submitted_by': 'user_click',
                   'observed_confirmation': {'text': seen['text'], 'url': seen['url']},
                   'screenshots': [_rel(ws, run / 'before-submit.png'), _rel(ws, confirmation)],
                   'started_at': started, 'finished_at': finished, 'submission': submission}
        atomic_json(run / 'receipt.json', receipt)
        atomic_json(run / 'submission.json', submission)
        _ledger_event(ws, plan, 'submitted', run_id, approval_id)
    return {'status': 'submitted', 'run_id': run_id, 'receipt': _rel(ws, run / 'receipt.json'),
            'submission': _rel(ws, run / 'submission.json'), 'fields': _summary(results)}


def _template_summary(plan, path):
    answers = plan['answers']
    return {'status': 'ok', 'plan': str(path), 'plan_sha256': plan['plan_sha256'],
            'prefilled': [{'field': a['field'], 'source': a['source']} for a in answers if a['value'] is not None],
            'needs_input': [{'field': a['field'], 'kind': a['kind']} for a in answers
                            if a['value'] is None and a['required']],
            'consent': [a['field'] for a in answers if a['kind'] == 'consent'],
            'optional_unanswered': plan['unanswered_optional'],
            'self_identification': {'mode': plan['self_identification']['mode'],
                                    'questions': [q['qkey'] for q in plan['self_identification']['questions']]},
            'files': {k: bool(v) for k, v in plan['files'].items()}}  # file names often carry the user's name


def main(argv=None):
    parser = add_workspace_arg(argparse.ArgumentParser(description=__doc__.splitlines()[0]))
    sub = parser.add_subparsers(dest='command', required=True)
    child = sub.add_parser('fetch', help="the job's questions as JSON")
    child.add_argument('--url', required=True)
    child.add_argument('--board', help='board token, when the link does not carry it (gh_jid links)')
    child.add_argument('--out', type=Path, help='also write the questions to this file')
    child = sub.add_parser('plan-template', help='write an answer-plan skeleton for one application')
    child.add_argument('--url', required=True)
    child.add_argument('--application-id', required=True)
    child.add_argument('--board')
    child.add_argument('--resume', help='workspace PDF (default: the application\'s latest validated CV)')
    child.add_argument('--cover-letter', help='workspace PDF')
    child.add_argument('--out', help='plan path inside the workspace (default: the application\'s ats/ folder)')
    child.add_argument('--force', action='store_true', help='replace an existing plan')
    child.add_argument('--test', action='store_true', help='a fictional application in the sandbox')
    child = sub.add_parser('validate', help='check a plan without a browser')
    child.add_argument('--plan', required=True)
    child = sub.add_parser('preview', help='fill, read back and screenshot; never submits')
    child.add_argument('--plan', required=True)
    child.add_argument('--profile', choices=sorted(BROWSER_PROFILES))
    child = sub.add_parser('submit', help='refill and wait for the user to click Submit')
    child.add_argument('--plan', required=True)
    child.add_argument('--approval', required=True)
    child.add_argument('--profile', choices=sorted(BROWSER_PROFILES))
    args = parser.parse_args(argv)
    code = EXIT_OK
    try:
        if args.command == 'fetch':
            result = questions(args.url, args.board)
            if args.out:
                atomic_json(args.out, result)
        elif args.command == 'plan-template':
            plan, path = plan_template(args.url, args.application_id, args.workspace, args.test, args.resume,
                                       args.cover_letter, args.board, args.out, args.force)
            result = _template_summary(plan, path)
        elif args.command == 'validate':
            result = validate(args.plan, args.workspace)
        elif args.command == 'preview':
            result = preview(args.plan, args.workspace, args.profile)
        else:
            result = submit(args.plan, args.approval, args.workspace, args.profile)
        if result.get('status') == 'needs_input':
            code = EXIT_STOP
    except Stop as stop:
        result, code = stop.as_dict(), stop.code
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
        result, code = {'status': 'error', 'message': str(error)}, EXIT_ERROR
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == '__main__':
    sys.exit(main())
