---
name: debugging-protocol
description: Root-cause-first debugging discipline with a hard stop after three failed fixes. Use when investigating a bug, a failing test, a red pipeline, or any "it doesn't work" report — before writing a fix. Not a knowledge base of framework tricks; a protocol that decides when you are allowed to change code and when you must escalate instead.
---

# Debugging protocol

This skill encodes *when you may change code*, not *how the framework works*.
Everything below is enforcement of one rule:

> **No fix without a root cause you can state in one sentence.**

A patch that makes the symptom disappear without that sentence is not a fix. It
is a second bug wearing the first one's clothes, and it will be rediscovered
later with less context.

## The protocol

### 1. Reproduce before you read

Write the failing check first — a test, a command, a request — that fails now and
will pass when the bug is gone. Put it in `tests/` or `scripts/verify/`, not in
your head.

If you cannot reproduce it deterministically, that is the bug you are working on.
Say so and stop guessing; an intermittent failure investigated by trial fix is
just noise.

### 2. Locate before you theorize

Read the whole error, including the parts that look boring. Then narrow with
evidence — `git log`/`git diff` on the suspect path, logging at the boundary the
bad value crosses, bisection when history is available.

The question is always "where does the bad value first exist?", never "what line
could I change to make this go away?".

### 3. State the cause in one sentence

Before editing anything, write the sentence: *"X fails because Y, which happens
when Z."* If you cannot fill in all three, you are still in step 2.

This sentence goes into the ExecPlan's Decision log, the commit message, or the
Activity Log entry — wherever the change is being recorded.

### 4. Fix at the cause, verify at the symptom

Change the thing named in the sentence. Then run the check from step 1, the
surrounding suite, and `make lint`.

A fix that needs the check from step 1 to be edited is not a fix — either the
check was wrong (say so explicitly) or the cause was misidentified.

## The three-strikes rule

**Three consecutive failed fix attempts on the same bug is a hard stop.**

Not "try once more with a different guess". Stop, and treat the count itself as
the finding: three misses mean the model of the system is wrong, not the code.
At that point:

1. Revert the failed attempts. Do not stack them — a pile of speculative patches
   makes the next diagnosis harder than the first one was.
2. Reclassify the work as a design problem, not a bug fix.
3. Escalate: for medium/high risk, that means an ExecPlan; otherwise a note to
   the owner with what you tried and what each attempt disproved.
4. Add a Failure Ledger entry in `AGENTS.md` if the harness let the bug through.

Counting is the point. Without a counter, "one more attempt" repeats until the
context is exhausted.

## Stop signals

Any of these means step back rather than continue:

- "Quick fix now, investigate properly later."
- "This should work" — said without being able to explain why it currently doesn't.
- The fix is a `try/except`, a retry, a sleep, or a widened type, and you cannot
  name the specific failure it handles.
- The reproduction only fails sometimes and you are fixing it anyway.
- You are editing a test to match the code's current behavior mid-investigation.

## What belongs in the record

Every debugging session that produced a change leaves behind: the reproduction
check (committed), the one-sentence cause, and — when the harness failed to catch
it — a Failure Ledger entry with the enforcement that would have. Prefer making
the check mechanical over writing a warning that a future reader must remember.

See also: `AGENTS.md` (Failure Ledger, Verification-First Engineering),
`.claude/skills/harness.plan/SKILL.md` (when a bug becomes an ExecPlan).
