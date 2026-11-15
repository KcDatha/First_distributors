# Cost of the dev run

Real numbers from `python -m rfqparse cost` (read from the committed `cache/` entries; no key needed), prices from
`rfqparse/config.py`: gpt-4.1-mini $0.40 in / $1.60 out per 1M tokens, gpt-4.1 $2.00 in / $8.00 out per 1M.
One row per email. E008 is a duplicate of E006, found in code, so it makes no call and has no row.

| Email | Model | Calls | Prompt tok | Completion tok | USD | Note |
|---|---|---|---|---|---|---|
| E001 | gpt-4.1-mini | 1 | 3,181 | 387 | $0.0019 | |
| E002 | gpt-4.1-mini | 1 | 2,996 | 290 | $0.0017 | |
| E003 | gpt-4.1-mini | 1 | 3,098 | 544 | $0.0021 | |
| E004 | gpt-4.1-mini | 1 | 3,266 | 546 | $0.0022 | |
| E005 | gpt-4.1-mini | 1 | 3,566 | 686 | $0.0025 | |
| E006 | gpt-4.1-mini | 2 | 10,016 | 3,611 | $0.0098 | **second pass** (completeness check found table rows with no line) |
| E007 | gpt-4.1 | 1 | 3,764 | 340 | $0.0102 | image in payload |
| E009 | gpt-4.1 | 1 | 4,599 | 2,527 | $0.0294 | scanned PDF pages sent as images |
| E010 | gpt-4.1 | 1 | 3,587 | 348 | $0.0100 | image in payload |
| E011 | gpt-4.1-mini | 1 | 3,325 | 25 | $0.0014 | |
| E012 | gpt-4.1-mini | 1 | 3,229 | 43 | $0.0014 | |
| E013 | gpt-4.1-mini | 1 | 3,064 | 36 | $0.0013 | |
| E014 | gpt-4.1-mini | 1 | 3,199 | 37 | $0.0013 | |
| E015 | gpt-4.1-mini | 1 | 3,469 | 597 | $0.0023 | |
| E016 | gpt-4.1-mini | 1 | 3,323 | 521 | $0.0022 | |
| E017 | gpt-4.1 | 1 | 3,284 | 74 | $0.0072 | image in payload (the logo) |
| E018 | gpt-4.1-mini | 1 | 3,118 | 510 | $0.0021 | |
| E019 | gpt-4.1-mini | 1 | 3,297 | 698 | $0.0024 | |
| E020 | gpt-4.1-mini | 1 | 3,330 | 277 | $0.0018 | |
| E021 | gpt-4.1-mini | 1 | 4,162 | 3,362 | $0.0070 | |
| E022 | gpt-4.1-mini | 1 | 3,039 | 55 | $0.0013 | |
| E023 | gpt-4.1 | 1 | 3,794 | 150 | $0.0088 | image in payload |
| E024 | gpt-4.1-mini | 1 | 3,053 | 447 | $0.0019 | |
| **Total** | | **24** | **84,759** | **16,111** | **$0.1121** | |

- Average: $0.0049 per email that made a call (23 emails), $0.0047 over all 24 emails in the inbox.
- Second pass: E006 only. Escalation (text model failed validation, retried once on gpt-4.1): none.
- The five gpt-4.1 emails (E007, E009, E010, E017, E023) cost $0.0656, 59% of the total, for 5 of 24 emails.
- The ledger (`python -m rfqparse cost --ledger`) shows the same $0.1121 as real spend; every email was also replayed
  once from the cache (24 hits at $0).
- Budget guard: a run stops making real calls at $2.00 (`MAX_USD_PER_RUN`).
