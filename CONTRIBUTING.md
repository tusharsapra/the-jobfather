# Contributing

Thanks for helping. A few rules keep this project safe for the people who use it.

## Never commit personal data

- Use the fictional persona in `examples/demo-workspace` (or invent your own clearly fictional one)
  for tests, docs and examples. Emails use `example.com`, `example.test` or a `.example` domain;
  phone numbers use the 555-01xx range.
- Install the hooks once: `python3 dev/install_hooks.py`. Every commit and push runs
  `dev/privacy_scan.py` (emails, phones, home paths, profile links, secrets, binaries, metadata).
- Maintainers also keep a private denylist outside the repository
  (`dev/build_denylist.py`); contributors do not need one.
- Never commit PDFs, images, spreadsheets, archives or browser data; `.gitignore` blocks them.

## Development

- Skill scripts: standard-library Python 3.9+. Run `python3 -m unittest discover -s tests`.
- MCP package (`packages/jobfather-mcp`): Python 3.10+, run its tests with
  `uv run --directory packages/jobfather-mcp python -m unittest discover -s tests`.
- PDF tests run only where pdfLaTeX and Poppler are installed.
- Tests must be hermetic: use `tests/support.py`'s `DemoWorkspace`, never a real workspace.
- Keep the skill portable: no host-specific assumptions in `SKILL.md` or the workflows.
- Prompts (`SKILL.md`, router, workflows) are the product: keep them short, plain and exact.
- The Naukri server follows `dev/CLEANROOM.md`; do not look at other Naukri automation code while
  working on it.

## Pull requests

Describe the user-visible change, how you tested it, and update `CHANGELOG.md`.
