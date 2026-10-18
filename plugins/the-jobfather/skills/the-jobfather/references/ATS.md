# Applicant tracking systems (ATS)

Company application forms run on a handful of systems. `scripts/ats/classify.py` names the system
behind a link; Greenhouse forms are filled by `scripts/ats/greenhouse.py`; every other system gets
guided filling by the host under the same rules. In every case **the user clicks Submit**.

## Routing

`python3 SKILL/scripts/ats/classify.py --url URL` (or `--file LINKS.txt`, one link per line) prints
`{url, ats, board, job_id, apply_url, supported_autofill}`. It is offline; `--resolve` follows short
links (`grnh.se`, `lnkd.in`, `bit.ly`, …) with HEAD requests only.

| ats | recognised by | ids |
| --- | --- | --- |
| greenhouse | `greenhouse.io`, `grnh.se`, `?gh_jid=` on a careers page | board + job id from `boards.`/`job-boards.` (incl. `.eu.`) `/<board>/jobs/<id>`, `embed/job_app?for=<board>&token=<id>`; `gh_jid` gives the job id only (pass `--board` later) |
| lever | `lever.co` | `jobs.lever.co/<site>/<uuid>` |
| ashby | `ashbyhq.com`, `?ashby_jid=` | `jobs.ashbyhq.com/<name>/<uuid>` |
| workday, smartrecruiters, icims, keka, darwinbox, workable | their hosts (`myworkdayjobs.com`, `myworkdaysite.com`, `apply.workable.com`, …) | none |
| google_forms | `docs.google.com/forms`, `forms.gle` | none |
| linkedin, naukri, iimjobs, internshala | their hosts (`lnkd.in` too) | none: use their own workflows |
| email | `mailto:`, a bare address, or a job record with only an email | none |
| company_site | anything else | none |

`supported_autofill` is true only for US-hosted Greenhouse boards in v1 (EU-hosted boards have no
verified question API yet). For Greenhouse, `apply_url` is the embeddable form
(`job-boards.greenhouse.io/embed/job_app?for=…&token=…`): it shows the form even when the hosted job
page redirects to the company's careers site.

## Greenhouse flow

1. `ats/greenhouse.py fetch --url URL [--board TOKEN]`: the job's questions from Greenhouse's public
   job-board API (`?questions=true`, no login), normalised to
   `{qkey, label, required, type, options, group, kind}` plus `questions_sha256`. Groups: main,
   location, compliance (EEOC), demographic. Kinds: identity, contact, resume, cover_letter, links,
   work_auth, sponsorship, relocation, salary, notice, consent, eeo, demographic, custom. Controls
   that only the hosted form shows are added (phone country, "Are you Hispanic/Latino?", school and
   degree, GDPR consent boxes). A job the board no longer lists stops with `closed` (exit 4).
2. `plan-template --url URL --application-id APP-… [--resume PDF] [--cover-letter PDF] [--test]`
   writes `jobs/applications/<APP>/ats/plan-greenhouse-<board>-<job>.json` (sandbox for `--test`).
   It prefills only from the workspace, each value with its `source`: identity.json
   (`profile.identity.email`, `…first_name`, `…links.linkedin`, `…country`), HR-DETAILS.json values
   that carry `confirmed_at` (`profile.hr.notice_period`, …) and FORM-ANSWERS.json answers whose
   question matches exactly (`profile.form_answers.<key>`). The resume defaults to the application's
   latest validated CV. Everything else is `null`; nothing is guessed.
3. The host fills the rest **with the user**: ask for every required `null`, every consent and the
   self-identification choice. Set `value` and `source` (`user:<note>` for the user's words,
   `draft:<output id>` for a reviewed open-text draft), then `approved: true` on each answer the
   user confirmed. `"manual": true` (with `value: null`) leaves a field for the user to fill by hand
   in the visible window. `validate --plan PLAN` checks the plan without a browser.
4. `preview --plan PLAN [--profile ats]`: re-fetches the questions (stops with `questions_changed`
   if they moved), validates, opens real Chrome, fills, reads every field back, saves
   `fill-report.json`, `review.png` and `approval-payload.json` under
   `jobs/applications/<APP>/ats/<run_id>/`, and closes. It never submits. `--profile ats` keeps
   logins in `.state/browser/ats`; the default is a fresh profile. Show the user the screenshot.
5. Policy mode `submit-with-approval`, then the user's approval of this exact application:
   `approvals.py create --kind ats_submit --payload-file <run>/approval-payload.json --statement
   "<their words>"`. The payload is `{plan_sha256, resume_sha256, url}`: any later edit to the plan,
   the PDF or the form address invalidates it.
6. `submit --plan PLAN --approval APV-ID`: needs a ready preview of the same plan, verifies the
   approval, re-fetches and re-validates, refills in a visible window and checks the read-back
   exactly. Then **the user** reviews the form, types any emailed security code, completes any
   CAPTCHA and clicks Submit. The script waits up to 15 minutes (heartbeat in `status.json`) for
   Greenhouse's confirmation (a `/confirmation` address, or "Thank you for applying" /
   "Application submitted" once the form is gone), marks the approval consumed and writes
   `receipt.json` and `submission.json`. There is no mode in which the script clicks Submit.

Exit codes: 0 ok, 2 needs input or stopped (`needs_input`, `questions_changed`, `paced`,
`daily_cap`, `readback_mismatch`, `browser_closed`, …), 3 approval invalid, 4 job closed, 5 timed
out waiting for the user, 1 error. Stdout carries field names, statuses and file locations only,
never answer values.

### Recording the submission

`submission.json` is exactly the job-intake submission record (`the-jobfather/submission@1`):

```json
{"schema": "the-jobfather/submission@1", "channel": "greenhouse",
 "submitted_at": "2026-10-18T14:03:12+00:00", "authorized_by_user": true,
 "user_request": "<the approval's statement>",
 "evidence": {"kind": "confirmation-page", "url": "<confirmation page>", "quote": "<confirmation text>",
              "capture_sha256": "<sha256 of confirmation.png>"},
 "cv_output_id": "<the application's cv-pdf output, when the resume is one>"}
```

`receipt.json` holds the same object under `submission`, plus the ATS details (`ats`, `job`,
`approval_id`, `plan_sha256`, `run_id`, `submitted_by: "user_click"`,
`observed_confirmation{text,url}`, `screenshots`, `started_at`, `finished_at`). Record it with
`job_intake.py record-submission --job-id JOB-… --application-id APP-… --file <run>/receipt.json
--platform HOST`: it reads the `submission` member and links the approval (`submission.json` alone
works too, without the link). After a timeout or a closed window the approval is used up: ask the
user whether they submitted and, if they did, record it as `user-reported`. An approval whose run
started and never recorded an end (the process was killed while the user had the form) cannot
start another run either.

## Safety rules (enforced, not advisory)

- **Unknown stays unknown.** A required question without an answer stops the run with
  `needs_input`. No "Yes" defaults, no first-option fallback. After filling, any required control
  on the page that the plan does not cover also stops the run.
- **Exact options only.** Values match an option after Unicode normalisation, casefolding and
  whitespace collapsing, nothing looser. Lists that search as you type get the label typed and the
  one `[role=option]` with that exact text clicked; never ArrowDown/Enter. The phone country list
  may add a dialling code ("Canada +1" matches "Canada").
- **Consent** is ticked only for a consent question whose answer has a `user:` source,
  `approved: true` and `consent_text_sha256` equal to the hash of its `consent_text` (label,
  description and options, as shown in the plan). GDPR boxes, whose wording exists only on the page,
  are ticked only by the user (`manual`); a box the page pre-ticks is cleared.
- **Self-identification** (EEO and demographic questions, including custom gender or pronoun
  questions) selects the decline option unless `self_identification.mode` is `answers` and the
  user gave the answer (`source: "user:…"`). A required one without a decline option stops.
- **Work authorisation, sponsorship, relocation, salary and notice** answers come only from
  `profile.*` or `user:*` sources, never the job description or a draft. Profile-sourced values
  are re-derived and must match: a yes/no is mapped only when the question names the country the
  answer was confirmed for (or the job's location does), asks it the plain way round ("Will you
  require sponsorship", not "able to work without sponsorship") and offers exactly Yes/No.
- **Uploads** are PDFs inside the workspace (no traversal, no symlinks) whose sha256 still matches
  the plan. A board that uploads files on selection gets them only when submitting.
- **Read-back** must match exactly (phone numbers by digits) before the user is asked to submit.
  If the page cannot be scanned for other required controls, the run stops.
- **Pacing:** at least 60 seconds between submissions and at most `ats.greenhouse.daily_cap`
  (config/platforms.json, default 15) per local day, counted in `.state/ats/ledger.jsonl`
  (unconfirmed runs count). One submission at a time.
- Fictional (`--test`) applications are filled only against the local test form, never a real site.
- No stealth: real Chrome, visible, default automation flags, no login or code handling. Security
  codes and CAPTCHAs are typed by the user in the window, never asked for in chat or read from files.

## Other systems: guided filling

Lever, Ashby, Workday, iCIMS, SmartRecruiters, Workable, Keka, Darwinbox, Google Forms and company
sites have no script yet. The host fills them with the browser server (real Chrome) following the
same rules: prepare the answers as a plan first (values with sources, from the workspace or the
user), stop and ask at any unknown required question, choose options by exact text, tick consent
only with the user's approval of that wording, decline self-identification by default, attach only
workspace PDFs, show the filled form, get the approval record, and let the user click Submit.
Record the result with `record-submission` (`evidence.kind` `confirmation-page` with the page's
text and address, or `user-reported`).

## Tests

`tests/test_greenhouse.py` fills `tests/fixtures/greenhouse/form.html`, a local imitation of the
hosted form, served on 127.0.0.1. These variables are honoured only with `JOBFATHER_TEST_MODE=1`:
`JOBFATHER_GREENHOUSE_BASE` (a loopback address standing in for Greenhouse),
`JOBFATHER_ATS_HEADLESS=1` and `JOBFATHER_ATS_WAIT_SECONDS`.
