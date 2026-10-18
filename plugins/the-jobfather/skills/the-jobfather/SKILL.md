---
name: the-jobfather
description: Job-hunting copilot for any field. Use when the user wants to set up The Jobfather, import their CV or career history, understand their job market, find or rank jobs (LinkedIn, Naukri, company career pages), tailor a one-page LaTeX CV, write applications, cover letters, recruiter emails, LinkedIn notes or follow-ups in their own voice, fill application forms, track replies, or teach it a preference. Keeps personal data in the user's local workspace and never sends, submits or messages without explicit approval.
---

# The Jobfather

Read [core/ROUTER.md](core/ROUTER.md) and route the request that follows the skill name.

- Resolve every path from this file's folder (call it SKILL), never from the shell's working
  directory. Run helpers as `python3 SKILL/scripts/NAME.py …`.
- The user's data lives in their workspace, not here. First run
  `python3 SKILL/scripts/jf.py status --brief`: no workspace means onboarding first.
- Nothing is sent, submitted or messaged without the user's approval of that exact item.
