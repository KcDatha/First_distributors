# RFQ parser take-home — working rules

Source of truth: `ASSESSMENT.md` and `data/README.md`. Re-read them when in doubt.

## Rules

- **Python 3.11. Must run on Windows** (graders use it). No Linux-only stuff (no fork-only multiprocessing, shell-outs to Unix tools, hard-coded `/` paths; use `pathlib`, open files with explicit `encoding`). **Ask before adding any dependency.**
- **OpenAI by default**, key from `OPENAI_API_KEY`. Budget is tight: **every model call goes through a cache** (replayable with no key; second run makes zero new calls).
- **No rules keyed on dev strings, senders, filenames or subjects.** The held-out inbox has the same traps with different text. Fix causes, not instances.
- **Priority order:** (1) silent losses, (2) hallucinated lines, (3) lost/phantom requests, (4) accuracy. When unsure, `needs_review` beats returning nothing.
- **Never crash** on a bad email, missing key, or API failure. Degrade and flag.
- **Run the eval after every change** and show before/after numbers. Small commits.
- **Stop after each task and wait for the user.**
