# EXPERIENCE.md format

EXPERIENCE.md is the source of truth for career facts. Every CV line, letter and answer must trace
to a claim here. `scripts/evidence.py` parses it; `scripts/cv_init.py` renders the CV from it.
See `examples/demo-workspace/profile/EXPERIENCE.md` in the repository for a complete example.

## Sections

```markdown
# Experience and evidence

## CV summary

Two or three sentences for the top of the CV. **Bold** is allowed. End with the claim IDs that
support it. [ABC-01, ABC-02]

## [ABC] Org name                      <- one section per role, group, degree, project, certificate
```jobfather-entry
kind: role                            # role | group | education | project | certification
org: Org name
title: Job title                      # required for role and education (degree name)
location: City, Region
start: 2022-04                        # YYYY-MM or YYYY
end: present                          # YYYY-MM, YYYY or present
cv: tailor                            # tailor (editable per job) | locked | omit
cv_weight: primary                    # primary | secondary | light (only with cv: tailor)
```

### Claims

| ID | Claim | Scope / metric definition |
| --- | --- | --- |
| ABC-01 | What the user did. | How the number was measured, period, share of credit. |

### Historical detail

- **ABC-H01:** Older or private detail that does not belong on the CV.

### CV bullets

1. Built a **thing** that improved a **measured result**. [ABC-01]

## CV skills

```jobfather-cv-lists
core_skills: Skill one; Skill two; Skill three
tools: Tool one; Tool two; Tool three
```

## Tools and platforms

```jobfather-vocabulary tools
Every tool the user has really used; CV tool lists may only use these.
```
```

## Rules

- The prefix in `## [ABC]` is 2-8 capital letters or digits, starting with a letter. It must be
  unique. Reserved: APP, JOB, SRC, TX, EV, SIG, MAIL, OUT, IN, EM, INIT, REQ, HR, CV, EXT, ID.
- Claim IDs are `PREFIX-NN` (or `PREFIX-HNN` for historical detail), defined inside their own
  section, either as the first cell of a claims-table row or as `- **ID:**` bullets.
- IDs are never deleted. To withdraw a claim, start its text with `(retired <date>)`.
- Any other mention of an ID is a reference and must resolve to a defined claim.
- `cv: tailor` entries need `### CV bullets`; their bullet count is fixed in the CV manifest
  (tailoring rewrites bullets, it does not add or remove them).
- Metric scope lives in the claims table: what was measured, when, and how much credit the user
  can take. Writing must keep that scope.
- Guidance inside `<!-- comments -->` is ignored by the parser.
- After setup, any change that alters CV text must re-render the master CV in the same update
  (`cv_init.py render`, then a write operation); memory.py checks this.
