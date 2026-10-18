# Writing rules

Default rules for everything The Jobfather writes for the user. **The user's own
`profile/VOICE.md` and `learning/LEARNED.md` win over this file**, and this file wins over the
vendored humanizer checklist (`third_party/humanizer/HUMANIZER.md`). Example: if the user likes
two short bold phrases in emails, that beats the humanizer's "bold as decoration" pattern; if
their subject lines use dashes, keep them.

## The voice by default

Plain English. Professional, conversational and natural. Say the main thing, support it with one
relevant detail, make the next step easy. Be confident about known experience and precise about
contribution. No inflated language, generic enthusiasm or long introductions.

1. Lead with the answer or purpose. In an application, connect the role to the most relevant
   experience early.
2. Use familiar words and concrete verbs: managed, built, ran, improved, planned, checked, worked
   with. Use technical terms only when they add precision.
3. Prefer active sentences with a clear actor. Do not start every sentence with "I".
4. Cut filler ("I am writing to express my keen interest", "dynamic professional", "proven track
   record") and unsupported claims of passion.
5. Replace praise with evidence. "I build dashboards finance uses every week" beats "I bring a
   unique blend of strategic excellence".
6. Vary sentence length naturally. Do not swap synonyms for a repeated technical term.
7. A small story only where it helps: context, what you did, result or next step. Never in a
   salary field, connection note or CV bullet.
8. Contractions where the channel allows. CV bullets stay compact; recruiter emails can sound like
   a real conversation.
9. Few adjectives; no exclamation marks, rhetorical questions or stacked buzzwords; no headings in
   short messages.
10. One main ask. Polite without repeated apologies.
11. Keep names, dates, numbers, qualifiers, scope and intent when rewriting.
12. No deliberate typos, fake anecdotes, invented vulnerability or claims about beating AI
    detectors. Natural writing comes from clear meaning and real detail.

Words like "leverage", "synergy", "spearheaded", "transformative" and "results-driven" usually add
nothing; prefer a precise plain word. This is judgment, not a word ban, except for phrases the user
lists under "Words and phrases I avoid" in VOICE.md, which `check_text.py` blocks.

## Tone by medium

| Medium | Default |
| --- | --- |
| CV | Factual, compact, evidence-led; keep the template's structure |
| Short form answer | Answer first; one example if needed; usually 2-3 short sentences |
| Cover letter | 150-200 words; role fit and 1-2 proof points; natural paragraphs |
| Cold LinkedIn note | Brief introduction, a real reason to contact, one clear ask |
| Warm LinkedIn reply | Use the existing relationship; no reintroduction |
| Recruiter or application email | Clear purpose; subject line for a new thread |
| Follow-up | 1-3 short sentences tied to the actual previous exchange |
| Thank-you | Brief; a real discussion detail if the user supplied one |

Real field limits and explicit requests override these defaults. Never assume a universal
LinkedIn note limit; use the supplied one or check it.

## Application and recruiter emails (lessons from real use)

- Greeting: the recipient's real first name if a source states it; otherwise exactly "Hi,".
  Never "Hiring Team", "Dear Sir/Madam" or a name guessed from an email address.
- Open with the most relevant experience for this role (value first), then employer and title.
- State relevant experience directly. Do not narrate the matching ("as mentioned in the job
  description", "you are looking for X and I have X", "my skills align with your requirements").
- Never mention internal labels ("master CV", "tailored CV", "baseline"). Say "my CV".
- No vacancy caveats ("if this role is still open", "if you are hiring") and no routing asks
  ("could you forward this to the right person").
- Do not volunteer pay, notice period or relocation in an introduction unless asked or the user
  said to. Respect per-location disclosure rules saved in the market map.
- Keep it skimmable: roughly 100-150 words, short paragraphs, at most 2-3 short bold phrases
  (only if the user's voice allows bold).
- Close with one direct line ("I've attached my CV for your review.") and, when the user wants
  one, a single portfolio line. "Thanks," sits above the user's real saved signature.
- Signature: append the user's real signature (captured once into `profile/signature.html`)
  exactly once. Never type a replacement contact block. If it cannot be added, finish the body,
  mark the signature as pending and say so.
- Never create a draft without a verified recipient address from a real source.

## One editorial pass (before showing anything)

- Is the purpose or answer clear in the first line?
- Does each sentence add a fact, useful context or a next step?
- Is the strongest relevant point visible?
- Does it sound natural read aloud?
- Any unsupported claim, generic praise or repetition left?
- Are metric scope, attribution and uncertainty intact?
- Does it fit the channel and the real limit?

Then run the humanizer checklist once (strongest tells first: not-X-but-Y contrasts, one-line
closers, staged openers, forced triads, dashes everywhere, inflated claims), apply the user's
VOICE.md, and run `check_text.py`. Return one ready draft; keep checking notes out of the text.

## Illustrative rewrites (generic)

- Overwritten: "I am a highly motivated, results-driven professional with a proven ability to
  leverage innovative strategies." Better: "I build forecasting models and the dashboards teams
  use to act on them."
- Overwritten: "I spearheaded transformational automation initiatives that revolutionised
  reporting." Better: "I automated the monthly report, which saved about three days of work."
- Overwritten: "It would be my absolute privilege to explore synergies with your esteemed
  organisation." Better: "The role fits my analytics work. I'd be glad to talk."

These show style, not templates. Motivation for a specific company must come from the real JD or
the user's own words.
