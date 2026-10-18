# Security

## Reporting a problem

Please report security or privacy problems privately through GitHub's
[private vulnerability reporting](https://github.com/tusharsapra/the-jobfather/security/advisories/new)
rather than a public issue. Include steps to reproduce and the version. You will get a reply within
a week.

## What counts

- Any way to send, submit or message without the user's approval of that exact item.
- Any way content from a job ad, web page, email or tool result could make the agent take an action.
- Any path that writes outside the workspace, escapes the CV build sandbox, or leaks profile data.
- Any way personal data could reach the repository despite the privacy scanner.
- Weaknesses in the approval records, workspace locks or the change journal.

## Design notes

- The skill's scripts are standard-library Python, run locally, with no network access except the
  public job-board reads and the MCP servers the user enables.
- CV builds run pdfLaTeX with shell escape off, restricted file access and a cleaned environment.
- Approval records are HMAC-signed for tamper evidence only; high-stakes actions also need a
  human-only step (host permission prompt, confirm page, or the user's own Submit click).
