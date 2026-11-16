# Running the RFQ parser

All commands run from `candidate-package/` (the git repo root is one level up).

## Setup

Python 3.11, Windows or macOS.

```
python -m venv .venv
.venv\Scripts\activate          # Windows (cmd/PowerShell)
source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
```

## Parse

```
python -m rfqparse parse --emails data/dev/emails --out parsed.json
python data/check_format.py parsed.json --emails data/dev/emails
```

`--emails` is required and must be an existing folder (otherwise: error, exit 2). A missing folder for `--out` is
created. A second file, `parsed.notes.json`, is written next to the output; it lists what the code guards changed
(not part of the graded output). The exit code is 0 whatever is wrong inside an email.

## With no API key

Nothing to set up. Without `OPENAI_API_KEY` the parser answers from the committed `cache/` folder and makes 0 model
calls. Every email whose exact input is in the cache gets the same answer as the original run. An email that is not
in the cache is returned as an `rfq` flagged `needs_review` with the reason, never dropped. To check this on your
machine:

```
python eval/replay_check.py
```

It runs parse twice without a key and prints PASS only if both runs exit 0, make 0 calls, answer every email from
the cache and write identical bytes.

## New emails (real model calls)

Set the key in the shell, then run the same parse command:

```
set OPENAI_API_KEY=sk-...          # Windows cmd   (PowerShell: $env:OPENAI_API_KEY="sk-...")
export OPENAI_API_KEY=sk-...       # macOS / Linux
```

Every call is cached in `cache/` (one JSON file per call), so a rerun on the same inputs makes no new calls. The key
is never written to the cache, the ledger or the notes.

## Tests and eval

```
python -m unittest discover -s tests -v
python eval/test_score.py
python eval/score.py parsed.json --key data/dev/answer_key.json --per-email
python -m rfqparse cost
```

The tests use fake clients: no key, no network, no writes to `cache/` or `cost_ledger.jsonl`.
`cost` reads the cache entries; `cost --ledger` reads `cost_ledger.jsonl`.

## Models and why

- Text-only emails: `gpt-4.1-mini` ($0.40 in / $1.60 out per 1M tokens). Cheap, and enough for text.
- An email with an image in the payload (a photo, a screenshot, a scanned PDF page): `gpt-4.1` ($2.00 / $8.00),
  chosen for vision quality on handwriting and dense scans (a judgment call, not benchmarked against the mini model here). Routing looks at what is actually sent,
  not at the attachment list.
- If the text model's answer fails validation, the email is retried once on `gpt-4.1`.
- Temperature 0 on both. Prompt version `v5`, schema `v2` (`rfqparse/config.py`); both are part of the cache key.

## Cost

Dev inbox (24 emails, 23 calls plus one second pass): **$0.1121 total, about $0.005 per email**. The numbers below
come from `python -m rfqparse cost` (read from the committed `cache/`, no key needed). Prices are in
`rfqparse/config.py`. E008 is a duplicate of E006, found in code, so it makes no call and has no row.

| Email | Model | Calls | Prompt tok | Completion tok | USD | Note |
|---|---|---|---|---|---|---|
| E001 | gpt-4.1-mini | 1 | 3,181 | 387 | $0.0019 | |
| E002 | gpt-4.1-mini | 1 | 2,996 | 290 | $0.0017 | |
| E003 | gpt-4.1-mini | 1 | 3,098 | 544 | $0.0021 | |
| E004 | gpt-4.1-mini | 1 | 3,266 | 546 | $0.0022 | |
| E005 | gpt-4.1-mini | 1 | 3,566 | 686 | $0.0025 | |
| E006 | gpt-4.1-mini | 2 | 10,016 | 3,611 | $0.0098 | second pass (completeness check) |
| E007 | gpt-4.1 | 1 | 3,764 | 340 | $0.0102 | image in payload |
| E009 | gpt-4.1 | 1 | 4,599 | 2,527 | $0.0294 | scanned PDF pages sent as images |
| E010 | gpt-4.1 | 1 | 3,587 | 348 | $0.0100 | image in payload |
| E011 | gpt-4.1-mini | 1 | 3,325 | 25 | $0.0014 | |
| E012 | gpt-4.1-mini | 1 | 3,229 | 43 | $0.0014 | |
| E013 | gpt-4.1-mini | 1 | 3,064 | 36 | $0.0013 | |
| E014 | gpt-4.1-mini | 1 | 3,199 | 37 | $0.0013 | |
| E015 | gpt-4.1-mini | 1 | 3,469 | 597 | $0.0023 | |
| E016 | gpt-4.1-mini | 1 | 3,323 | 521 | $0.0022 | |
| E017 | gpt-4.1 | 1 | 3,284 | 74 | $0.0072 | image in payload (a logo) |
| E018 | gpt-4.1-mini | 1 | 3,118 | 510 | $0.0021 | |
| E019 | gpt-4.1-mini | 1 | 3,297 | 698 | $0.0024 | |
| E020 | gpt-4.1-mini | 1 | 3,330 | 277 | $0.0018 | |
| E021 | gpt-4.1-mini | 1 | 4,162 | 3,362 | $0.0070 | |
| E022 | gpt-4.1-mini | 1 | 3,039 | 55 | $0.0013 | |
| E023 | gpt-4.1 | 1 | 3,794 | 150 | $0.0088 | image in payload |
| E024 | gpt-4.1-mini | 1 | 3,053 | 447 | $0.0019 | |
| **Total** | | **24** | **84,759** | **16,111** | **$0.1121** | |

- Average: $0.0049 per email that made a call (23 emails), $0.0047 over all 24 emails in the inbox.
- The five `gpt-4.1` emails (E007, E009, E010, E017, E023) cost $0.0656, 59% of the total.
- Escalation (text model failed validation, retried once on `gpt-4.1`): none on dev.
- `cost --ledger` shows the same $0.1121 as real spend. Each replay appends cache-hit lines ($0) to
  `cost_ledger.jsonl`, so `git status` may show it as modified after a run.
- A run stops making real calls at $2.00 (`MAX_USD_PER_RUN`). Duplicates cost nothing.


Recording (about 5:30): [recording/demo.mp4](recording/demo.mp4)