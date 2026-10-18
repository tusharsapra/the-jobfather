# jobfather-mcp

MCP servers for [The Jobfather](https://github.com/tusharsapra/the-jobfather), run with `uvx`
from a pinned git source. MIT licence. Python 3.10 or newer.

| Command | What it does |
| --- | --- |
| `jobfather-browser` | Starts [Playwright MCP](https://github.com/microsoft/playwright-mcp) (`@playwright/mcp@0.0.83`) on the workspace's own Chrome profile. |
| `jobfather-proxy` | Puts an allowlist in front of another stdio MCP server (used for the optional LinkedIn server). |
| `jobfather-naukri` | The clean-room Naukri server (built separately; see `dev/CLEANROOM.md` in the repository). |

The plugin manifests start them like this (the pin is a release tag or commit):

```text
uvx --from git+https://github.com/tusharsapra/the-jobfather@v0.1.0#subdirectory=packages/jobfather-mcp jobfather-browser
```

Every server finds the user's workspace the same way the skill does: `JOBFATHER_HOME`, then the
pointer file written at onboarding, then `~/Jobfather`. Diagnostics go to stderr only.

## jobfather-browser

```text
jobfather-browser [--workspace PATH] [--print-argv] [--prefetch] [--version] [-- PLAYWRIGHT ARGS]
```

- `config/browser.json` `{"mode": "profile"}` (default) keeps a dedicated Chrome profile in
  `<workspace>/.state/browser/main`; `{"mode": "extension"}` attaches to the user's running Chrome
  through the Playwright extension. Optional `"channel": "msedge"` uses Microsoft Edge.
- Fixed flags: `--browser chrome --snapshot-mode none --timeout-navigation 30000`, output files in
  `<workspace>/.state/browser/output`.
- LinkedIn (`https://www.linkedin.com;https://linkedin.com`) is blocked unless
  `config/platforms.json` has LinkedIn enabled, acknowledged and in `"browser"` mode. Naukri
  (`https://www.naukri.com;https://naukri.com;https://login.naukri.com`) is always blocked here.
  Playwright's origin blocking is a guard against accidents, not a security boundary.
- Without a workspace it runs with `--isolated` (nothing persists).
- `--print-argv` prints the Playwright command as JSON; `--prefetch` downloads Playwright MCP into
  the npx cache. On POSIX the launcher replaces itself with `npx`; on Windows it runs it and
  passes the exit code through.

## jobfather-proxy

```text
jobfather-proxy --server-name NAME --allow TOOL[,TOOL...] [--daily-cap TOOL=N ...]
                [--startup-timeout SECONDS] [--workspace PATH] -- COMMAND [ARGS...]
```

Starts `COMMAND` as a child MCP server. `tools/list` shows only allow-listed tools (schemas
unchanged), `tools/call` refuses any other tool with a `not_enabled` error, progress and log
notifications pass through, and `--daily-cap` limits calls per local calendar day. Caps are
counted in `<workspace>/.state/proxy/NAME-ledger.jsonl` (tool name and time only, never
arguments) or in memory when there is no workspace. A child that stops is restarted on the next
call; a crash loop is refused. Example (what `connect.py` registers for LinkedIn):

```text
jobfather-proxy --server-name linkedin --allow search_jobs,get_job_details,get_saved_jobs,get_job_apply_url,get_company_profile,search_posts,search_people,get_person_profile,get_my_profile,close_session -- uvx mcp-server-linkedin@4.27.0 --transport stdio --no-auto-import
```

## Library modules for server authors

The Naukri server (and any future server) should use these instead of re-implementing them.
`workspace`, `approvals` and `errors` are standard-library only; `errors.JFError.to_result()`
imports `mcp` lazily.

### `jobfather_mcp.workspace`

| Function | Purpose |
| --- | --- |
| `resolve(explicit=None) -> Path` | Workspace folder (may not exist yet). Raises `WorkspaceError` for unsafe locations or, with `JOBFATHER_TEST` set, when `JOBFATHER_HOME` is missing. |
| `resolve_with_source(explicit=None) -> (source, Path)` | Same, plus where it came from: `argument`, `JOBFATHER_HOME`, `pointer` or `default`. |
| `require(explicit=None) -> Path` | Workspace that is set up (has `config.json`), else `WorkspaceError`. |
| `is_initialized(ws) -> bool` | `config.json` exists. |
| `state_dir(ws, *parts) -> Path` | `<ws>/.state/<parts>`, creating missing folders as 0700; refuses symlinks and never creates the workspace. Use `state_dir(ws, 'naukri')` for server state and `state_dir(ws, 'browser', 'naukri')` for a browser profile. |
| `read_platforms(ws) -> dict` | `config/platforms.json` merged over defaults (all opt-ins off). |
| `read_browser(ws) -> dict` | `config/browser.json` merged over `{"mode": "profile"}`. |
| `platform_enabled(platforms, name) -> bool` | True only when `enabled` is `true` and the opt-in was acknowledged (the user's words were recorded by `connect.py enable`). |
| `read_json(path)`, `config_home()`, `pointer_file()`, `machine_file()`, `default_workspace()` | Helpers matching the skill's `local.py`. |

### `jobfather_mcp.approvals`

Same record format as the skill's `scripts/approvals.py` (`the-jobfather/approval@1`, records in
`<ws>/.state/approvals/<id>.json`, append-only `ledger.jsonl`, HMAC key in
`<ws>/.state/keys/approvals.key`); either side can read and update what the other wrote.

| Function | Purpose |
| --- | --- |
| `verify(ws, approval_id, kind, payload, allowed=('pending',)) -> dict` | Check kind, exact payload (`payload_hash`), expiry, status and single use. |
| `consume(ws, approval_id, kind, payload, detail=None, allowed=('pending', 'confirmed')) -> dict` | Verify and mark consumed under one lock. Call it right before the outward action, so two calls can never use one approval. |
| `mark(ws, approval_id, status, detail=None) -> dict` | Move a record to `confirmed`, `consumed`, `cancelled`, `expired` or `revoked`. |
| `load(ws, approval_id) -> dict`, `ledger(ws) -> list` | Read with MAC check / read the ledger (fails closed if damaged). |
| `create(ws, kind, subject, payload, user_statement, created_by, ttl_minutes=15)` | Same rules as the skill (policy mode, the user's own words, 1-60 minutes). Normally the skill creates approvals. |
| `payload_hash(payload)`, `canonical(value)`, `policy_mode(ws)` | Shared format helpers. |

Kinds: `email_send`, `ats_submit`, `naukri_apply`, `linkedin_message`. Every failure raises
`ApprovalError` (a `JFError` and a `ValueError`) with code `approval_required`,
`approval_invalid`, `approval_expired`, `approval_consumed` or `approval_mismatch`.

### `jobfather_mcp.errors`

`JFError(code, message, retryable, retry_after_s=None, user_action=None)`. Codes: `not_enabled`,
`workspace_invalid`, `invalid_argument`, `browser_unavailable`, `profile_locked`, `auth_required`,
`auth_inconclusive`, `checkpoint`, `blocked`, `rate_limited`, `site_changed`, `not_found`,
`closed`, `approval_required`, `approval_invalid`, `approval_expired`, `approval_consumed`,
`approval_mismatch`, `flow_expired`, `timeout`, `internal`.

| Helper | Purpose |
| --- | --- |
| `JFError.to_result()` | `CallToolResult` with `isError: true`; the error object is the JSON text block and `structuredContent` (`{"error": {code, message, retryable, retry_after_s, user_action}}`). |
| `error_result(code, message, retryable, ...)` | Build that result directly. |
| `tool_errors` | Decorator for `MCPServer` tools (sync or async): `JFError` becomes its result, unexpected exceptions become `internal` (details only in the server log), `MCPError` still propagates. Put it under `@app.tool()`. |
| `as_jferror(exc, tool=None)`, `result_for(exc, tool=None)` | Map any exception (timeouts become `timeout`). |

```python
from mcp.server import MCPServer
from jobfather_mcp.errors import JFError, tool_errors
from jobfather_mcp.workspace import read_platforms, platform_enabled, require

app = MCPServer('jobfather-example')

@app.tool()
@tool_errors
async def example_status() -> dict:
    ws = require()
    if not platform_enabled(read_platforms(ws), 'naukri'):
        raise JFError('not_enabled', 'Naukri is off until the user opts in.', False,
                      user_action='Run the connect workflow to show the notice and record the opt-in.')
    return {'ok': True}
```

## Development

```text
python -m unittest discover -s packages/jobfather-mcp/tests     # from the repository root, with mcp installed
uv run --directory packages/jobfather-mcp jobfather-browser --print-argv
```

Set `JOBFATHER_DEV_MCP_SOURCE=<repo>/packages/jobfather-mcp` before `connect.py plan` to register
`uv run --directory …` commands instead of the pinned git source.
