# Saved updates: the memory.py proposal

Every saved change to a profile file is one proposal JSON that the host writes and
`python3 SKILL/scripts/memory.py apply --file PROPOSAL.json` checks and commits. No model API is
called. The script locks the workspace, checks the revision and file hashes, validates the result,
snapshots before/after, journals the write, appends one event to `learning/changes.jsonl` and
regenerates `learning/CHANGELOG.md`. Write proposals under the workspace's `.state/` folder.

A proposal needs the user's own words asking to save or change something. "Looks good" approves
a draft; it does not authorize a saved change.

## Before writing one

1. `memory.py current` finishes interrupted writes, logs edits made outside The Jobfather and
   returns the revision: `{"revision": "...", "files": {"profile/VOICE.md": "<sha256>", ...}}`.
2. `memory.py read --files VOICE.md HR-DETAILS.json` returns the current text of those files and
   the same `context_revision`. Use `context_revision.revision` as `expected_revision` and
   `context_revision.files[path]` as each operation's `before_sha256` (`"missing"` for a file that
   does not exist yet).
3. Make the smallest change that captures the lesson or correction.

## Fields

| Field | Required | Rules |
| --- | --- | --- |
| `transaction_id` | yes | `TX-` + 6-80 letters, digits, `_` or `-`. Stable: retrying the same proposal is safe; reusing the id for different content is refused. |
| `intent` | yes | Always `"explicit-update"`. |
| `user_request` | yes | The user's words (max 4000 characters). Must ask to save or change something: update, correct, remember, save, store, change, set, replace, add, remove, forget, switch, enable, disable, allow, stop, use, or a word from `authorization_keywords` in `config.json`. Application scope also accepts "focus", "emphasise" and "for this". |
| `platform` | yes | `claude-code`, `codex`, `antigravity`, `cursor`, `other` or `unknown` (the real host). |
| `scope` | yes | `global` (profile files) or `application` (one application's local preferences). |
| `change_type` | yes | See the table below. |
| `expected_revision` | global | The revision you read. A different current revision means someone changed the files: reread and rebuild. |
| `summary` | yes | `{"before": "...", "after": "..."}`, each non-empty and at most 1000 characters. `after` is the rule as it should read later; `learn.py compact` lists it in LEARNED.md. |
| `reason` | yes | Why, in one or two sentences (max 2000 characters). |
| `review` | yes | `{"user_authorized": true, "facts_checked": true, "scope_checked": true}`. |
| `changes` | yes | One operation per file (below); a path appears once. |
| `dependency_review` | when needed | `{"<dependent path>": "updated" | "unchanged"}` (below). |
| `learning_ids` | optional | `LRN-...` ids from `learn.py queue` that this change saves; stored on the event. |
| `sensitive` | optional | `true` stores `[withheld: contains sensitive values]` instead of the user's words. |
| `accepted_output_ref` | optional | `{"application_id": "APP-...", "output_id": "...", "test": false}`; the output must be user-approved. |
| `application_id`, `test_application` | application scope | Which application record; `test_application: true` for the fictional sandbox. |
| `surface`, `version` | optional | The host surface and version, only when known. |

No other fields are accepted.

## Change types

| `change_type` | Typical files | Notes |
| --- | --- | --- |
| `factual-correction` | EXPERIENCE.md, identity.json, HR-DETAILS.json, PROFILE.md | Career facts and changing values (notice, pay, availability). |
| `preference` | VOICE.md, LEARNED.md | Writing choices and how the user wants things done. |
| `example` | VOICE.md, STORY-BANK.md | A sample or story the user approved. |
| `workflow` | LEARNED.md | Process rules; also used by `learn.py compact`. |
| `market` | market.json, MARKET.md | Titles, keywords, companies, exclusions, platforms, queries. |
| `form-answer` | FORM-ANSWERS.json, HR-DETAILS.json `custom` | Recurring application questions. |
| `policy` | config/policy.json only | `mode`: `drafts-only`, `send-with-approval` or `submit-with-approval`. Only when the user asks for it in their own words. |
| `cv-render` | cv/master-cv.tex, cv/cv-manifest.json | Staged by `cv_init.py adopt` after a visual review. |
| `application-specific` | one application's `record.json` | Application scope only. |

## Operations

Each item in `changes` is `{"path": ..., "before_sha256": ..., <one operation>}`. Paths may be
written `VOICE.md` or `profile/VOICE.md`.

- `"replace": {"old": "exact passage", "new": "replacement"}`: `old` must occur exactly once.
- `"append": "## Heading\n\nShort lesson."`: Markdown added at the end (max 12000 characters).
- `"json_set": [{"keys": ["notice_period", "value"], "value": "..."}]`: sets existing keys. In
  `FORM-ANSWERS.json` and `market.json` it may create keys. Lists are replaced whole: to add an
  exclusion, set `["exclusions"]` to the full new list.
- `"json_add": [{"keys": ["custom", "night_shifts"], "value": {...}}]`: adds keys that do not
  exist yet and never overwrites. New HR facts go only under `["custom", "<lower_snake_name>"]`
  with `value`, `confirmed_at` and `description` (and `"sensitive": true` when private).
- `"write": {"text": "...", "sha256": "<sha256 of text>"}`: whole-file rewrite, only for the
  summary files `MARKET.md` and `learning/LEARNED.md`.
  `"write": {"from_render": ".state/renders/R-.../master-cv.tex", "sha256": "..."}`: only for the
  generated CV files, from a render directory.

A change that leaves a file identical is refused.

## Dependent files

Some files summarise others. When a proposal changes a file, each existing dependent must appear
in `dependency_review` under its full path (`"profile/PROFILE.md"`): `"updated"` when the same
proposal changes it too, `"unchanged"` after you checked it still holds.

| Changed file | Review |
| --- | --- |
| EXPERIENCE.md | PROFILE.md, STORY-BANK.md, cv/master-cv.tex, cv/cv-manifest.json |
| identity.json | PROFILE.md, cv/master-cv.tex, cv/cv-manifest.json |
| HR-DETAILS.json | PROFILE.md, FORM-ANSWERS.json |
| market.json | MARKET.md |

For the CV files, "unchanged" is verified: memory.py re-renders the CV from the new facts and
refuses if it would differ. Then run `cv_init.py render` and include `write` operations for both
CV files with "updated".

## What is validated

- Markdown files start with a `# ` title and contain no TODO or TBD lines.
- Evidence IDs mentioned in any profile Markdown file must exist in EXPERIENCE.md. EXPERIENCE.md
  prefixes and claim IDs are never removed; retire a claim by starting its text with
  `(retired <date>)`.
- identity.json, HR-DETAILS.json (shape fixed; new facts under `custom`), FORM-ANSWERS.json
  (slug keys; each answer has `question`, `answer`, `confirmed_at`), config/policy.json and
  market.json are checked against their schemas.

## Sensitive values

Current, expected and minimum pay, notice period, last working date, resignation status, current
address, date of birth, and `custom` HR facts marked `"sensitive": true` never go into `summary`
or `reason` (write "Expected pay updated", not the number). When such a value changes, the event
stores `[withheld: contains sensitive values]` instead of the user's words. Set `"sensitive": true`
to withhold the words for anything else private.

## After applying

`apply` returns the event: `event_id` (`EV-...`), `transaction_id`, UTC and local time, platform,
change type, scope, `affected_paths`, before/after hashes and revisions, summary, reason,
`user_feedback` (the request or the withheld marker), `status` (`committed`, or `recovered` after
an interruption) and `learning_ids` when given. Report the change as saved only after this.
Then, if the proposal carried `learning_ids`: `learn.py resolve --event EV-...`.

| Error | Do this |
| --- | --- |
| Stale revision / stale target hash | Reread (`memory.py read`), rebuild the operations, keep the same intent. |
| Replacement text must match exactly once | Quote a longer, unique `old` passage. |
| Review dependent X | Check X and add it to `dependency_review`. |
| Sensitive values in summary or reason | Describe the change without the value. |
| The request does not ask to save anything | Ask the user; acceptance of a draft is not a save request. |
| Transaction ID reused | Use a new `TX-` id for different content. |

## Application scope

`"scope": "application"` changes one application's `jobs/applications/APP-.../record.json` and
nothing else: one operation whose `path` is that record (workspace-relative), only `json_set` on
`["local_preferences", "<key>"]`, with `application_id` (and `test_application: true` in the
sandbox). `before_sha256` is the record's current hash. No `expected_revision` is needed and
profile revisions do not change. Company-specific feedback belongs here unless the user says it
is general.

## Undo, history and outside edits

- `memory.py rollback EV-... --request "<the user's words: undo, roll back, restore or revert>"
  --platform HOST --transaction-id TX-...` restores each file to its state before that event and
  logs a new event (`status: rolled-back`, `rollback_of`). It refuses when a later change touched
  the same file (make a targeted correction instead), and it cannot undo the setup event or the
  creation of a file. An application-scope rollback restores only the keys that event changed.
- `memory.py status` and `memory.py history` show the revision and the events;
  `learning/CHANGELOG.md` is the readable history (generated; never edit it).
- Edits made in an editor are detected at the next `current`, `read` or `apply` and logged as
  `external-edit` events with `status: observed`.

## Examples

A voice preference (append), saving a queued learning item:

```json
{
  "transaction_id": "TX-voice-short-openings-01",
  "intent": "explicit-update",
  "user_request": "Remember: open recruiter emails with the role, then one relevant point.",
  "platform": "claude-code",
  "scope": "global",
  "change_type": "preference",
  "expected_revision": "<context_revision.revision>",
  "summary": {"before": "No rule for email openings.",
              "after": "Open recruiter emails with the role, then one relevant point."},
  "reason": "The user rewrote two openings this way and asked to keep it.",
  "review": {"user_authorized": true, "facts_checked": true, "scope_checked": true},
  "learning_ids": ["LRN-0123456789ab"],
  "changes": [{"path": "profile/VOICE.md", "before_sha256": "<context_revision.files[path]>",
               "append": "## Recruiter emails\n\nOpen with the role, then one relevant point."}]
}
```

A recurring form answer (`json_add` on an open file):

```json
{
  "changes": [{"path": "profile/FORM-ANSWERS.json", "before_sha256": "<hash>",
               "json_add": [{"keys": ["answers", "willing-to-work-weekends"],
                             "value": {"question": "Are you willing to work weekends?",
                                       "answer": "Occasionally, with notice", "scope": "global",
                                       "confirmed_at": "2026-01-15", "source": "user"}}]}],
  "change_type": "form-answer"
}
```

A new private HR fact (only the changed parts shown; `PROFILE.md` and `FORM-ANSWERS.json`
reviewed as dependents):

```json
{
  "change_type": "factual-correction",
  "summary": {"before": "No expected pay for the second target city.",
              "after": "Expected pay recorded for the second target city."},
  "dependency_review": {"profile/PROFILE.md": "unchanged", "profile/FORM-ANSWERS.json": "unchanged"},
  "changes": [{"path": "profile/HR-DETAILS.json", "before_sha256": "<hash>",
               "json_add": [{"keys": ["custom", "expected_pay_second_city"],
                             "value": {"value": "<the amount the user gave>", "confirmed_at": "2026-01-15",
                                       "description": "Expected pay if working from the second target city",
                                       "sensitive": true}}]}]
}
```

LEARNED.md is rebuilt with `learn.py compact`: it previews the file, and with
`--request "<the user's words>"` stages a `workflow` proposal with a `write` operation under
`.state/proposals/`. Apply it like any other.
