---
name: the-jobfather
description: Job-hunting copilot for any field. Use when the user wants to set up The Jobfather, import their CV or career history, understand their job market, find or rank jobs (LinkedIn, Naukri, company career pages), tailor a one-page LaTeX CV, write applications, cover letters, recruiter emails, LinkedIn notes or follow-ups in their own voice, fill application forms, track replies, or teach it a preference. Keeps personal data in the user's local workspace and never sends, submits or messages without explicit approval.
---

# The Jobfather

Read [core/ROUTER.md](core/ROUTER.md) and route the request that follows the skill name.

- Resolve paths from this file's folder, never from the shell's working directory.
- The user's data lives in their workspace, not here. Find it with `python3 scripts/jf.py where`.
- No workspace yet, or setup unfinished: run the onboarding workflow first.
- Setup or connection tasks: run `python3 scripts/connect.py status --brief` and offer to resume a pending step.
