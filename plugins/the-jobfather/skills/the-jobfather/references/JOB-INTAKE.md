# Job intake, stages, submissions and the tracker

`scripts/job_intake.py` keeps the job registry. Captured listings are data: intake never drafts,
sends or applies. Every command takes `--workspace PATH` and `--test` (fictional data under
`.state/sandbox/`), before or after the command name.

| Command | Does |
| --- | --- |
| `ingest --file BATCH.json --platform HOST` | Validate the whole batch, then save it |
| `show [--job-id JOB-…]` | The registry, or one job |
| `init` | Create an empty registry |
| `set-stage --job-id JOB-… --stage NAME` | The user's stage (one of `config.json` `tracker.stages`) |
| `set-notes --job-id JOB-… --text TEXT` | The user's own notes (`""` clears them; 10,000 characters max) |
| `record-submission --job-id JOB-… --application-id APP-… --file S.json --platform HOST` | Record an authorized, evidenced submission |
| `history [--job-id JOB-…]` | The row-change journal |
| `export` | Refresh `jobs/outputs/Tracker.xlsx` (same as `tracker.py export`) |

`set-stage`, `set-notes`, `init` and `export` also accept `--platform HOST` (default `unknown`).

## Batch input

Stage the batch under `<workspace>/.state/intake/`. It is `{"jobs": [...]}`; `boards.py` output
(which may add a `report` list) is accepted as is. One object per opening. Unknown fields are
refused; omit unknown values or use `null`. Scalar fields take text or a finite number; prefer the
source's wording for pay, experience and dates. `full_jd` and `notes` keep every character.

| Group | Fields |
| --- | --- |
| Employer and opening | `company`, `role`, `location`, `work_mode`, `employment_type`, `experience`, `salary`, `requisition_id` |
| Contact | `recruiter_name`, `recruiter_company`, `recruiter_title`, `email`, `phone`, `recruiter_linkedin`, `post_author` |
| Description | `jd_summary`, `full_jd`, `skills`, `responsibilities`, `requirements`, `benefits` |
| Schedule and dates | `shift`, `working_days`, `joining_timeline`, `posted_date`, `deadline` |
| Applying | `application_url`, `application_method`, `notes` (what the listing or the host noted) |

Structured fields:

- `sources` (required, non-empty): objects with `type` (`screenshot`, `linkedin`, `url`, `text`,
  `document`), `label` (required), `access` (required: `readable`, `partial`, `blocked`) and at
  least one of `url` (absolute http(s), no credentials), `text` (exactly what was read) or `file`
  (a local path; relative paths resolve against the batch file's folder; 50 MB max).
- `additional_contacts`: objects with only `name`, `email`, `phone`, `linkedin`, `role`. Keep each
  person's details together; never pair an address with a guessed name.
- `uncertain_fields`: field name → short reason. A doubtful value stays out of its field.
- `workflow_status`: a configured stage (default: the first stage, usually `New`).
- `application_id`: an existing `APP-…` record; intake never creates applications.
- `job_id`: an existing `JOB-…` for deliberate enrichment.

A readable source needs a role or JD text; a partial or blocked source may be saved with unknown
fields. The store adds hashes, capture times, the platform and history; do not supply them.

```json
{"jobs": [{
  "company": "Example Corp", "role": "Data Analyst", "location": "City", "email": "jobs@example.com",
  "jd_summary": "SQL reporting and dashboards.",
  "full_jd": "Example Corp is hiring a Data Analyst ... Apply at jobs@example.com.",
  "sources": [{"type": "text", "label": "Pasted listing",
               "text": "Example Corp is hiring a Data Analyst ... Apply at jobs@example.com.", "access": "readable"}]
}]}
```

## Identity, duplicates and conflicts

- The whole batch is validated before anything is written; one bad row saves nothing.
- URLs are compared without tracking parameters (`utm_*`, click IDs, LinkedIn tracking). LinkedIn
  posts compare by activity ID and LinkedIn jobs by job ID, whatever the URL form.
- A row is the same listing only when company, role, location and requisition all match AND a
  source matches (same normalized URL, text or file hash). Different roles in one post stay
  separate jobs. The same company, role and location from a different source becomes a separate
  job flagged as a possible duplicate (both rows `needs_review`).
- Exact repeats change nothing. Enrichment fills empty fields only; a different value is kept as a
  conflict (`conflicts`: field, existing, incoming) and the row needs review. Stage and the user's
  notes are never changed by intake.

## Storage and history

| Path (in the workspace) | Holds |
| --- | --- |
| `jobs/registry.json` | Every job (`schema_version` 1) |
| `jobs/sources/` | Immutable source captures (`<source_key>.json`) and supplied files (`<sha256>.<ext>`) |
| `jobs/history/rows/<job_id>/<sha256>.json` | Every committed version of a row, named by its hash |
| `jobs/history/journal.jsonl` | One line per row change: action, platform, before/after hashes, registry hash |

Each read checks every source capture against its hash. Edits made outside the tools (or a write
interrupted after the registry was saved) are logged as `observed` on the next write.

## Submissions (`the-jobfather/submission@1`)

```json
{"schema": "the-jobfather/submission@1", "channel": "greenhouse",
 "submitted_at": "2026-10-03T09:00:00-04:00", "authorized_by_user": true,
 "user_request": "The user's own words asking to submit",
 "evidence": {"kind": "confirmation-page", "url": "https://…", "quote": "Thank you for applying"},
 "cv_output_id": "optional: a validated cv-pdf output of the application",
 "approval_id": "optional: the approvals.py record"}
```

- `submitted_at` needs a timezone and cannot be in the future. `authorized_by_user` must be `true`.
- `evidence.kind`: `confirmation-page`, `confirmation-email`, `sent-email` or `user-reported`.
  Anything but `user-reported` needs a `url`, `quote` or `message_id`; email kinds need
  `message_id`. Optional `capture_sha256` is the SHA-256 of the captured confirmation.
- Effect: an `application-submitted` history entry on the job, the job linked to the application,
  the stage moved to `Applied` unless it is already further along in the configured order, and an
  `authorized-submission` status event on the application (status `submitted`) unless the
  application is already at interview, offer, rejected or withdrawn. Repeating the same receipt
  changes nothing. A job linked to a different application is refused.

## The tracker workbook

`jobs/outputs/Tracker.xlsx` (`--test`: `.state/sandbox/jobs/outputs/Tracker.xlsx`) has four sheets:
**Jobs** (33 columns, one row per job), **Job Details** (full texts in numbered parts of at most
1,000 characters and 22 lines), **Outreach** (one row per outreach record) and **Activity** (one row
per recorded mail message; rows are only added). Row 2 is the title, row 3 states the timezone,
row 5 holds the headers, panes are frozen at D6, and every sheet is an Excel table.

Editable: Jobs **Stage** (drop-down from `tracker.stages`) and **Notes**; Outreach **Follow-up date
(editable)**, **Next action (editable)** and **Notes (editable)**. Each export compares every kept
cell with the value the last export wrote (`.state/tracker/export-base.json`):

| Workbook | Records | Result |
| --- | --- | --- |
| unchanged | changed | the records' value is written |
| changed | unchanged | an editable cell is saved into the records (`set-stage`, the job's notes, `outreach.py set`); another cell keeps the user's correction (`kept_edits`) |
| changed | changed | the workbook keeps its value, the records stay as they are, the receipt lists a conflict until one side matches the other |

Derived columns (sources, review state, IDs, outreach status and mail columns) always show the
records. Text is always stored as text, so nothing from a listing becomes a formula; a formula
typed into an editable cell is not saved (`rejected_edits`). Characters XML cannot hold are removed
and listed in `sanitized_cells`. Your own columns to the right of the managed ones, your own sheets
and rows whose ID is no longer in the records are left alone; a deleted row comes back
(`restored_rows`); set its Stage to Skipped or Closed instead.

The export refuses, changing nothing, when the workbook is open (`~$Tracker.xlsx` or a LibreOffice
lock file exists), contains drawings, charts, images, cell notes, pivot tables, slicers, threaded
comments or macros, has a renamed or moved managed header, a row without an ID, or two rows with
the same ID. It saves to a temporary file, reopens and checks it, replaces the workbook only if the
workbook did not change meanwhile, and keeps the previous version as `Tracker.xlsx.bak`.

Excel work needs openpyxl: `tracker.py` runs `tracker_xlsx.py` with `uv run --script` when uv is
installed (it fetches openpyxl itself), otherwise with the current Python if openpyxl is importable.
Without either, the export saves `.state/tracker/projection.json` and says how to install them; the
registry is never affected. If an export fails after a successful intake, retry the export only.
