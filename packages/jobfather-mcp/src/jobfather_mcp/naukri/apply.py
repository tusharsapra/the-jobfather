"""Applying on Naukri: per-item approval, the user's click on a confirm tab, then one Apply click.

``prepare`` reads the job (no outward action) and records an approval bound to
``{job_id, answers_sha256, flow_id}``. ``apply`` verifies that approval, checks pacing and caps,
shows the confirm tab in the server's own Chrome window and waits for the user's click. Only on
"Submit application" does it click Naukri's Apply button (or fill the approved answers of an open
questionnaire and press its submit button). The approval is consumed with the package's locked
``consume`` once the outcome is known. A questionnaire is never answered without approved answers
and is never previewed by clicking Apply.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
import time
from datetime import timedelta
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .. import approvals
from ..errors import JFError
from . import parse, reads, store
from .session import Session
from .site import JobRef

KIND = 'naukri_apply'
APPROVAL_TTL_MIN = 15
FLOW_TTL_S = 1800
OUTCOME_WAIT_S = 25
NO_ANSWERS = approvals.payload_hash([])


def answers_hash(answers: Sequence[Mapping[str, Any]]) -> str:
    normal = sorted(({'qid': a['qid'], 'value': a['value']} for a in answers), key=lambda a: a['qid'])
    return approvals.payload_hash(normal) if normal else NO_ANSWERS


def payload(job_id: str, answers_sha256: str, flow_id: str | None) -> dict[str, Any]:
    return {'job_id': job_id, 'answers_sha256': answers_sha256, 'flow_id': flow_id}


def summary_of(detail: Mapping[str, Any]) -> dict[str, Any]:
    keys = ('job_id', 'url', 'title', 'company', 'locations', 'experience', 'salary', 'skills', 'posted', 'work_mode',
            'apply_type', 'already_applied', 'snippet')
    return {k: detail.get(k) for k in keys}


# ====================================================================== page actions (Playwright)
class PageActions:
    """What the server does on a Naukri job page. Replaced by a fake in offline tests."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.site = session.site
        self.spec = session.site.section('apply')

    async def _button(self, page: Any, regex: str) -> Any:
        pattern = re.compile(regex, re.I)
        locator = page.get_by_role('button', name=pattern)
        with contextlib.suppress(Exception):
            if await locator.filter(visible=True).count():
                return locator.filter(visible=True).first
        return None

    async def state(self, page: Any, detail: Mapping[str, Any]) -> str:
        """'already_applied', 'external_apply', 'closed' or 'ready' for an open job page."""
        if detail.get('already_applied') is True:
            return 'already_applied'
        if await self.session.visible(page, self.spec.get('already_applied_selectors', [])) or \
                await self._button(page, self.spec['already_applied_regex']) is not None:
            return 'already_applied'
        if detail.get('apply_type') == 'company_site' or \
                await self._button(page, self.spec['company_site_name_regex']) is not None:
            return 'external_apply'
        closed = self.site.section('job').get('closed_text_regex')
        if closed:
            with contextlib.suppress(Exception):
                if re.search(closed, await page.inner_text('body', timeout=5000), re.I):
                    return 'closed'
        if await self.apply_button(page) is None:
            return 'closed'
        return 'ready'

    async def apply_button(self, page: Any) -> Any:
        locator = page.locator(self.spec['apply_button'])
        with contextlib.suppress(Exception):
            if await locator.filter(visible=True).count():
                return locator.filter(visible=True).first
        return await self._button(page, self.spec['apply_button_name_regex'])

    async def snapshot(self, page: Any) -> dict[str, Any]:
        text = ''
        with contextlib.suppress(Exception):
            text = await page.inner_text('body', timeout=5000)
        return {'success': len(re.findall(self.spec['success_text_regex'], text, re.I)),
                'applied': await self.session.visible(page, self.spec.get('already_applied_selectors', [])),
                'dialog': await self._questionnaire(page) is not None}

    async def _questionnaire(self, page: Any) -> Any:
        for selector in self.spec['questionnaire']['containers']:
            locator = page.locator(selector).filter(visible=True)
            with contextlib.suppress(Exception):
                count = await locator.count()
                for index in range(count):
                    item = locator.nth(index)
                    if await item.locator('input:not([type=hidden]), textarea, select, [contenteditable]').count():
                        return item
        return None

    async def click_apply(self, page: Any) -> None:
        button = await self.apply_button(page)
        if button is None:
            raise JFError('closed', 'The Apply button is no longer on the job page.', False)
        await button.click()

    async def outcome(self, page: Any, before: Mapping[str, Any], known_qids: set[str] = frozenset(),
                      wait_s: float = OUTCOME_WAIT_S) -> dict[str, Any]:
        """Watch the page after a submit: applied, questions, login, error or unknown."""
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            if page.is_closed():
                return {'kind': 'unknown', 'text': 'The Naukri tab was closed.', 'url': None}
            found = await self.session.checkpoint_on(page, None)
            if found is not None:
                raise await self.session.stop_at(page, found)
            if self.site.is_login_url(page.url):
                return {'kind': 'login', 'text': None, 'url': page.url}
            text = ''
            with contextlib.suppress(Exception):
                text = await page.inner_text('body', timeout=5000)
            matches = list(re.finditer(self.spec['success_text_regex'], text, re.I))
            if len(matches) > before.get('success', 0):
                start = max(0, matches[-1].start() - 80)
                return {'kind': 'applied', 'text': parse.clean(text[start:matches[-1].end() + 160], 400),
                        'url': page.url}
            if not before.get('applied') and await self.session.visible(page, self.spec.get('already_applied_selectors', [])):
                return {'kind': 'applied', 'text': 'Naukri now shows the job as applied.', 'url': page.url}
            container = await self._questionnaire(page)
            if container is not None:
                questions = parse.questions_from_dom(await container.inner_html(), self.site)
                fresh = [q for q in questions if q['qid'] not in known_qids]
                if fresh:
                    return {'kind': 'questions', 'questions': questions, 'url': page.url}
        return {'kind': 'unknown', 'text': None, 'url': page.url}

    async def fill(self, page: Any, questions: Sequence[Mapping[str, Any]], answers: Mapping[str, Any]) -> None:
        container = await self._questionnaire(page)
        if container is None:
            raise JFError('flow_expired', 'The questionnaire is no longer open on the Naukri page.', False,
                          user_action='Prepare the application again from the start.')
        for question in questions:
            if question['qid'] not in answers:
                continue
            value = answers[question['qid']]
            where = question['_locator']
            kind = where.get('kind')
            if kind in ('radio', 'checkbox'):
                inputs = container.locator('input[type="%s"][name="%s"]' % (kind, where.get('name', '').replace('"', '')))
                wanted = value if isinstance(value, list) else [value]
                if question['type'] == 'boolean':
                    box = inputs.first
                    await (box.check() if value else box.uncheck())
                    continue
                for index in range(await inputs.count()):
                    box = inputs.nth(index)
                    label = await self._label(container, box)
                    if label in wanted:
                        await self._check(container, box)
                continue
            target = self._control(container, where)
            text = str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
            if kind == 'select':
                values = value if isinstance(value, list) else [value]
                await target.select_option(label=values)
            elif kind == 'contenteditable':
                await target.click()
                await target.fill(text)
            else:
                await target.fill(text)

    def _control(self, container: Any, where: Mapping[str, Any]) -> Any:
        tag = {'contenteditable': '[contenteditable]'}.get(where.get('kind'), where.get('kind') or 'input')
        if where.get('id'):
            return container.locator('%s[id="%s"]' % (tag, where['id'].replace('"', ''))).first
        if where.get('name'):
            return container.locator('%s[name="%s"]' % (tag, where['name'].replace('"', ''))).first
        return container.locator(tag).first

    async def _label(self, container: Any, box: Any) -> str:
        ident = await box.get_attribute('id')
        if ident:
            label = container.locator('label[for="%s"]' % ident.replace('"', ''))
            if await label.count():
                return parse.clean(await label.first.inner_text())
        aria = await box.get_attribute('aria-label')
        return parse.clean(aria or await box.get_attribute('value') or '')

    async def _check(self, container: Any, box: Any) -> None:
        if await box.is_visible():
            await box.check()
            return
        ident = await box.get_attribute('id')
        if ident:
            await container.locator('label[for="%s"]' % ident.replace('"', '')).first.click()

    async def submit(self, page: Any) -> None:
        container = await self._questionnaire(page)
        if container is None:
            raise JFError('flow_expired', 'The questionnaire is no longer open on the Naukri page.', False)
        button = container.get_by_role('button', name=re.compile(self.spec['questionnaire']['submit_name_regex'], re.I))
        if not await button.filter(visible=True).count():
            raise JFError('site_changed', 'No submit button in the questionnaire (site profile %s).'
                          % self.site.version, False)
        await button.filter(visible=True).first.click()


# ====================================================================== orchestration
class Applier:
    def __init__(self, session: Session, actions: Any | None = None) -> None:
        self.session = session
        self.ws = session.ws
        self.actions = actions or getattr(session, 'page_actions', None) or PageActions(session)

    # ------------------------------------------------------------------ flows
    def _flow(self, flow_id: str, job_id: str) -> dict[str, Any]:
        record = store.read_flow(self.ws, flow_id)
        live = self.session.flows.get(flow_id)
        if record is None or live is None or live['page'].is_closed() or \
                time.monotonic() - live['created'] > FLOW_TTL_S or record.get('status') not in ('needs_input', 'prepared'):
            if record is not None and record.get('status') in ('needs_input', 'prepared'):
                record['status'] = 'expired'
                store.write_flow(self.ws, record)
            self.session.flows.pop(flow_id, None)
            raise JFError('flow_expired', 'That questionnaire is no longer open (the window was closed, the server '
                          'restarted, or it expired).', False,
                          user_action='Call naukri_prepare_apply again without flow_id to start over.')
        if record.get('job_id') != job_id:
            raise JFError('invalid_argument', 'That flow belongs to another job.', False)
        return record

    async def resume_used(self) -> dict[str, Any]:
        try:
            summary, _ = await reads.profile(self.session, use_cache=True)
        except JFError as error:
            if error.code in ('checkpoint', 'blocked', 'rate_limited'):
                raise
            return {'source': 'naukri_profile', 'file_name': None, 'uploaded_on': None}
        resume = summary.get('resume') or {}
        return {'source': 'naukri_profile', 'file_name': resume.get('file_name'), 'uploaded_on': resume.get('uploaded_on')}

    # ------------------------------------------------------------------ prepare
    async def prepare(self, ref: JobRef, statement: str, answers: list[dict[str, Any]] | None,
                      flow_id: str | None) -> dict[str, Any]:
        if answers and not flow_id:
            raise JFError('invalid_argument', 'Answers belong to a questionnaire: pass the flow_id that naukri_apply '
                          'returned with needs_input.', False)
        if flow_id:
            record = self._flow(flow_id, ref.job_id)
            questions = record['questions']
            given = [{'qid': a['qid'], 'value': int(a['value']) if isinstance(a['value'], float) and
                      a['value'].is_integer() else a['value']} for a in (answers or [])]
            problems = parse.check_answers(questions, given)
            if problems:
                raise JFError('invalid_argument', 'Answers do not fit the questions: ' + '; '.join(problems[:8]), False,
                              user_action='Ask the user for the missing or invalid answers; select answers must be '
                                          'one of the option labels exactly.')
            digest = answers_hash(given)
            record.update({'answers': [{'qid': a['qid'], 'value': a['value']} for a in given],
                           'answers_sha256': digest, 'status': 'prepared'})
            store.write_flow(self.ws, record)
            self.session.flows[flow_id]['answers'] = {a['qid']: a['value'] for a in given}
            job = record['job']
            status = 'ready'
            external = None
            resume = record.get('resume_used') or await self.resume_used()
        else:
            loaded, detail, _ = await self._read(ref, actions=['job'])
            job = summary_of(detail)
            status = await self.actions.state(loaded.page, detail)
            external = detail.get('external_apply_url') if status == 'external_apply' else None
            digest = NO_ANSWERS
            resume = await self.resume_used() if status == 'ready' else {'source': 'naukri_profile',
                                                                       'file_name': None, 'uploaded_on': None}
        approval = None
        if status == 'ready':
            subject = {'platform': 'naukri', 'job_id': ref.job_id, 'title': job.get('title'),
                       'company': job.get('company'), 'url': job.get('url')}
            record = approvals.create(self.ws, KIND, subject, payload(ref.job_id, digest, flow_id), statement,
                                      'jobfather-naukri', ttl_minutes=APPROVAL_TTL_MIN)
            approval = {'approval_id': record['approval_id'], 'expires_at': record['expires_at'],
                        'binds': payload(ref.job_id, digest, flow_id),
                        'summary_for_user': self._summary(job, resume, flow_id)}
        return {'status': status, 'job': job, 'external_apply_url': external, 'resume_used': resume,
                'approval': approval}

    def _summary(self, job: Mapping[str, Any], resume: Mapping[str, Any], flow_id: str | None) -> str:
        where = ', '.join(job.get('locations') or []) or 'location not stated'
        text = 'Apply on Naukri to "%s" at %s (%s, job %s) with the resume on your Naukri profile%s.' % (
            job.get('title') or 'untitled job', job.get('company') or 'an unnamed company', where, job.get('job_id'),
            ' (%s)' % resume['file_name'] if resume.get('file_name') else '')
        if flow_id:
            record = store.read_flow(self.ws, flow_id) or {}
            labels = {q['qid']: q['label'] for q in record.get('questions', [])}
            parts = ['%s: %s' % (labels.get(a['qid'], a['qid']),
                                 ', '.join(a['value']) if isinstance(a['value'], list) else a['value'])
                     for a in record.get('answers', [])]
            text += ' Answers: ' + ('; '.join(parts) if parts else 'none') + '.'
        return text + ' After you approve here, a confirm page opens in the Naukri window; nothing is sent until ' \
                      'you click Submit application there.'

    def _cancel_if_pending(self, approval_id: str, detail: Mapping[str, Any]) -> None:
        """Cancel only an approval nobody has claimed (another process may be using it right now)."""
        with contextlib.suppress(JFError):
            with approvals._exclusive(self.ws):
                if approvals.load(self.ws, approval_id)['status'] == 'pending':
                    approvals.mark(self.ws, approval_id, 'cancelled', detail)

    async def _read(self, ref: JobRef, actions: list[str]) -> tuple[Any, dict[str, Any], str]:
        try:
            loaded, detail, method = await reads.read_job(self.session, ref, actions=actions)
        except reads.JobGone:
            raise JFError('closed', 'Naukri no longer shows this job; it cannot be applied to.', False) from None
        if reads.logged_in_on_job_page(loaded, self.session.site) is False:
            store.set_auth(self.ws, 'no', 'job page shows a logged-out visitor')
            raise JFError('auth_required', 'Naukri shows this page logged out.', False,
                          user_action='Call naukri_login; the user signs in in the Naukri window.')
        return loaded, detail, method

    # ------------------------------------------------------------------ apply
    async def apply(self, ref: JobRef, approval_id: str | None, flow_id: str | None, timeout_s: float,
                    tick: Callable[[float], Awaitable[None]] | None = None) -> dict[str, Any]:
        base = {'job_id': ref.job_id, 'approval_id': approval_id or '', 'flow_id': flow_id, 'questions': None,
                'evidence': None, 'message': None}
        if not approval_id:
            approvals.verify(self.ws, None, KIND, {})  # raises approval_required
        if flow_id:
            record = self._flow(flow_id, ref.job_id)
            if not record.get('answers_sha256'):
                raise JFError('approval_required', 'Prepare the answers first: naukri_prepare_apply with the job, the '
                              "user's words, the approved answers and this flow_id.", False)
            bound = payload(ref.job_id, record['answers_sha256'], flow_id)
        else:
            record = None
            bound = payload(ref.job_id, NO_ANSWERS, None)
        approvals.verify(self.ws, approval_id, KIND, bound)
        limiter = self.session.limiter
        if flow_id is None:
            await limiter.wait_allowed(['apply'])
            loaded, detail, _ = await self._read(ref, actions=['job'])
            page = loaded.page
            state = await self.actions.state(page, detail)
            if state in ('already_applied', 'external_apply'):
                return dict(base, status=state, message='Nothing was submitted.')
            if state == 'closed':
                raise JFError('closed', 'The job no longer accepts applications on Naukri.', False)
            job = summary_of(detail)
            resume = await self.resume_used()
            shown_answers: list[dict[str, Any]] = []
        else:
            live = self.session.flows[flow_id]
            page = live['page']
            job = record['job']
            resume = record.get('resume_used') or {'file_name': None}
            labels = {q['qid']: q['label'] for q in record['questions']}
            shown_answers = [{'label': labels.get(a['qid'], a['qid']), 'value': a['value']} for a in record['answers']]
        approval = approvals.load(self.ws, approval_id)
        decision = await self.session.confirm({
            'title': job.get('title'), 'company': job.get('company'), 'job_id': ref.job_id, 'url': job.get('url'),
            'resume': 'The resume on your Naukri profile' + (' (%s)' % resume['file_name'] if resume.get('file_name')
                                                            else ''),
            'approval': '%s, valid until %s' % (approval_id, approval['expires_at']), 'answers': shown_answers,
        }, timeout_s, tick)
        if decision == 'cancel':
            self._cancel_if_pending(approval_id, {'by': 'user on the confirm page'})
            return dict(base, status='cancelled', message='The user cancelled on the confirm page; nothing was sent.')
        if decision != 'submit':
            return dict(base, status='confirmation_timeout',
                        message='No click on the confirm page in time; nothing was sent. The approval stays valid '
                                'until it expires, so naukri_apply can be called again.')
        # The click is claimed atomically: if another process already used this approval, nothing is clicked.
        approvals.claim(self.ws, approval_id, KIND, bound, {'by': 'user click on the confirm page',
                                                            'at': store.iso(store.now_utc())})
        if flow_id is None:
            limiter.record(['apply'])
        with contextlib.suppress(Exception):
            await page.bring_to_front()
        before = await self.actions.snapshot(page)
        if flow_id is None:
            await self.actions.click_apply(page)
            outcome = await self.actions.outcome(page, before)
        else:
            questions = self.session.flows[flow_id]['questions']
            await self.actions.fill(page, questions, self.session.flows[flow_id]['answers'])
            await self.actions.submit(page)
            outcome = await self.actions.outcome(page, before, known_qids={q['qid'] for q in questions})
        return await self._finish(ref, approval_id, bound, flow_id, record, job, resume, page, outcome, base)

    async def _finish(self, ref: JobRef, approval_id: str, bound: dict[str, Any], flow_id: str | None,
                      record: dict[str, Any] | None, job: Mapping[str, Any], resume: Mapping[str, Any], page: Any,
                      outcome: Mapping[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        kind = outcome['kind']
        observed_at = store.iso(store.now_utc())
        if kind == 'applied':
            approvals.consume(self.ws, approval_id, KIND, bound, {'outcome': 'applied', 'flow_id': flow_id})
            shot = await self.session.screenshot(page, '%s-%s.png' % (ref.job_id, store.now_utc().strftime('%Y%m%dT%H%M%SZ')))
            if flow_id:
                record['status'] = 'applied'
                store.write_flow(self.ws, record)
                self.session.flows.pop(flow_id, None)
            return dict(base, status='applied', evidence={'observed_text': outcome.get('text'), 'url': outcome.get('url'),
                                                          'screenshot': shot, 'observed_at': observed_at},
                        message='Naukri confirmed the application.')
        if kind == 'questions':
            approvals.consume(self.ws, approval_id, KIND, bound, {'outcome': 'needs_input'})
            questions = outcome['questions']
            new_id = flow_id or 'flw_' + secrets.token_hex(8)
            if flow_id is None:
                self.session.detach_main_page()
            self.session.flows[new_id] = {'page': page, 'questions': questions, 'answers': None,
                                          'created': time.monotonic()}
            store.write_flow(self.ws, {
                'flow_id': new_id, 'job_id': ref.job_id, 'status': 'needs_input', 'job': dict(job),
                'resume_used': dict(resume), 'questions': parse.public_questions(questions), 'answers': None,
                'answers_sha256': None, 'created_at': observed_at,
                'expires_at': store.iso(store.now_utc() + timedelta(seconds=FLOW_TTL_S))})
            return dict(base, status='needs_input', flow_id=new_id, questions=parse.public_questions(questions),
                        message='Naukri asks screening questions. Nothing has been answered yet. Get the user\'s '
                                'answers (option labels exactly), then call naukri_prepare_apply with them and this '
                                'flow_id, and naukri_apply again.')
        if kind == 'login':
            with contextlib.suppress(JFError):
                approvals.consume(self.ws, approval_id, KIND, bound, {'outcome': 'login required, nothing sent'})
            store.set_auth(self.ws, 'no', 'redirected to the login page while applying')
            raise JFError('auth_required', 'Naukri asked for a login when Apply was clicked; nothing was submitted.',
                          False, user_action='Call naukri_login, then prepare the application again.')
        approvals.consume(self.ws, approval_id, KIND, bound, {'outcome': 'unconfirmed'})
        shot = await self.session.screenshot(page, '%s-%s-unconfirmed.png'
                                             % (ref.job_id, store.now_utc().strftime('%Y%m%dT%H%M%SZ')))
        return dict(base, status='failed', evidence={'observed_text': outcome.get('text'), 'url': outcome.get('url'),
                                                     'screenshot': shot, 'observed_at': observed_at},
                    message='Apply was clicked but Naukri showed no confirmation the server recognises. Ask the user '
                            'to check the Naukri window (and "Applied jobs") before trying again; the approval was '
                            'used.')
