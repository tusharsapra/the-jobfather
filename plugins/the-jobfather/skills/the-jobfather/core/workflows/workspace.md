# Application records

`python3 SKILL/scripts/applications.py --help` lists operations. Pass the real host with
`--platform`. Records live in the workspace under `jobs/applications/APP-…/`.

- **Create** one record per application: `create --company C --role R --jd-file FILE --source S
  --platform HOST`. Keep the returned id; repeat applications get new records. `show ID`, `jd ID`
  and `analysis ID` read stored data. `revise-jd ID --file FILE --source S --platform HOST` keeps
  the old JD and invalidates the analysis.
- **Analysis:** `save-analysis ID --file ANALYSIS.json --platform HOST --context-revision REV`
  (schema in fit.md). Use the revision from the task read; never fetch a new one to bless stale work.
- **Drafts:** write the final text to a file plus a review JSON (`facts_preserved: true`,
  `voice_checked: true`, `context_revision`, short notes) after really reviewing. Then
  `save-output ID --kind KIND --file FILE --review-file REVIEW --platform HOST --evidence ID…
  [--max-chars N --max-words N --one-line --exclude-spaces]`. Kinds: fit, forms, cover-letter,
  linkedin, email, follow-up, humanize, cv-content, cv-review, application-pack. Outgoing text is
  checked for limits, placeholders, claim IDs and phrases the user avoids.
- **Drafts without a JD:** `python3 SKILL/scripts/drafts.py --kind KIND --file FILE --review-file
  REVIEW --platform HOST`.
- **Events** (only real, supplied facts): `event ID --file EVENT.json --platform HOST` with
  `type` (interaction, status, local-preference, accepted), `text`, `basis` (`user-reported`,
  `authorized-send-result`, `connector-verified` with full mailbox evidence, or
  `authorized-submission` with confirmation evidence), optional `status`, `next_action`,
  `contacts`, `local_preferences`, `output_id`. Generating or approving never sets `submitted`.
- **Mail draft receipts:** `record-draft ID OUTPUT_ID --file RECEIPT.json --platform HOST`
  (the draft must be verified and unsent; attachments must match a validated CV).
- **Paths:** CV PDFs are stored workspace-relative (`path_root: workspace`); `output ID OUTPUT_ID`
  resolves and checksum-checks any output.
- **Fictional fixtures** use `--test` on every command; they live under `.state/sandbox/` and
  never mix with real records.

Writes take a lock and replace files atomically; an interrupted write never shows up as a
finished output. Company-specific emphasis goes in `local_preferences` (update-status.md for an
audited saved preference), never in the global profile.
