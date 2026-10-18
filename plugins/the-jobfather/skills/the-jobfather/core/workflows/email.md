# Emails: applications, recruiter replies, scheduling, thanks

New email: subject plus a concise body. Thread reply: keep the subject unless asked.
Read VOICE.md (including its signature name), LEARNED.md and `references/WRITING-RULES.md` first.

## Writing
- Applications and introductions: lead with the most relevant experience for this role, make the
  value easy to skim, one direct close ("I've attached my CV for your review."). No JD-matching
  narration, vacancy caveats, routing asks or internal CV labels. Do not volunteer pay, notice or
  relocation unless asked or the user said to.
- Recruiter questions and scheduling: answer first. Read the requested HR fields; finish what is
  known and ask for the one missing value. Use only supplied dates and availability.
- Greeting: the recipient's first name only when a source states it; otherwise exactly "Hi,".
  Never "Hiring Team"; never a name guessed from an address.
- Thank-you: a real discussion detail if supplied; otherwise thank them for their time honestly.
  Never invent topics, praise or outcomes.

## Before drafting from the tracker
Check the outreach history for this company and recipient (`outreach.py list`, job-pipeline.md):
never repeat a send, never recreate a draft the user deleted, and never write to an inbox that
bounced until a new verified contact exists. Shared group inboxes get one grouped email. A draft
needs a verified recipient address from a real source.

## Mail drafts and the real signature
- Use the connected mail tools when available. Resolve the signature once per batch from
  `profile/signature.html` (captured from Gmail settings or a real sent message; see connect.md).
  Connector-created drafts do not get Gmail's saved signature automatically, so insert the body
  before it or append it exactly once. Never type a replacement contact block.
- Read the saved draft back: recipient, subject, body, attachments (the attached CV's hash must
  match a validated CV), exactly one signature and its links. Record it with
  `applications.py record-draft ID OUTPUT_ID --file RECEIPT.json --platform HOST` and
  `outreach.py record-drafted …`.
- If the signature cannot be added, finish everything else, mark it pending and say so.
- Creating drafts can hit a per-minute limit: check for an existing draft before retrying.

## Sending (only when asked, only inside policy)
Drafts are the default. Sending needs `send-with-approval` policy mode (update-status.md) and the
user's approval of that exact email: `python3 SKILL/scripts/approvals.py create --kind email_send
--subject-file … --payload-file … --statement "<their words>" --platform HOST`. Space sends at
least 12 seconds apart. After a send, verify it in Sent and record it (`outreach.py record-sent`
with the real message and thread ids). SENT is not proof of delivery; a bounce does not close the
job.
