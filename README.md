# The Jobfather

An open-source job-hunting copilot for AI coding agents: Claude Code, Codex, Antigravity, Cursor
and anything else that runs agent skills. It works in any field and any country.

The Jobfather learns who you are from your own CV and answers, maps the job market for your kind of
role, finds and tracks openings, tailors a one-page LaTeX CV for each job, writes applications and
follow-ups in your voice, and keeps a change log of everything it learns about you. Nothing goes
out (no email, no application, no message) unless you approve that exact item.

> **Status:** v0.1, early. Expect rough edges; issues and pull requests are welcome.

## What it does

- **Onboarding that collects everything first.** Import your CV (PDF, Word, LaTeX), your LinkedIn
  data export, writing samples and job posts you like. It drafts your profile from those facts only,
  then asks short questions to fill the gaps (target roles, locations, notice, work authorization,
  form answers, how you write).
- **Understands your market.** Reads 20-50 live postings for your field and region and builds a map:
  the titles employers actually use, synonyms, seniority, must-have skills mapped to your evidence,
  company types, locations, platforms, saved searches and exclusions.
- **Finds and tracks jobs.** Company career pages (public Greenhouse, Lever, Ashby and other job-board
  APIs), LinkedIn and Naukri (both opt-in), pasted links and screenshots. Everything lands in a local
  tracker workbook you can edit.
- **Tailored one-page CVs.** A clean, ATS-friendly LaTeX design rendered from your profile. For each
  job only the summary, chosen bullets and skills change; every PDF is checked (one page, fonts
  embedded, text extractable, nothing clipped) and you look at it before it is saved.
- **Writes like you.** Applications, cover letters, recruiter replies, LinkedIn notes and follow-ups
  follow your own voice rules, then a humanizer pass and a lint for phrases you never use.
- **Applies only with your say-so.** Drafts by default. Sending and submitting are switched on by a
  logged change, each item needs your explicit approval, and the final Submit click is yours.
- **Tracks replies and follow-ups.** Reads your mailbox (if connected) to match recruiter replies to
  applications and tells you what needs attention.
- **Learns from you.** Facts you confirm, edits you make to drafts before sending, and outcomes
  become proposed updates. Every saved lesson is logged in your own change history and can be undone.

## Install

You need an agent that supports skills, plus: Python 3.9+, [uv](https://docs.astral.sh/uv/)
(runs the bundled MCP servers), Node 18+ (browser automation), Google Chrome. For CV PDFs: a TeX
distribution with pdfLaTeX (TeX Live, MacTeX or MiKTeX) and Poppler. `doctor.py` checks all of it
and tells you what to install.

**Claude Code**

```text
/plugin marketplace add tusharsapra/the-jobfather
/plugin install the-jobfather@the-jobfather
```

**Codex**

```bash
codex plugin marketplace add tusharsapra/the-jobfather
codex plugin add the-jobfather@the-jobfather
```

**Antigravity**

```bash
git clone https://github.com/tusharsapra/the-jobfather
agy plugin install the-jobfather/plugins/the-jobfather
```

**Any agent (skill only; the setup step registers the MCP servers)**

```bash
npx skills add tusharsapra/the-jobfather -g -a claude-code   # or -a codex, -a antigravity, -a '*'
```

Then say: **"Set up The Jobfather."**

## Using it

Ask in plain words, for example:

- "Set up The Jobfather" / "Import my CV and LinkedIn export"
- "Which job titles should I be searching for?"
- "Find data analyst jobs posted this week in Toronto"
- "Save these links to my tracker" (or paste screenshots)
- "Rank my saved jobs" / "What needs my attention?"
- "Tailor my CV for this job" / "Write the application email"
- "Apply to this Greenhouse job" (prepares and fills; you click Submit)
- "Remember: I never use 'Dear Hiring Manager'" / "What have you learned about me?"
- "Undo the last change"

## Your data

Everything about you lives in your **workspace** folder (default `~/Jobfather`): your profile, CV
master, voice rules, market map, applications, tracker and change history. The Jobfather's code
never stores your data and never uploads it anywhere. Services you choose to connect (your mailbox,
LinkedIn, Naukri, the AI host you run it in) see what they need for the task you ask for. See
[PRIVACY.md](PRIVACY.md).

## Safety model

- **Policy modes:** `drafts-only` (default) → `send-with-approval` → `submit-with-approval`. Changing
  mode is itself a logged update.
- **Per-item approval:** every send or submit needs your words approving that exact item; the
  approval is bound to the content, expires in minutes and works once.
- **Human-only final step:** you click Submit on application forms and on the Naukri confirm page.
- **No guessing:** facts come from your profile with evidence; unknown values are asked, never
  invented; instructions hidden in job ads are ignored.
- **Responsible automation:** your own logins in real Chrome, slow fixed pacing and daily caps, no
  CAPTCHA solving or anti-detection tricks, and it stops at any checkpoint.

**Platform terms.** Automating LinkedIn, Naukri or other job sites can break their terms of use and
can get an account restricted. LinkedIn and Naukri automation is off until you opt in. The Jobfather
is an independent project, not affiliated with or endorsed by any job platform, ATS vendor or mail
provider; product names are used only to describe compatibility.

## How it is built

```text
plugins/the-jobfather/skills/the-jobfather/   the skill: SKILL.md, router, workflows, references,
                                              stdlib Python scripts, CV template, workspace templates
packages/jobfather-mcp/                       MCP servers: browser launcher, allowlist proxy,
                                              clean-room Naukri server (run with uvx)
examples/demo-workspace/                      a fully fictional user, used by tests and docs
tests/                                        hermetic tests (they never touch a real workspace)
dev/                                          maintainer tools, including the privacy scanner
```

The agent does the reading and writing; small Python scripts do storage, validation, checks and PDF
builds. No model API keys, servers or background jobs are needed.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Every commit is scanned for personal data. Test with the
fictional demo persona, never with real people's data.

## Credits and licence

MIT, see [LICENSE](LICENSE). Built by [@tusharsapra](https://github.com/tusharsapra) out of a real
job search. Third-party work and licences: [THIRD-PARTY.md](THIRD-PARTY.md).
