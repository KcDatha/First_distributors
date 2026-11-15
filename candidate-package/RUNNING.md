cd candidate-package

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

Dev inbox (24 emails, 23 calls plus one second pass): **$0.1121 total, about $0.005 per email**. Per-email table:
[docs/COST.md](docs/COST.md). A run stops making real calls at $2.00 (`MAX_USD_PER_RUN`). Duplicates are found in code
and cost nothing.
