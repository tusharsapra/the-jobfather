# Priorities, research, reply tracking and the action brief

Use for "rank my jobs", company research, tracking recruiter replies, follow-ups due and "what
needs my attention?". Chat is the interface; the user does not need to open the spreadsheet.
Nothing here sends, applies or polls in the background. The pipeline concepts (rank, sync,
outcome tracking) are adapted from MadsLorentzen/ai-job-search (MIT).

`python3 SKILL/scripts/job_pipeline.py --help`; payload schemas in `references/JOB-PIPELINE.md`.

## Rank saved jobs
1. `job_pipeline.py candidates [--limit N] [--focus TEXT]` returns up to ten new or changed
   eligible jobs (already contacted or applied jobs are excluded unless `--include-sent`).
2. Read PROFILE, the relevant EXPERIENCE sections and needed HR fields once. `job_pipeline.py job
   --id JOB-…` gives each stored JD. A title or snippet is not a JD; a blocked page is not a
   closed job.
3. Judge each with fit.md: **Strong** (critical requirements supported), **Review** (information
   or practical constraints to check), **Stretch** (real skill or seniority gaps), **Skip** (clear
   mismatch or the user's exclusion). Show practical gaps (notice, location, authorization, pay
   floor, shifts). No percentages.
4. Save each with `save-priority --file FILE --platform HOST` using the hashes from step 1.
5. `brief` returns the top valid priorities, ordered by priority, closing date, location rank
   (market.json) and the user's tie-breaks.

## Company research
`research --company C --website URL` returns the cached, source-backed research (30 days).
Refresh with `save-research --file FILE --platform HOST` after reading real sources. Keep parent
groups and hiring brands apart; contact names and emails need literal source evidence.

## Track replies (mail connector)
Use only the connected mail tools; never send, label, archive, delete or draft while tracking.
1. `sync-plan` gives bounded queries (saved contacts, companies, ATS sender domains) with a fixed
   capture-until time and already-recorded ids. Search metadata first, skip known ids, read at
   most the configured number of new messages.
2. Classify each message (acknowledgement, HR question, assessment, interview, offer, rejection,
   bounce, the user's own reply or follow-up) from the current message only, with an exact quote,
   real message and thread ids and the received time.
3. `reconcile --file FILE --platform HOST`. Exact thread or requisition evidence beats name
   similarity; shared-inbox messages never decide several jobs; unclear matches stay pending.
4. Only after every page of the window is done: `reconcile --window-complete --completed-through
   CAPTURE_UNTIL` with an empty batch. A cap, error or missing body keeps the cursor unchanged.
5. `export` refreshes the tracker. Meanings: an assessment is not an interview; an offer is not an
   acceptance; a bounce is not a closed job; a received time is not the application date.

## The brief and follow-ups
`brief` → a short list: replies needing action, follow-ups due, top opportunities, unresolved
matches. Omit empty sections and internal ids; say plainly if a check was incomplete.
Follow-ups are due 10 days after the last verified real contact, at most two per thread, unless a
promised date or the user's follow-up date says otherwise. Drafting them goes through
follow-up.md and email.md. If the user handled one elsewhere: `resolve-action --message-id ID
--platform HOST`.
