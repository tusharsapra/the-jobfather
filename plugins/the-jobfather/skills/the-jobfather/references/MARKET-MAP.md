# market.json

`profile/market.json` is the user's job-market model. The market-map workflow writes it; it changes
only through `memory.py` (change type `market`). `scripts/market.py validate` checks it. Every item
that came from somewhere carries `source`: `user-confirmed`, `inferred-from-JDs` or `researched`.
A readable summary lives in `profile/MARKET.md`.

```json
{
  "schema_version": 1,
  "taxonomy": {
    "employer_type": {"values": {"in_house": "Hiring for its own team", "agency": "Serves clients"}},
    "role_track":    {"values": {"analytics": "…", "bi_reporting": "…", "hybrid": "…"}}
  },
  "role_families": [
    {"id": "data-analytics", "name": "Data analytics", "status": "core", "cv_variant": "data-analytics",
     "titles": [{"title": "Data Analyst", "status": "core", "source": "user-confirmed"}],
     "synonyms": ["BI Analyst"], "regional_variants": []}
  ],
  "seniority": {"band": {"min_years": 4, "max_years": 9}, "stretch_years": 2, "junior_floor_years": 3,
                "ladders": {"in_house": [{"title": "Analyst", "years": "0-2"}]}},
  "skills": {"must": [{"name": "SQL", "evidence": ["ABC-01"], "source": "inferred-from-JDs"}],
             "nice": [], "never_done": [{"name": "Spark", "source": "user-confirmed", "confirmed_at": "2026-01-01"}]},
  "company_types": [{"id": "in_house", "where_they_post": ["careers pages"], "outreach": "…",
                     "typical_titles": ["…"], "cv_variant": "data-analytics"}],
  "target_companies": [{"name": "Example Corp", "type": "in_house", "careers_url": "https://…",
                        "ats": "greenhouse", "board_token": "examplecorp", "source": "researched"}],
  "geography": {"locations": [{"name": "City", "match": ["city", "metro area"], "rank": 1,
                               "disclose_relocation": false,
                               "pay_floor": {"amount": 0, "currency": "XXX", "period": "year"}}],
                "remote": "open", "timezone": "Area/City"},
  "platforms": [{"id": "linkedin", "enabled": true, "time_window_days": 7, "notes": "…"}],
  "queries": [{"id": "core-titles", "platform": "linkedin", "query": "(\"A\" OR \"B\") NOT (\"C\")",
               "location": "City", "posted_within_days": 7, "source": "user-confirmed"}],
  "exclusions": [{"kind": "title|seniority|sector|skill|pay|notice|shift|relocation|company|contract|other",
                  "value": "…", "source": "user-confirmed", "confirmed_at": "…"}],
  "scoring": {"weights": {"title": 3, "must_skills": 3, "seniority": 2, "location": 2, "nice_skills": 1,
                          "freshness": 1},
              "tie_breaks": ["priority", "closing_soon", "location_rank", "network", "published_contact", "job_id"]},
  "ats_senders": ["greenhouse.io", "lever.co"],
  "evidence": {"postings_analysed": 0, "analysed_at": null, "keyword_frequencies": {}}
}
```

## What each part is for

1. **Title graph** (`role_families`): every title and synonym the user would accept, tagged core,
   adjacent, stretch or excluded. Searches use core titles first; excluded titles become NOT terms.
2. **Seniority**: the user's band, how far to stretch, and the junior floor. Years written in a JD
   beat the title.
3. **Skills**: must-have and nice-to-have skills for the target roles, each mapped to the claim IDs
   that prove it, plus a confirmed "never done" list (used to flag real gaps honestly).
4. **Company types**: where each type posts, how outreach reaches it (including shared group
   inboxes), typical titles and which CV variant to start from.
5. **Geography**: ordered locations with match words (rank 1 is preferred), remote stance, pay
   floor per location, what may be said about relocation, timezone.
6. **Platforms**: which sources work for this user and region (from `references/PLATFORMS.json`),
   with notes on filter reliability and the time window that works.
7. **Queries**: saved searches per platform. `market.py queries --platform P` builds first-page
   starting points from the title graph; refine and save the ones that work.
8. **Exclusions**: things the user ruled out. "Never widen the date window to hit a target" is
   always on.
9. **Scoring and tie-breaks**: weights and the order used after fit when ranking jobs.

`taxonomy` defines the labels fit analyses use; without it a generic set applies
(employer_type: direct_employer, agency, recruitment_agency, consultancy; role_track: core,
adjacent, stretch). Renaming taxonomy values marks older analyses stale.
