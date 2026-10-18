# Clean-room rules for the Naukri MCP server

`packages/jobfather-mcp/src/jobfather_mcp/naukri/` is an independent implementation.
It must not use, copy, translate or be derived from
[`Sundeepg98/naukri-mcp`](https://github.com/Sundeepg98/naukri-mcp), whose licence is
"all rights reserved" and grants no right to use, copy, run or distribute it.

## Roles

- **Spec author.** Writes only the capability list and behaviour notes. These come from
  public product knowledge and from our own workflow requirements: what a job seeker
  needs (status, login, search, details, similar, saved, recommendations, a read-only
  profile summary, and an apply flow gated by human approval). The spec author has seen the
  upstream project's README summary and licence text, and no upstream source code.
- **Implementer.** A separate session or person who has never opened the upstream
  repository, any clone or fork, its virtualenv or data directory, cached copies of its
  tool schemas, or any launcher, probe or log produced by running it.

## Tainted material (never open while working on this package)

- the upstream repository, every fork, and the scoring-engine package it depends on;
- any local clone, virtualenv, data or profile directory created by installing it;
- cached tool catalogues or schemas of that server saved by any MCP host;
- launchers, probes, catalogues, logs and receipts written while running it.

Maintainers keep the machine-specific paths of tainted material in a private file
outside this repository and configure their agent's read-deny rules from it.
`dev/taint-hashes.txt` holds SHA-256 hashes of upstream-internal identifiers;
`dev/privacy_scan.py` fails if any token in the repository hashes to one of them.

## How page structure is learned

1. `dev/naukri_recorder.py` opens real Chrome with a developer profile
   (`~/.cache/jobfather-dev/naukri-profile`). The developer logs in by hand.
2. It passively records page structure and JSON responses for search, job detail,
   saved, similar, recommended and profile pages. It never records request headers or
   cookies and never clicks Apply. Apply/questionnaire structure is captured only while
   the developer applies by hand to a job they actually want.
3. Captures stay in `~/.cache/jobfather-dev/captures/`, outside the repository.
4. `dev/sanitize_fixtures.py` turns captures into synthetic fixtures. It keeps keys and
   shapes, replaces free text and identifiers, and refuses to write if
   `dev/privacy_scan.py` finds anything.

Selectors and URL templates live as data in `site_profile.json`, versioned by date.

## Attestation

Commits that touch the Naukri package carry the trailer `Clean-Room-Attested: yes`,
meaning the author followed these rules.
