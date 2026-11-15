# AI_USAGE

## Setup

I used Claude Code in VS Code as the coding agent, working inside `candidate-package/`. It read `CLAUDE.md` at the start of every session. The rules in it: nothing keyed to dev strings, senders or filenames, run the eval before and after every change, ask before adding a dependency, never crash on any email.

Before any code I read all 24 dev emails myself and wrote down what could go wrong in each one. The agent took its rules from my notes, not from the dev answers. I also used a separate chat to plan one step at a time and review each result. Its log isn't included, as the brief says. I ran every command that makes real model calls myself, and the API key was only ever set in my terminal, never typed into a chat.

## How I worked

One small step at a time. A goal, one prompt to the agent, then I read what came back before committing. The eval (`eval/score.py`) mirrors the grading, so every change had a before and after number. I looked for dev-string fitting, swallowed exceptions, silent losses and new dependencies before accepting a step. Every step is its own commit and nothing is squashed.

## Three moments

**1. The agent was wrong and I caught it: the scorer (`b1e9707`).** The scorer matched substrings, so "150" matched inside "1500", and its own "perfect key" test was circular. It compared the key to itself, so it could never fail. I caught it in review before I trusted any number from it. The fix was whole-token matching plus 20 mutation tests (break the output a known way, check the score drops). Session: `ai-sessions/b7bd7a43-fee7-4fdd-aa62-25b37b249b68.jsonl` (the scorer step, early in the session).

**2. I didn't accept the agent's result: the scan verification pass (built on top of `839726f`, never committed).** The agent built a second-read pass for scanned tables, with row numbers as anchors and a 2-of-3 vote on quantities. Its report said the tests passed. I ran it for real ($0.08) and it didn't fix E009's quantities and added a second invented row. I reverted it instead of keeping a feature the numbers didn't support. Session: `ai-sessions/b7bd7a43-fee7-4fdd-aa62-25b37b249b68.jsonl` (the last part of the session, after the clean cache run).

**3. The agent saved real time: the completeness check (`6d7a6f9`).** E006 came back with 33 of 42 rows and no error. I told the agent what I'd found and it built the row counter and the targeted second pass, with tests, in one step. Doing that by hand would have taken me much longer. Session: `ai-sessions/b7bd7a43-fee7-4fdd-aa62-25b37b249b68.jsonl` (the completeness step, about the middle of the session).

## Logs

`ai-sessions/` has the raw log of the coding agent session and the `CLAUDE.md` it followed. The only edits: strings shaped like API keys (they were fake keys from the tests, not real ones) are replaced with a REDACTED marker, and my work email address is replaced with `[work-email-redacted]`.
