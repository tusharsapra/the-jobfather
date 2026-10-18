# Connect services (logins)

Everything here is optional. Without connections The Jobfather still works from pasted listings,
links and files. Each service is checked in four separate states: **installed**, **visible to the
agent**, **logged in**, **verified** (a real read worked). Never report one as another.

Rules: never ask for passwords or one-time codes in chat (the user types them in the browser
window); never retry a blocked account or work around a checkpoint; never change host config
without the user's consent for that exact step; log every change.

## 1. Where are we
`python3 SKILL/scripts/connect.py detect` (say which host you are: `--host claude-code|codex|
antigravity|cursor|other`). It reports the host, how The Jobfather is installed (plugin, skills
folder or developer checkout) and duplicates. `connect.py status` shows what is already connected.
`python3 SKILL/scripts/doctor.py` covers prerequisites: `uv` (runs the MCP servers), Node/npx
(browser automation), Google Chrome.

## 2. Choose services and opt in
Offer: **browser** (real Chrome with the user's own logins; used for career sites, ATS forms,
LinkedIn browsing), **LinkedIn**, **Naukri** (from v0.2.0; `connect.py` says so until then),
**mail** (Gmail). For LinkedIn and Naukri show the
notice first: automating a platform can break its terms and an account can be restricted; The
Jobfather keeps to slow, human-paced use and never applies without a confirm click. Record the
user's own words: `connect.py enable --platform naukri --ack "<their words>"`.
LinkedIn defaults to the browser. The third-party LinkedIn MCP server is an extra opt-in
(`--linkedin-mode mcp`): it is Apache-2.0, read-only through our allowlist proxy, but it uses
Patchright, a browser library built to avoid bot detection. Say so before offering it.

## 3. Plan, consent, run
`connect.py plan --host HOST --services browser,naukri,linkedin,gmail [--linkedin-mode mcp]`
writes the steps to `<workspace>/.state/connect-plan.json` (it detects the install mode; override
with `--install plugin|skills|dev`). Show each step in one line (what it changes, how to undo).
Run each only with consent: `connect.py run --plan <that file> --step ID --consent "<their words>"`.
`connect.py disable --platform naukri|linkedin` turns an opt-in off again.
- Plugin installs register the browser server automatically (and the Naukri server from v0.2.0).
- Skills-folder installs register them with the host's own CLI (`claude mcp add-json`,
  `codex mcp add`, `agy mcp add`), plus tool allowlists and an approval prompt for applying.
- Mail: Claude Code uses the claude.ai Gmail connector (claude.ai → Settings → Connectors →
  Gmail); Codex uses `codex plugin add gmail@openai-curated`; other hosts use a Gmail MCP with the
  user's own Google OAuth client (secrets stay in the host config, never in the workspace).
- `connect.py prewarm` downloads servers before a restart so the first start is fast.

## 4. Restart and resume
After registering, tell the user exactly what to do (Claude Code: `/reload-plugins` or restart;
Codex: new thread or restart; Antigravity: restart) and stop. `connect.py resume` picks up where
the plan left off. The router offers this automatically when `connect_resume=yes`.

## 5. Log in and verify (record each state)
- **Browser:** open a public page, then let the user sign in to the sites they want inside that
  Chrome window. Check one logged-in page per site.
- **Naukri:** `naukri_login` opens Chrome on the login page; the user signs in with Naukri's own
  email/OTP login (Google sign-in may be refused in automated Chrome). Then `naukri_status` with
  `check: "live"`, then a 5-result search.
- **LinkedIn (MCP mode):** the user runs the login command shown by the plan in a terminal; then
  `get_my_profile` (store only ok/not ok) and one job search.
- **Mail:** with the user's OK, read one recent sent message (`in:sent`, newest) to confirm access
  and capture their real signature into `profile/signature.html` (plus `profile/signature.json`
  with source and date). No test email is sent.
Record each result: `connect.py record --host HOST --service S --check installed|visible|
authenticated|verified --result yes|no|inconclusive`.

## Doctor and re-login
`doctor.py` flags `reauth_needed`, `stale` (Naukri login older than 7 days, LinkedIn 14),
`restart_pending` and `not_connected`. Offer the specific fix; do not reset profiles.
