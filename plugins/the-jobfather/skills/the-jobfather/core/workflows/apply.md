# Applying

"Apply" covers four channels. In every one: the JD is read in full, fit is checked (fit.md), the CV
and answers are prepared first, and nothing is submitted without (1) policy mode
`submit-with-approval` (update-status.md), (2) the user's approval of that exact application,
recorded with `python3 SKILL/scripts/approvals.py create --kind … --statement "<their words>"`,
and (3) a human-only final step where the tool offers one. In `drafts-only` mode, prepare
everything and hand over the link and the files.

## Company forms on an applicant tracking system
1. `python3 SKILL/scripts/ats/classify.py --url URL` names the system.
2. **Greenhouse** (autofill supported): `ats/greenhouse.py fetch --url URL` → `plan-template` →
   fill the answer plan from the profile and ask the user for every unknown required answer,
   consent box and self-identification choice → user approves the plan → `preview --plan PLAN`
   (fills, reads back, screenshots; never submits) → show the screenshot → approval record (kind
   `ats_submit`) → `submit --plan PLAN --approval ID`: the form is refilled in a visible window and
   **the user clicks Submit** (and types any security code). The script waits for the
   confirmation, then writes a receipt.
3. **Other systems** (Lever, Ashby, Workday, iCIMS, SmartRecruiters, Google Forms…): guided filling
   with the browser server under the same rules: unknown required question → stop and ask; exact
   option matches only; consent only when approved; self-identification declines unless the user
   chose otherwise; uploads only workspace PDFs; the user clicks the final Submit.
4. Record the result: `job_intake.py record-submission --job-id … --application-id … --file
   RECEIPT.json --platform HOST` (evidence: confirmation page text and URL).

## Naukri (opt-in)
`naukri_prepare_apply` → show the summary (job, resume used: the one already on the user's Naukri
profile, answers) → the user approves → `naukri_apply` with the approval id opens a confirm page
in the Naukri window; **the user clicks Submit there**. Screening questions come back as
`needs_input`: answer from the profile, ask for unknowns, then prepare again with the answers.
Never call `naukri_apply` to preview. `already_applied` is not a new application.

## LinkedIn
Open the job's own application flow in the browser only when the user asks to apply. Never claim
Easy Apply was submitted unless the page confirms it after the user's own click.

## Email applications
Write with email.md. Sending needs `send-with-approval` or higher and an `email_send` approval for
that exact email.

## After any submission
Record it (`record-submission` or an application event with `authorized-submission` evidence),
update the tracker stage, and set the follow-up date. Daily caps and pacing apply (one form at a
time, at least a minute apart). Stop at any CAPTCHA, checkpoint or block and hand over.
