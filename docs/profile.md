# Opportunity radar profile (`profile.md`)

The radar's profile path is `KEEPER_DATA_DIR/memory/profile.md` (normally `data/memory/profile.md` locally or `/sandbox/keeper/data/memory/profile.md` in the sandbox). This is a user-maintained profile, separate from the append-only topic notes managed by `keeper memory add`. Keep a real profile on the private data volume; `data/` is ignored by Git. [The demo profile](../demo/profile.md) is synthetic.

Use the headings and labels below exactly. List values are comma-separated. `Not specified` means the user has not supplied a value; it must never be interpreted as a negative eligibility finding. `None` means the user explicitly has no constraint or exclusion for that field. Do not infer profile facts from an inbox message, resume, name, or location unless the user confirms them for the profile.

| Heading and field | Value | Radar use |
| --- | --- | --- |
| `## Target roles` / `- Roles:` | One or more role names, or `Not specified` | Search terms and relevance scoring; synonyms may broaden search but do not change the recorded roles. |
| `## Education` / `- Graduation year:` | Four-digit year or `Not specified` | Compare only with an explicit graduation-year requirement. |
| `## Education` / `- Major:` | User-supplied major or majors, or `Not specified` | Compare only with an explicit major or field-of-study requirement. |
| `## Location constraints` / `- Acceptable locations:` | Cities, regions, countries, `Any`, or `Not specified` | Reject only a posting whose required location clearly conflicts with a stated constraint. `Any` is an explicit lack of geographic restriction. |
| `## Location constraints` / `- Remote:` | `Allowed`, `Required`, `Not allowed`, or `Not specified` | Apply only when the posting clearly states its work arrangement. |
| `## Location constraints` / `- Relocation:` | `Allowed`, `Not allowed`, or `Not specified` | Apply only to an explicitly on-site or relocation-required posting. |
| `## Work authorization` / `- Voluntarily supplied:` | `Yes` or `No` | `No` means authorization details are absent. Never infer status or mark a result ineligible for that absence. |
| `## Work authorization` / `- User statement:` | User's exact relevant statement, or `Not specified` | Include only when `Voluntarily supplied: Yes`. Compare with explicit posting requirements; do not rewrite it into an inferred immigration category. |
| `## Opportunity preferences` / `- Types:` | Internship, co-op, new graduate, hackathon, fellowship, etc., or `Not specified` | Search and rank matching types. |
| `## Opportunity preferences` / `- Preferred topics:` | User-supplied interests, or `None` / `Not specified` | Rank only; these are not eligibility rules. |
| `## Opportunity preferences` / `- Exclude:` | Explicitly unwanted opportunity types or topics, or `None` / `Not specified` | Filter only clear matches to an exclusion and record the reason. |

Eligibility is `ineligible` only for a demonstrated conflict between a stated profile constraint and an explicit posting requirement. Missing profile values, vague posting requirements, or an absent authorization statement yield `unverified` for the affected condition, with a short reason. Preferences influence discovery and ranking; they do not prove eligibility. Record the source and reason for each skip. Do not send the full profile or its authorization statement to search queries or logs; build search terms from roles, opportunity types, topics, and broad locations only.
