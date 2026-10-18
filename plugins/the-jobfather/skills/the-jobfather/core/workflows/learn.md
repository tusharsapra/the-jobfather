# Learning loop

The Jobfather gets more personal through three habits. Nothing is learned silently: every saved
lesson is an explicit, logged, reversible update (update-status.md).

## 1. End-of-task sweep (every task)
Before finishing, check what the user confirmed or decided during the task:
facts (a degree title, notice period, a tool they do or never used), preferences (greeting,
sign-off, length), exclusions (a sector, a title, a company), form answers, market signals,
pay details. For each one: `python3 SKILL/scripts/learn.py queue --kind fact|preference|exclusion|
form-answer|market|voice|policy --text "…" --source chat`. Then ask once: "Save these N things for
next time?" and list them in plain words. On yes, write one proposal per owning file with the
items' `learning_ids` and apply it; then `learn.py resolve --event EV-…` (it reads the event's `learning_ids`). On no,
`learn.py dismiss --ids … --reason "…"`. `learn.py sweep` prints the checklist and pending items;
the router shows `learning_pending=N` at the start of the next task.

## 2. Learn from what the user actually sent
When the user edits drafts before sending (often), compare drafts with the sent versions:
1. With the mail connector, collect pairs of (saved draft text, sent message text) for recent
   applications, matched by thread or by recipient and subject. Skip pairs you cannot match.
2. Write them to a JSON file and run `python3 SKILL/scripts/learn_diff.py --pairs FILE`.
   It strips signatures and quoted threads, diffs word by word and counts recurring edits.
3. Turn recurring edits (seen in several messages) into one or two VOICE.md rules with the counts
   as evidence. Explicit user rules beat contrary examples. Ignore mail-client artefacts
   (spacing, auto-links).
4. Show the proposed rules; save only what the user approves.

## 3. Weekly review
`python3 SKILL/scripts/learn.py stats --days 30`: replies, interviews, rejections and silence by
role family, location and company type, plus skip reasons. Propose at most five specific changes
(market titles to drop or add, exclusions, follow-up timing, a writing rule), each with its
evidence. Save only approved ones.

## LEARNED.md
`learning/LEARNED.md` is the short list of active rules the router loads at the start of each
task. After saved changes pile up, `learn.py compact` previews a rewrite from the change history
(one line per rule with its event id). When the user asks to tidy it, run
`learn.py compact --request "<their words>" --platform HOST`; it writes a proposal under
`.state/proposals/` that you apply with `memory.py apply` like any other update.
