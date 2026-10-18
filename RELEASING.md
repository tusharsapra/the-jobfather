# Releasing

1. On `main` with a clean tree: `python3 dev/release.py prepare X.Y.Z`. It bumps every version,
   dates the changelog, runs the tests, the package validator and the full-history privacy scan
   (with the maintainer's private denylist), and commits "Release vX.Y.Z".
2. `python3 dev/release.py pin`: pins the MCP sources in `.mcp.json`, `.codex-mcp.json` and
   `mcp_config.json` to that commit's SHA, commits, and tags `vX.Y.Z`.
3. `python3 dev/release.py verify`.
4. Review, then push: `git push --atomic origin main vX.Y.Z`. The tag workflow runs the pinned MCP
   package from a clean cache.
5. Users update with their host's plugin update command (`/plugin` in Claude Code,
   `codex plugin marketplace upgrade` in Codex) or `npx skills update`.

Never rewrite a published tag; publish a new version instead.
