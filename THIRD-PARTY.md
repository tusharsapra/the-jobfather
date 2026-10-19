# Third-party work

## Included in this repository

### Humanizer (MIT)

`plugins/the-jobfather/skills/the-jobfather/third_party/humanizer/HUMANIZER.md` is
[blader/humanizer](https://github.com/blader/humanizer) version 3.1.0, renamed from `SKILL.md` so
agents do not load it as a separate skill; the text is otherwise unchanged. Copyright (c) 2025
Siqi Chen, MIT licence, full text in `third_party/humanizer/LICENSE`.

### Adapted job-search workflow concepts (MIT)

The ranking, research-cache, mail-signal and follow-up concepts in
`core/workflows/job-pipeline.md`, `references/JOB-PIPELINE.md` and `scripts/job_pipeline.py` are
adapted from Mads Lorentzen's [ai-job-search](https://github.com/MadsLorentzen/ai-job-search),
reviewed at revision a011214d60f943770e33f7d5f070e1a5f91e3822 (sources: `.claude/commands/rank.md`,
`gmail-sync.md`, `outcome.md` and `.claude/skills/job-application-assistant/04-job-evaluation.md`).
The upstream project is not installed or vendored; its profile, templates, commands and runtimes
are not included.

```text
MIT License

Copyright (c) 2026 Mads Lorentzen

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Logos in the README

`docs/works-with-light.svg` and `docs/works-with-dark.svg` use brand logo paths from
[Simple Icons](https://github.com/simple-icons/simple-icons) 16.35.0 (CC0-1.0), in each brand's own
colour. The names and logos are trademarks of their owners and appear only to say what The
Jobfather works with; brands that Simple Icons does not carry are shown by name.

## Downloaded and run on the user's machine (not included)

| Component | Licence | Used for |
| --- | --- | --- |
| [@playwright/mcp](https://github.com/microsoft/playwright-mcp) (pinned) | Apache-2.0 | the `browser` MCP server |
| [Playwright for Python](https://github.com/microsoft/playwright-python) | Apache-2.0 | Naukri server, Greenhouse form filler |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | MIT | the bundled MCP servers |
| [pydantic](https://github.com/pydantic/pydantic) | MIT | tool schemas |
| [openpyxl](https://foss.heptapod.net/openpyxl/openpyxl) | MIT | the tracker workbook |
| [tzdata](https://github.com/python/tzdata) | Apache-2.0 | time zones where the OS lacks them |
| [mcp-server-linkedin](https://github.com/stickerdaniel/linkedin-mcp-server) (optional, pinned) | Apache-2.0 | LinkedIn MCP mode, behind the allowlist proxy |
| [workspace-mcp](https://github.com/taylorwilsdon/google_workspace_mcp) (optional) | MIT | Gmail on hosts without a Gmail connector |
| [uv](https://github.com/astral-sh/uv) | Apache-2.0 or MIT | runs the MCP servers |
| TeX distribution with pdfLaTeX, the `carlito` font package and standard LaTeX packages | LPPL, SIL OFL 1.1 | CV PDFs |
| Poppler utilities | GPL-2.0 | PDF checks |

## Not used

The Jobfather's Naukri server is an independent, clean-room implementation (see
`dev/CLEANROOM.md`). It does not use, copy or derive from any other Naukri automation project.
