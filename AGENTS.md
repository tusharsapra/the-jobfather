# Guide for agents working on this repository

This repo is The Jobfather: an agent skill (Markdown prompts + standard-library Python scripts) and
a small MCP package. Read `README.md` first.

## Rules

1. **No personal data, ever.** Use the fictional demo persona. Run
   `python3 dev/privacy_scan.py --paths <changed files>` before finishing and fix every finding.
2. **Clean room for the Naukri server.** Never open other Naukri automation projects or their caches
   (`dev/CLEANROOM.md`).
3. **The skill is portable.** Do not tie instructions to one agent host. Paths resolve from the
   skill folder; user data resolves through `scripts/local.py`'s `workspace()`.
4. **Scripts** are stdlib-only and Python 3.9-compatible; they write through `local.py` helpers
   (atomic, byte-exact, workspace-relative POSIX paths). Profile files change only through
   `memory.py`.
5. **Tests** use `tests/support.py` `DemoWorkspace`; run `python3 -m unittest discover -s tests`.
6. **Safety model:** nothing outward without per-item approval inside the user's policy mode; never
   weaken these checks to make a feature work.

## Key files

- `plugins/the-jobfather/skills/the-jobfather/SKILL.md`: entry prompt.
- `.../core/ROUTER.md` and `.../core/workflows/*.md`: the product.
- `.../references/`: schemas and rules the workflows load on demand.
- `.../scripts/`: storage, validation, CV engine, pipeline, connectors' helpers.
- `packages/jobfather-mcp/`: MCP servers launched with uvx.
- `examples/demo-workspace/`: fictional user.
- `dev/`: privacy scanner, denylist builder, hooks, package validator, release tooling.
