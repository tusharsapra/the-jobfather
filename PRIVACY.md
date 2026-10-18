# Privacy

## Where your data lives

The Jobfather keeps everything about you in one folder you choose: your **workspace** (default
`~/Jobfather`). It holds:

| Folder | Contents |
| --- | --- |
| `profile/` | identity and contacts, experience and evidence, HR values (notice, pay if you add it), form answers, voice rules, signature, market map, imported originals |
| `cv/` | your master CV source, manifest and baseline PDF |
| `jobs/` | job registry, outreach records, application records, tailored CVs, the tracker workbook |
| `learning/` | your change history (`CHANGELOG.md`), pending lessons, the short list of active rules |
| `.state/` | browser profiles for automation, connection status, approvals, locks |

The skill's own folder (installed by your agent) contains no personal data and is replaced on
update; your workspace is never touched by an update or uninstall.

## What leaves your machine

The Jobfather has no server and sends nothing to its maintainers. Data leaves your machine only
through services you use:

- **Your AI host** (Claude Code, Codex, Antigravity, Cursor…) sees the files and pages the agent
  reads for a task, under that host's own privacy terms.
- **Services you connect** (mailbox, LinkedIn, Naukri, career sites) receive what a task needs, for
  example a search query or, after your approval, an application.
- **Public job-board APIs** receive the company board names you look up.

Nothing is sent, submitted or messaged without your approval of that exact item.

## Sensitive values

Pay, notice period and address are stored only in `profile/HR-DETAILS.json`. The change history
records that they changed but withholds the values. Self-identification questions on application
forms default to "prefer not to say".

## Logins and secrets

You type passwords and one-time codes in the browser window yourself; The Jobfather never asks for
them in chat and never reads or exports browser cookies. OAuth secrets for a mail server stay in
your host's own configuration, never in the workspace. `.state/connections.json` stores connection
states and dates, not accounts or tokens.

## Deleting your data

Delete the workspace folder. To keep the folder but stop automation, remove the browser profiles in
`.state/browser/` and disconnect services in your host. Recruiter contact details you saved are
stored for your own job search only; delete them when you no longer need them.

## For contributors

The repository must never contain anyone's real personal data. Tests and examples use the fictional
demo persona. `dev/privacy_scan.py` runs on every commit and push; see CONTRIBUTING.md.
