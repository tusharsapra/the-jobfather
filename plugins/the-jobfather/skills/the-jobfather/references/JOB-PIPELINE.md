# Pipeline: triage, research, outreach records and mail tracking

`scripts/job_pipeline.py` ranks saved jobs, caches company research and tracks replies and
follow-ups. `scripts/outreach.py` records what was drafted, sent, bounced or deleted. Neither reads
mail, calls a model, sends anything or submits applications: the host does the reading and judging
and these scripts validate and store it. The ranking, mail-sync and outcome-tracking concepts are
adapted from MadsLorentzen/ai-job-search (MIT). Every command takes `--workspace PATH` and
`--test` (fictional data under `.state/sandbox/`).

## State

| File | Owner | Holds |
| --- | --- | --- |
| `jobs/registry.json` | job_intake.py | Jobs, stages, the user's notes (JOB-INTAKE.md) |
| `jobs/outreach.json` | outreach.py | Outreach records |
| `jobs/pipeline/state.json` | job_pipeline.py | Priorities, research, committed message signals, pending matches, handled actions, the completed-sync cursor |

Locks are taken in one order: pipeline, then an application, then the jobs folder (registry and
outreach). A message is first written to the pipeline state as `prepared`, then applied to the
application record (and, for a bounce, to the outreach record), then marked `committed`; an
interrupted run is finished by the next `reconcile` or `export`. Event IDs derive from the real
application and message IDs, so a replay never duplicates history, and a replay with different
content is refused.

## Settings (`config.json` `pipeline`)

`rank_limit` 10, `top` 5, `research_days` 30, `follow_up_days` 10, `max_follow_ups` 2,
`sync_limit` 50, `overlap_days` 2. Optional `exclude_stages` (default Drafted, Applied,
Interviewing, Offer, Rejected, Withdrawn, Closed, Skipped) and `contact_stages` (default Drafted)
adapt ranking to custom stage names. The timezone is `identity.json` `locale.timezone`; the mailbox
is `identity.json` `mail.account` (no default: mail tracking refuses without it).

## Ranking (`candidates`, `job`, `save-priority`, `brief`)

`candidates [--limit N] [--focus TEXT] [--include-sent]` lists jobs that are new or changed since
their last triage. Excluded: stages in `exclude_stages`; jobs with a drafted, sent or bounced
outreach record; jobs whose application is past draft; known past deadlines.
`--include-sent` brings back contacted jobs (never applied ones) and never authorizes a repeat
send. Jobs without a readable full JD are listed under `needs_readable_jd` (blocked is unverified,
not closed). `job --id JOB-…` returns the stored JD.

`save-priority --file F --platform HOST`:

```json
{"job_id": "JOB-…", "priority": "Strong", "job_sha256": "from candidates", "jd_sha256": "from candidates",
 "context_hashes": {"profile/EXPERIENCE.md": "from candidates", "…": "…"},
 "strengths": ["A concrete, supported match"], "gaps": [], "recommendation": "Short next step",
 "requirements": [{"quote": "exact JD text", "assessment": "supported", "critical": true, "evidence_ids": ["ABC-01"]}]}
```

Priorities: Strong, Review, Stretch, Skip. Quotes must occur in the stored JD; evidence IDs must be
current EXPERIENCE claims; a supported requirement needs evidence; Strong cannot hide a critical
gap. Hashes cover the job's facts and the triage context (identity, profile, experience, HR details,
market map): a change in any of them asks for reassessment, a VOICE-only edit does not. No scores.

`brief` returns reply actions, follow-ups due, unresolved matches and the top valid priorities.
Ties after priority follow `market.json` `scoring.tie_breaks` (default `priority`, `closing_soon`
(a deadline within 7 days), `location_rank` (`geography.locations` rank), `network` (a referral
outreach for the job or company), `published_contact` (the listing names an email), `job_id`).
Unknown names are ignored with a warning. The brief is a local snapshot, not a mailbox check.

## Company research (`save-research`, `research`)

`save-research --file F --platform HOST` takes `company`, `website` and `findings`: objects with
`kind` (`company`, `careers`, `contact`), `summary`, source `url`, the exact source `text`, and
optional `email` or `name` that must appear literally in that text. The cache key is the company
or brand name plus the website host. `research --company C --website URL` returns the capture and
whether it is fresh (`research_days`).

## Outreach records (`outreach.py`)

```json
{"outreach_id": "OUT-…", "created_at": "…", "channel": "email", "company": "…", "recipient": "…",
 "roles": ["…"], "job_ids": ["JOB-…"], "application_id": "APP-… or null",
 "outreach_type": "application|enquiry|follow_up|referral", "subject": "…",
 "status": "drafted|sent|bounced|draft-deleted|cancelled",
 "draft": {"provider": "gmail", "draft_id": "…", "message_id": "…", "thread_id": "…",
           "receipt": "workspace-relative path or null", "receipt_sha256": "…"},
 "sent": {"message_id": "…", "thread_id": "…", "sent_at": "…", "url": "…", "capture_sha256": "…"},
 "next_action": null, "follow_up_override": null, "notes": null, "history": [{"at": "…", "action": "drafted"}]}
```

| Command | Input |
| --- | --- |
| `record-drafted --file F --platform HOST` | `{"application_draft": {"application_id": "APP-…", "output_id": "…"}}` after `applications.py record-draft` (everything else is read from the receipt and cross-checked), or the full details plus `"draft": {"provider", "draft_id", "message_id", "thread_id"}` read back from the mailbox |
| `record-sent --file F --platform HOST` | `{"outreach_id": "OUT-…", "evidence": {…}}`, or the full details plus `evidence` for a message sent without a recorded draft |
| `record-deleted --outreach-id OUT-… [--cancelled] [--basis user-reported\|connector-verified] [--note T]` | A draft deleted (or an outreach cancelled) before sending |
| `set --outreach-id OUT-… [--next-action T] [--follow-up-date YYYY-MM-DD\|ISO] [--notes T]` | The user's fields; an empty value clears one |
| `list [--company C] [--recipient R] [--job-id J] [--status S] [--type T]`, `show --outreach-id OUT-…` | Read |

`record-drafted` is idempotent on provider + draft ID (a revised draft updates the record; a sent,
deleted or cancelled draft cannot be re-recorded) and warns about a bounced inbox, a deleted draft
or an earlier send for the same job. Sent `evidence` needs exactly `provider`, `message_id`,
`thread_id`, `sent_at` (with a timezone), `url` (the provider's own https link), `sender` (must be
`mail.account`), `recipients` (must include the record's recipient), `labels` (must include `SENT`,
must not include `DRAFT`) and `capture_sha256`. Only a drafted record can become sent.

## Mail tracking (`sync-plan`, `reconcile`, `resolve-action`)

`sync-plan [--at T]` returns bounded queries over saved recipients, companies and ATS sender domains
(`market.json` `ats_senders`, plus mailer-daemon and postmaster), with `after:`/`before:` as epoch
seconds so the window is exact in any timezone: from the last complete sync minus `overlap_days`
(or 30 days back the first time) to a fixed `capture_until`. It lists already-recorded and pending
message IDs. Search all mail including archived; separate Sent queries verify the user's own
follow-ups and replies.

`reconcile --file F --platform HOST` takes `{"messages": [...]}`, at most `sync_limit` per batch:

```json
{"message_id": "real id", "thread_id": "real thread id", "occurred_at": "2026-10-13T10:00:00-04:00",
 "sender": "recruiter@example.com", "recipients": ["you@example.com"], "subject": "Actual subject",
 "body": "The current message only, without quoted history", "direction": "incoming",
 "signal": "hr_question", "quote": "Exact supporting text from the body", "action": "Optional short task",
 "url": "optional; defaults to https://mail.google.com/mail/?authuser=<account>#all/<message_id>",
 "labels": ["…"], "original_message_id": "optional", "requisition_id": "optional",
 "promised_at": "optional", "promise_quote": "optional", "promise_note": "optional"}
```

Signals: acknowledgement, hr_question, assessment, interview, offer, rejection, bounce,
follow_up, sent_reply, other_reply, noise. Incoming messages must be addressed to `mail.account`;
`follow_up` and `sent_reply` must be outgoing from it with the `SENT` label. The quote must occur in
the body; a `requisition_id` must occur in the body; `promised_at` needs a `promise_quote`;
unclear timing goes in `promise_note`. Any other field (tokens, raw headers) is refused.

Matching: a verified related message ID beats the thread, the thread beats the contact, and a new
thread from an ATS needs the exact company and role. A requisition ID narrows the match. A grouped
enquiry never decides several roles. Unmatched, ambiguous and contradictory messages stay pending
and are retried. `noise` keeps only the ID and a checksum.

After every query page of the window is done, reconcile an empty batch with `--window-complete
--completed-through CAPTURE_UNTIL`; until then the cursor does not move. `resolve-action
--message-id ID --platform HOST` clears a reply action the user handled elsewhere without
recording a send.

## Outcome rules

- Generated is not sent; sent is not submitted; a submission needs its own evidence (JOB-INTAKE.md).
- SENT is not delivered. A bounce marks the outreach `bounced` and stops follow-ups to that inbox;
  it never closes the job or changes the application.
- A deleted draft is never a send. An assessment is not an interview. An offer is not an
  acceptance. A received time is not the application date.
- An acknowledgement can move a matched draft application to submitted; interviews, offers and
  rejections change only a uniquely matched application, and never reopen a withdrawn, rejected or
  offered one (those stay pending for review).
- Follow-ups fall due `follow_up_days` after the last verified contact (a sent message or a
  recorded reply), at most `max_follow_ups` per thread, one per thread. A promised date pushes the
  date out; the user's follow-up date (a calendar date counts from local midnight) replaces it;
  an unclear promise or an open reply action holds it. A new draft never resets the clock.
  Bounced inboxes, deleted or cancelled drafts, resolved applications and offers get none.

## Projection and export

`projection [--at T] [--output F]` returns the outreach summary and committed signals. `export`
refreshes the tracker workbook (JOB-INTAKE.md): replays interrupted writes, then brings Stage,
Notes and the editable outreach columns back from the workbook before writing it.
