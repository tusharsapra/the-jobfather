# Searching platforms

Use for job searches, saved jobs and recommendations. Ordinary LinkedIn messages use outreach.md;
applying uses apply.md. Check the host's actual tool list and `connect.py status`: an install on
one machine is not a login here (connect.md).

## Plan the search
Take titles, locations, seniority and exclusions from market.json (`market.py queries --platform
P --location L` gives a starting query) and the user's request. Ask only for a filter that blocks
a useful search. One small first-page search per platform and title group; fetch full JDs only for
relevant results. No network-wide scans, dozens of repeated queries or background loops.
Platform filters are unreliable: put the location in the keywords (on Indeed, its own location
box) and check each JD; check the real posting date. Never widen the date window to reach a number.

## Sources
- **Company career pages:** public job-board APIs (Greenhouse, Lever, Ashby, Workable,
  SmartRecruiters; endpoints in `references/PLATFORMS.json`) for target companies in market.json.
  No login needed.
- **Browser** (the `browser` server: real Chrome with the user's own logins): career sites, job
  boards and LinkedIn pages. Read a focused snapshot, not whole pages repeatedly.
- **LinkedIn MCP** (only if the user opted in): `search_jobs` returns ids and sections, not a jobs
  array; read shortlisted ids with `get_job_details`; `get_saved_jobs` on request; `search_posts`
  for hiring posts. `section_errors` means partial extraction, not zero results. There is no
  submission tool: applying is a browser hand-off.
- **Naukri** (only if the user opted in; otherwise the user pastes Naukri listings or links and
  job-intake.md reads them): `naukri_status` first (`inconclusive` is not logged
  out), `naukri_search_jobs`, `naukri_get_job`, `naukri_recommended_jobs`, `naukri_saved_jobs`,
  `naukri_similar_jobs`. The server spaces requests and caps daily use; `rate_limited` means wait.
  `checkpoint` or `blocked` means stop and tell the user; never retry.
- **Indeed** (public pages; no connector and no login): the user's country site (`in.indeed.com`,
  `uk.indeed.com`, `ca.indeed.com`, `www.indeed.com` …) through a direct read or the browser.
  `market.py queries --platform indeed` gives the query (`-"term"` excludes). One first-page search
  per title group; open each shortlisted job's `viewjob?jk=<key>` page for the full ad. A search
  card is not a JD: keep its "posted" text exactly as shown and never infer the date or the JD
  from it. Sponsored cards and reposts repeat: the job key (`jk`) is the identity, and
  job-intake.md dedupes on it. Where Indeed says "apply on company site", look for the same
  requisition on the employer's careers page and prefer that listing. A login wall, verification
  page or block means stop on Indeed, tell the user and use the employer's careers page; never
  bypass it. Indeed job-alert emails already in the user's mail can supply leads during a mail task
  the user asked for; never create alerts or scheduled searches.
- **Pasted links and listings** always work (job-intake.md).

## Results
Show strengths and real gaps per job (fit.md logic), not server scores. To keep results, pass them
to job-intake.md with full JD text where available (a search summary is not a JD; fetch details or
mark them missing). Exclude tokens, cookies and private messages from anything saved. A saved row
is not an application.
