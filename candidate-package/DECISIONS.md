# DECISIONS

Time spent: about 10 hours. Roughly 7 to get the dev set working, the rest on reliability, regenerating a clean cache, one experiment I threw away, and these docs.

## 1. Something in the data that surprised me

The model can stop in the middle of a table and say nothing. E006 is a 42-row table inside a PDF. The first call came back with 33 rows and `finish_reason=stop`. No error, no truncation flag, no refusal. If I had trusted it, 9 requested items would have vanished and nobody would know, which is the failure that hurts most in grading.

So now the renderers print table rows as `R12: a | b` and code counts them against the lines the model returned. If some are missing it makes one targeted second pass, and if it is still short the email gets `needs_review`. E006 now returns all 44 lines.

A second surprise came late. I deleted the cache and regenerated it with the same prompt and settings, and the dense scan E009 was read differently. Quantity went from 203/209 correct to 198/209 and one invented row showed up. Temperature 0 does not make a vision read repeatable. That is why I commit the cache. The cache is the reproducible answer, a re-run is not.

## 2. Something in the brief I disagree with

The contract treats a null uom as EA. In E001, "12 jts - 2" SCH 40 ... pipe" comes out as qty 12, uom EA. A joint of pipe is not "each", it is usually 20 or 21 ft, so the convention hides a real question and the scorer can't see it. I followed the contract so my output stays comparable, and I kept `raw` verbatim so the original unit is still in the data. In my own system I would keep the unit as written and let a catalog match decide.

Related, my own eval doesn't check sizes. In E009 the blind-flange sizes shifted by one step and nothing scored it.

## 3. Predicted held-out numbers, and where I expect to be wrong

Dev: 24/24 classification, 209/210 line recall, 0 silent losses, 1 hallucinated line (E009), 94.7% quantity. I tuned the prompt and the guards on these 24 emails, so that overstates what the held-out set will give. My guess for the 40:

- classification 90 to 95%
- line recall about 93%, precision about 95%
- silent losses 0 to 2
- hallucinated lines 1 to 3
- quantity about 88%

Two kinds of email I expect to get wrong:

- **Degraded scans and photos.** E009 is the sample: 20 of 31 quantities right, one invented row, one real row missed, and it is not flagged, so it fails silently. I built a second-read verification pass and ran it for real ($0.08). It didn't help. Quantities stayed at 20/31 and it added a second invented row. Reading the same image again gives the same kind of mistakes, so I reverted it. Next I would try cutting the table into bands so the model sees something different. The cheap option is flagging every email whose lines come only from an image. That makes the failure loud, but it would also flag clean photos.
- **Revisions with no usable parent.** `revises` comes from `In-Reply-To` headers only. A revision with no header, or whose parent isn't in the inbox, gets `revises: null` with no flag. The model's own `revises_hint` is stored but I don't use it.

I also expect plain-text lists to be weaker than tables, because the completeness check only counts table rows.

## 4. How this runs for real

Built and tested:
- Every email gets an entry, and one bad email can't stop the run. 22 chaos tests cover zero-byte and random-byte files, a locked PDF, a 300-page PDF, provider down, no key, and the budget tripping.
- Replays and re-deliveries are free. Answers are cached by a hash of the model, prompt, settings and payload, so a second run makes 0 calls and the output is byte-identical.
- Duplicates (same sender, same attachments) are caught before any model call.
- A 300-page PDF reads the first 15 pages and the email is flagged "only the first 15 read".
- Cost per email comes from a ledger (`python -m rfqparse cost`). About $0.005 per email on dev, and the five image emails are about 59% of the spend. A run stops at $2.00.

Design only, not built:
- **3,000 emails a day** is about two a minute. One sequential worker at about 5 s per email covers it, and the code is sequential. A real setup would have a webhook that acknowledges fast, puts the email on a queue, and has a few workers read from it. That is a design, not code. At my per-email cost the model bill would be about $15 a day.
- **A 2-hour provider outage.** Each call retries 3 times with backoff, then the email becomes a flagged entry. Failed emails aren't cached, so running the same command later only calls the ones that are missing. I didn't build a retry-later queue or an alert.
- **300-page PDFs.** The page cap with a flag is built. Reading in chunks is not.
- **Duplicate delivery of a real new request** from the same sender with identical text would be marked as a duplicate. I accepted that. The later copy points at the earlier one with `duplicate_of`, so nothing is lost.
