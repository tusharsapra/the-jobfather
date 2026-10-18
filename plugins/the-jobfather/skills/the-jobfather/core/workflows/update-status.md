# Saved updates, status, history and undo

Ordinary drafts and "looks good" stay in application records; they do not change the profile.
An explicit request authorizes a saved change: "remember this", "update my profile", "save this as
my default", "never say X again", "I don't have Y", "add Z as a target", "switch to sending mode".
Company-specific feedback stays with that application unless the user says it is general. Ask
only about consequential ambiguity, and never twice for the same authorized update.

## Which file owns what

| Kind | File | Change type |
| --- | --- | --- |
| Career facts, claims, CV bullets | profile/EXPERIENCE.md (+ re-rendered CV master) | factual-correction |
| Name, contacts, links, locale, mail account | profile/identity.json (+ CV master) | factual-correction |
| Changing values: notice, pay, availability, authorization | profile/HR-DETAILS.json (new kinds under `custom`) | factual-correction |
| Answers to recurring form questions | profile/FORM-ANSWERS.json | form-answer |
| Writing choices and examples | profile/VOICE.md | preference / example |
| Targets, titles, keywords, companies, exclusions | profile/market.json + MARKET.md | market |
| Summary and index | profile/PROFILE.md | factual-correction |
| Stories | profile/STORY-BANK.md | example |
| Compact active rules | learning/LEARNED.md | (see learn.md) |
| Sending/submitting mode | config/policy.json | policy |

## Steps
1. `memory.py current`, then `memory.py read --files <affected files>`.
2. Extract the smallest reusable lesson or the exact correction.
3. Write a proposal JSON (schema: `references/MEMORY-USAGE.md`) under the workspace's `.state/`:
   explicit intent, the user's words, real host, scope, change type, short before/after summary,
   reason, review flags, a stable `TX-…` id, the revision you read, exact operations (`replace`
   matching once, `append`, `json_set`, `json_add` for new HR facts, `write` for regenerated
   files), and `dependency_review` for every dependent file (updated or unchanged).
   - Changes to EXPERIENCE.md or identity.json that alter CV text must include the re-rendered
     master: `cv_init.py render`, then `write` operations for `cv/master-cv.tex` and
     `cv/cv-manifest.json` (memory.py re-renders to check an "unchanged" claim).
   - Pay, notice and address values never go into the summary or reason; the log withholds them.
   - Include `learning_ids` when the change resolves queued learning items.
4. `memory.py apply --file PROPOSAL.json`. It locks, checks the revision and hashes, snapshots,
   journals, appends one event and regenerates `learning/CHANGELOG.md`. Retry with the same
   transaction id after an interruption.
5. Reply with a one-line receipt: what changed and in which files. A staged proposal is not saved.

## Policy mode
`config/policy.json` `mode` is `drafts-only` (default), `send-with-approval` or
`submit-with-approval`. Change it only when the user asks in their own words, as a `policy`
change. Even then each send or submit needs its own approval (apply.md).

## Status, history, undo
- `memory.py status` and `memory.py history` show the current revision and events.
- `learning/CHANGELOG.md` is the readable history.
- Undo: `memory.py rollback EVENT_ID --request "<their words>" --platform HOST --transaction-id
  TX-…`. It restores the earlier content and adds a new event; it refuses when a later change
  touched the same file (make a targeted correction instead).
- Edits the user makes in an editor are detected and logged as "edited outside The Jobfather".
- Application status: `applications.py status ID` and `applications.py thread ID`. Never infer a
  send, submission, meeting or date.
