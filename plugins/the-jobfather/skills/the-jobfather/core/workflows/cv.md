# CVs: content, review, PDF

The user's master CV (`cv/master-cv.tex`) was rendered from their profile at onboarding
(cv_init.py) and is fixed: contacts, employers, titles, dates, education, layout, fonts and
spacing never change per job. Only the slots listed in `cv/cv-manifest.json` change:
the summary paragraph, the bullets of entries marked `cv: tailor`, and the skills and tools lists.

`python3 SKILL/scripts/cv_content.py schema` shows the slots, bullet counts and tailoring weights;
`cv_content.py baseline` gives the current content patch.

## cv-content (text only)
Copy the baseline patch and edit allowed slots only: tailor `primary` entries most, `secondary`
lightly, `light` rarely; small summary changes; reorder or rephrase skills truthfully. Every bullet
must stay true to its claims (keep metric scope). Tools may only come from the tools vocabulary in
EXPERIENCE.md. No TeX, no `**` markup, no claim IDs in content. Baseline wording is better when the
JD does not justify a change. `cv_content.py check PATCH.json` checks the mechanics; you check the
facts. Save the content and its evidence IDs as a `cv-content` output.

## cv-review
Review supplied content for facts, scope, grammar, JD relevance and supported keywords. For a PDF,
`cv_pdf.py inspect PDF` (add `--external` for a CV not made from this master), then look at every
rendered page before claiming a full review. Give prioritised corrections; no ATS score.

## cv (the default for "make my CV for this job"): a validated one-page PDF
1. Create or reuse the application and its valid analysis (fit.md).
2. Write the content patch once (above). Optional emphasis JSON: `{"lkm.0": [{"start": 8,
   "end": 19}]}` (bullet slots only; no overlaps, never a whole bullet). Without it, bold phrases
   carry over where the same words remain.
3. Review JSON: `{"context_revision": REVISION, "facts_preserved": true, "voice_checked": true,
   "notes": "…"}` after actually reviewing.
4. `python3 SKILL/scripts/cv_pdf.py compile --patch PATCH --review REVIEW --application-id ID
   --platform HOST [--emphasis FILE] [--test]`. Builds run in isolation with shell escape off.
5. Only `awaiting-visual-review` continues. On overflow, read `validation.json` (page count,
   overfull boxes, per-slot line positions), shorten only the affected content and retry with
   `--retry-of BUILD_DIR` (at most three retries). Never change the design to fit; never return an
   older PDF as the new one.
6. Look at every page image. Write the visual receipt `{"passed": true, "reviewed_by": HOST,
   "notes": "…", "render_sha256": {workspace-relative render path: sha256}}`.
7. `cv_pdf.py promote BUILD_DIR --visual-review RECEIPT`. It rechecks integrity, saves a new
   version under `jobs/outputs/cv/` named with the user's prefix, and registers it on the
   application. Return the PDF path. A PDF is not a submission.

## Role-family variants
For a core role family in market.json, a checked content patch can be saved as the starting point
for that family's jobs: `cv_content.py save-variant --family ID PATCH.json --notes "…"` (after the
user likes a CV for that family; `--replace` to update it). For a new job,
`cv_content.py variant --family ID` returns that patch, or the baseline when there is none or the
master CV no longer fits it; `master_changed: true` means re-check the wording against the current
facts. `cv_content.py variants` lists them. Variants are content patches, never new designs, and
every job's patch is still tailored and checked as above.

## Changing the master
New facts or reworded bullets go into EXPERIENCE.md first (update-status.md), then
`cv_init.py render` → `check` → look → write operations in the same saved update. The master can
never drift from the facts.
