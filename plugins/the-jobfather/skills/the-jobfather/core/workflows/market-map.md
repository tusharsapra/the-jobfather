# Market map: understand the user's job market

Use during onboarding, when the user asks which roles or keywords to target, or for a refresh
(monthly, or when results dry up). Output: `profile/market.json` (schema in
`references/MARKET-MAP.md`) and a readable `profile/MARKET.md`. Do not assume the user's field:
work it out from their profile and the live market.

## 1. Start from the person
Read PROFILE.md targets, EXPERIENCE.md headings, claims and tools, and HR-DETAILS locations.
Write down: likely role families, seniority (years of relevant experience), strongest skills with
claim IDs, locations and remote stance, and anything they ruled out.

## 2. Look at the real market (20-50 live postings in their region)
Sources, cheapest first: public job-board APIs of plausible employers (`references/PLATFORMS.json`
→ `public_ats_apis`), web search for current postings, then connected platforms (LinkedIn,
Naukri) with one small first-page search per title. Read full JDs, not snippets. Note for each
posting: title as written, seniority/years, must-have and nice-to-have skills, tools, company
type, location/work mode, pay if published. Stop at about 50; never widen the date window to get
more.

## 3. Build the map
- **Title graph:** canonical titles, synonyms, regional variants. Tag each core / adjacent /
  stretch / excluded with the user. Prefer the words employers actually use.
- **Seniority ladder** per company type, the user's band, stretch margin and junior floor.
- **Skills:** must/nice for the target roles, each mapped to claim IDs; list real gaps honestly and
  ask the user which they have never done (that list prevents bluffing later).
- **Company types:** where they post, how to reach them (careers page, recruiter, shared inbox),
  typical titles, which CV variant fits. Add target companies with careers URL, ATS and board
  token (`scripts/ats/classify.py --url URL` identifies the ATS).
- **Geography:** ordered locations with match words, remote stance, pay floor per location if the
  user wants one, what may be said about relocation.
- **Platforms** that work for this region and field, with notes (filter reliability, time window).
- **Queries:** `python3 SKILL/scripts/market.py queries --platform linkedin --location CITY` gives a
  starting point from the title graph; refine with the user and keep the ones that return good
  results.
- **Exclusions** the user confirmed.
- **Scoring:** default weights unless the user cares about something else; location rank and
  closing date break ties.
Tag everything with `source`: `user-confirmed`, `inferred-from-JDs` or `researched`. Fit labels
(`taxonomy`) should describe this field (for example in-house vs agency vs consultancy; or for
nursing: hospital vs clinic vs agency).

## 4. Validate before saving
Run the saved queries once. Look at the share of relevant results and the spread of fit. Add
synonyms found in good postings, exclusions from bad ones. Show the user a one-screen summary
(titles, seniority, top skills and gaps, locations, platforms, 3-5 example postings) and agree
changes.

## 5. Save
During onboarding (before setup is registered) write the files directly and run
`market.py validate`. Afterwards save through `memory.py apply` with `change_type: "market"`
(json_set on top-level keys; whole-file `write` for MARKET.md), after the user asks to save.
Create one CV variant per core role family when useful (cv.md → variants).

## Refresh and learning
Outcomes and skip reasons feed the map (learn.md → `learn.py stats`): titles that never get
replies, companies that always reject, locations that dominate. Propose specific changes with the
evidence; save only what the user approves.
