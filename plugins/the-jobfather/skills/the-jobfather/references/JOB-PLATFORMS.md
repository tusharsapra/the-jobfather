# Platform notes

Which sources suit a user depends on field and region: start from `PLATFORMS.json`, then record
what works in market.json. Automating a site can break its terms of use; The Jobfather keeps
platform automation opt-in, slow and read-mostly, and every submission needs the user's own click.

## Browser server (`browser`)
Playwright MCP (pinned) driving real Google Chrome with a dedicated profile in the workspace
(`.state/browser/main`), or the user's everyday Chrome in extension mode. The user signs in to
sites inside that window; sessions persist. A fresh profile starts logged out. LinkedIn is blocked
in this browser until the user enables it; Naukri is always blocked here and goes through its own
server.

## LinkedIn
Default: the browser. Optional: the third-party `mcp-server-linkedin` (Apache-2.0) behind
The Jobfather's allowlist proxy with read tools only: `search_jobs`, `get_job_details`,
`get_saved_jobs`, `get_job_apply_url`, `get_company_profile`, `search_posts`, `search_people`,
`get_person_profile`, `get_my_profile`, `close_session`. It uses Patchright, a Playwright fork made
to avoid bot detection, and imports browser cookies unless `--no-auto-import` is set (The
Jobfather always sets it). Messaging and connection tools are excluded.

## Naukri
The Jobfather's own clean-room server (`naukri`). Opt-in with a terms notice. Tools:
`naukri_status`, `naukri_login`, `naukri_search_jobs`, `naukri_recommended_jobs`, `naukri_get_job`,
`naukri_similar_jobs`, `naukri_saved_jobs`, `naukri_my_profile` (no contact fields),
`naukri_prepare_apply`, `naukri_apply` (needs an approval id and the user's click on a confirm
page), `naukri_close`. Fixed spacing between requests and daily caps; stops at any checkpoint.
Applying uses the resume already on the user's Naukri profile, not a local PDF.

## Mail
Claude Code: claude.ai Gmail connector. Codex: `gmail@openai-curated`. Others: a Gmail MCP with the
user's own OAuth client. Connectors cannot read Gmail's signature settings, so the real signature
is captured once into `profile/signature.html`. Draft creation can hit per-minute limits.

## Public job-board APIs
No login, designed for listing jobs (endpoints in `PLATFORMS.json`). Best for target companies.
Greenhouse also publishes each job's application questions (`?questions=true`), which the ATS
flow uses; applicant submissions still happen in the hosted form.
