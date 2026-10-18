# Follow-ups

For a batch of follow-ups due, or "what needs my attention", start with job-pipeline.md (`brief`).
Defaults: 10 calendar days after the last verified real interaction, at most two sends per thread,
unless the other side promised a date or the user set a follow-up date. A generated draft never
counts as a send or resets the clock. For one application the user picks now, go straight to its
thread without waiting for the threshold.

1. Resolve the exact application and channel. If several match, ask which one.
2. Read `applications.py status ID` and `applications.py thread ID` (not every old output).
   A saved draft is not a sent message. If the thread is missing, ask for it.
3. Write 1-3 short sentences tied to the real last exchange, with a clear request. Keep promised
   timing relative unless a date was given. No guilt, invented urgency or competing offers; do not
   repeat the introduction.
4. Mail drafts use email.md (real signature, read-back, receipt). Save the draft and the intended
   next action; a next-follow-up date is a note, not a scheduled reminder.
