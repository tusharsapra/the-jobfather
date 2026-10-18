# The Jobfather

The Jobfather is a job-search assistant that runs inside the AI agent you already use: Claude Code,
Codex, Cursor or Antigravity. It learns your background from your own CV and answers, works out
which job titles and keywords your field really uses, finds and tracks openings, tailors a one-page
LaTeX CV for each job, and writes applications and follow-ups that sound like you. It works for any
field and any region, and nothing is sent or submitted until you approve it.

> v0.2.0 adds Naukri, through The Jobfather's own opt-in Naukri server.

## Why I built it

I'm Tushar Sapra. I work in marketing, and I use The Jobfather in my own job search. It started as a
private skill for my hunt: it began with marketing roles, and over time it learned which titles and
keywords found the right jobs, which openings to skip, and how I write. I rebuilt it so you can set
it up with your own details and get the same help. None of my personal data is in this repository.

## How it fits together

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/anatomy-dark.svg">
  <img src="docs/anatomy-light.svg" alt="The Jobfather drawn as an instrument rack on a bench: a request card and a selector knob on top, a shelf of 19 workflow cards, a rail of script modules, a cabinet of six card drawers with a dial gauge resting on the open drawer's stack of cards, and four tools wired to it on the bench.">
</picture>

Picture it as a rack on a workbench. Your request goes in at the top, and everything it knows about
you sits in the drawers at the bottom.

- The card standing in the slot on top is your request. The agent reads `SKILL.md` first, a short
  file that hands every request to the router.
- The selector knob is the router (`core/ROUTER.md`). It matches your request to one of 19
  workflows and loads only that one, so each task stays quick and cheap.
- The card file on the shelf holds those 19 workflows (`core/workflows/`): setup, market mapping,
  job search, fit checks, CVs, cover letters, emails, follow-ups, form filling, applying and
  learning, among others.
- The row of modules under the shelf is the Python helpers (`scripts/`). They handle the work that
  has to be exact: saving and undoing changes to your profile, building and checking CV PDFs, the
  tracker, approvals and reply tracking.
- The drawers are your workspace, a folder on your own computer (`~/Jobfather` by default) with your
  profile, career history and the evidence behind it, writing voice, market map, CV and change log.
  Updating or removing the skill never touches it.
- The gauge resting on the open drawer reads how much The Jobfather knows about you.
- The tools on the bench are wired in only when you connect them: a browser for career sites and
  application forms, your mailbox, the PDF builder and the Excel tracker.

## The more it knows you, the better it works

Everything The Jobfather produces is built from your workspace. Job matches, fit checks, CV bullets
and emails all come from facts you have given it, so a thin profile gets generic results and a full
one gets results that fit you. When a fact is missing, it asks you.

What helps most:

- your full CV, your LinkedIn data export, and a portfolio or GitHub link if you have one
- three or four emails or cover letters you wrote yourself, so it can learn your voice
- the roles, seniority and places you want, and the ones you don't
- skills and tools you have never used, so it stops matching you to jobs that need them
- your answers to the questions application forms keep asking, like notice period and work
  authorization

It keeps learning while you use it. When you correct a fact, edit a draft before sending it, or skip
a job and say why, it offers to save that for next time. Every saved lesson goes into a change log
in your workspace that you can read, and any change can be undone.

## What it does

- Sets you up from your own material. It imports your CV (PDF, Word or LaTeX), your LinkedIn export,
  writing samples and job posts you like, drafts your profile from those facts, then asks short
  questions to fill the gaps.
- Maps your job market. It reads 20 to 50 live postings for your field and region and records the
  titles employers use, the seniority levels, must-have skills mapped to your experience, company
  types, locations, where to search and what to skip.
- Finds and tracks jobs from company career pages (Greenhouse, Lever and Ashby job boards),
  LinkedIn, Naukri, pasted links and screenshots, all in one tracker workbook you can edit by hand.
- Tailors a one-page CV for each job. The design and your facts stay fixed; the summary, the chosen
  bullets and the skills change. Every PDF is checked (one page, fonts embedded, nothing cut off),
  and you see it before it's saved. CVs support Latin-script languages for now.
- Writes applications, cover letters, recruiter replies, LinkedIn notes and follow-ups in your
  voice, checked against your own writing rules and a humanizer pass.
- Fills Greenhouse application forms from an answer plan you approve, then waits for you to click
  Submit. Other application systems get guided filling under the same rules. On Naukri it applies
  only after you approve that job and click Submit on a confirm page it opens.
- Reads replies in your mailbox, if you connect it, matches them to your applications and tells you
  what needs attention and when to follow up.

## What it uses

| Tool | What it's for | When you need it |
| --- | --- | --- |
| An AI agent with skills: Claude Code, Codex, Cursor or Antigravity | Reads, writes and runs the workflows | Always |
| Python 3.9 or newer | Runs the helper scripts, standard library only | Always |
| [uv](https://github.com/astral-sh/uv) | Starts The Jobfather's MCP servers and installs openpyxl for the tracker | For the browser and the tracker |
| Node.js 18+ and Google Chrome | Run [Playwright MCP](https://github.com/microsoft/playwright-mcp), which drives a real Chrome window with your own logins | For career sites, LinkedIn pages and application forms |
| A TeX distribution with pdfLaTeX (TeX Live, MacTeX or MiKTeX) and Poppler | Build and check the one-page CV PDF | For CVs |
| A Gmail connector (the claude.ai connector in Claude Code, the Gmail plugin in Codex) | Reads replies, captures your real email signature, saves drafts | Optional |
| [mcp-server-linkedin](https://github.com/stickerdaniel/linkedin-mcp-server) | LinkedIn job search, read-only behind an allowlist | Optional |
| The Jobfather's Naukri server | Naukri search, job details, recommended and saved jobs, and applying with your click | Optional, built in |
| Greenhouse, Lever and Ashby public job-board APIs | Listings straight from company career pages, no login | Built in |
| [humanizer](https://github.com/blader/humanizer) by Siqi Chen | A checklist that keeps the writing sounding like a person | Bundled |

During setup it checks which of these you have and tells you how to install the rest.

## Install

### Claude Code

```text
/plugin marketplace add tusharsapra/the-jobfather
/plugin install the-jobfather@the-jobfather
```

### Codex

```bash
codex plugin marketplace add tusharsapra/the-jobfather
codex plugin add the-jobfather@the-jobfather
```

### Antigravity

```bash
git clone https://github.com/tusharsapra/the-jobfather
agy plugin install the-jobfather/plugins/the-jobfather
```

### Any agent that supports skills

```bash
npx skills add tusharsapra/the-jobfather -g -a claude-code   # or -a codex, -a antigravity, -a '*'
```

The setup step then registers the MCP servers for you.

Then say: **"Set up The Jobfather."**

## Using it

Ask in plain words, for example:

- "Set up The Jobfather" or "Import my CV and LinkedIn export"
- "Which job titles should I be searching for?"
- "Find data analyst jobs posted this week in Toronto"
- "Search Naukri for marketing manager roles from the last three days"
- "Save these links to my tracker" (or paste screenshots)
- "Rank my saved jobs" or "What needs my attention?"
- "Tailor my CV for this job" or "Write the application email"
- "Apply to this Greenhouse job" (it prepares and fills the form; you click Submit)
- "Remember: I never use 'Dear Hiring Manager'" or "What have you learned about me?"
- "Undo the last change"

## Your data

Everything about you lives in your workspace folder: your profile, CV master, voice rules, market
map, applications, tracker and change history. The Jobfather's code stores none of it and uploads
none of it. The services you choose to connect (your mailbox, LinkedIn, Naukri, the AI agent you run it in)
see only what a task needs. [PRIVACY.md](PRIVACY.md) has the details.

## How it keeps you in control

- It starts in drafts-only mode. Sending emails and submitting applications are switched on
  separately, and each switch is a logged change you can undo.
- Every send or submit needs your approval of that exact item. The approval is tied to the content,
  expires after 15 minutes and works once.
- On application forms and on the Naukri confirm page, you click Submit yourself.
- Facts come from your profile, each with its source. It asks you about anything it doesn't know,
  and it ignores instructions hidden inside job ads.
- Browser automation uses your own logins in a normal Chrome window, at a slow, steady pace with
  daily limits. You type every password and security code yourself, and at a CAPTCHA it hands
  over to you.

## How it is built

```text
plugins/the-jobfather/skills/the-jobfather/   the skill: SKILL.md, router, workflows, references,
                                              Python scripts, CV template, workspace templates
packages/jobfather-mcp/                       MCP servers: browser launcher, allowlist proxy and the
                                              clean-room Naukri server (run with uvx)
examples/demo-workspace/                      a fully fictional user, used by tests and docs
tests/                                        tests that never touch a real workspace
dev/                                          maintainer tools, including the privacy scanner
docs/                                         the figure above, in light and dark
```

The agent does the reading and writing, and small Python scripts handle storage, checks and PDF
builds. You don't need an API key, a server or a background job.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Every commit is scanned for personal data, and the tests use
only the fictional demo persona.

## Credits and licence

MIT licence: anyone can use The Jobfather, free, to find a job. See [LICENSE](LICENSE).

Built by Tushar Sapra ([@tusharsapra](https://github.com/tusharsapra)). Third-party work and
licences are listed in [THIRD-PARTY.md](THIRD-PARTY.md). The Jobfather is an independent project
with no ties to any job site, application system or mail provider; product names only say what it
works with.
