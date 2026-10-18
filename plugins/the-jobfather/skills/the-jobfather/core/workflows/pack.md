# Application pack

Resolve or create one application; analyse its JD once (fit.md). Reuse that analysis and revision
for the fit note, CV PDF (cv.md), cover letter and the requested outreach. Answer form questions
only if questions were supplied. Never invent recipients, threads or limits.

After saving each requested module run `applications.py pack ID --platform HOST --kinds cv-pdf
cover-letter forms linkedin` (exactly the requested modules; `--test` for fictional records). It
checks checksums, the same JD, current facts and a validated PDF, and saves a versioned index.
Missing or stale modules fail visibly: report the exact blocker with the finished pieces rather than
calling the pack complete. Drafts stay drafts; a pack is never a submission.
