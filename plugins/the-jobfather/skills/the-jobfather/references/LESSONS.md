# Lessons from real use

Each rule below came from something that went wrong in a real job search. They apply on every
host and in every field. Workflows repeat the ones they need.

## Integrations

- **Third-party servers can act on their own.** Some platform servers ship schedulers that
  auto-apply or "boost" profiles. Only run servers whose behaviour you know; turn off anything
  autonomous.
- **Some tools submit on the first call.** Never call an apply/submit tool to "preview" a form.
- **Login checks are wrong in both directions.** Report *installed*, *visible to the agent*,
  *logged in* and *search verified* separately. "Inconclusive" is not "logged out".
- **A README can describe tools the pinned release lacks.** Use the tools the host actually lists.
- **Browser profiles do not share logins.** A new automation profile starts logged out. Never
  reset or delete a profile to fix a startup error. Never ask for passwords or one-time codes in
  chat; the user types them in the window.
- **Mail quotas exist.** Creating many drafts quickly can hit a per-minute limit: check for an
  existing draft before retrying, and space real sends at least 12 seconds apart.
- **Drafts created through a mail connector do not get the saved signature.** Capture the real
  signature once, append it exactly once, read the draft back to verify.
- **Spreadsheets:** use plain URLs (no formulas), split text longer than a cell allows into
  numbered parts, never overwrite the user's Stage/Notes edits, keep a `.bak` of the last version.

## Research quality

- **Platform filters are unreliable.** Location filters drop or keep stale cities: put the city in
  the keywords and check every JD. Freshness filters return old posts: check the real date.
- **A repost is not a new opening.** Match on company, title, location and requisition.
- **A search snippet is not a JD.** Fetch the full description or mark it missing.
- **A blocked page is not a closed job.** Only an explicit closure or a passed deadline is.
- **Never guess** an email address, a person's name from an address, or an employer from a domain.
  Contact details need literal source evidence.
- **Shared inboxes** (group recruiting addresses) get one grouped message, and all past sent mail is
  checked before any first contact.
- **Never widen a date window to hit a number.** Report fewer results instead.

## Outcome meanings

- generated ≠ approved ≠ sent ≠ submitted.
- A SENT label is not proof of delivery; a bounce does not mean the job closed.
- A deleted draft was not sent.
- An assessment is not an interview; an offer is not an acceptance.
- Follow up 10 days after the last verified real contact, at most twice per thread. Creating a
  draft does not reset the clock or count as a send.

## Interaction

- Confirm what "these" or "all" covers before batch work.
- Factual form fields contain only the value (a title field gets just the title).
- Ask about the one missing fact you need, after finishing everything else.
- When the user confirms a fact mid-task, queue it for saving (learn.md); do not let it die in a
  task file.
- Big behaviour changes (starting to send, starting to submit) are policy changes: they go through
  a logged update, never silently.

## Engineering

- Every workspace write goes through the scripts (locks, atomic replace, journal). Hand edits to
  stores break history; edits to profile files are detected and logged.
- Tests use fictional data and never depend on anyone's real profile.
- Documentation never embeds personal values; they go stale and leak.
