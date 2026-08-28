---
name: harness-review-panel
description: Use for High-risk changes post-implementation, before a PR/merge — an internal multi-lens review panel that spawns several read-only reviewer subagents in parallel, each with a distinct emphasis (architectural cleanliness / security / completeness+documentation), then arbitrates their verdicts.
---

# Internal Review Panel

An independent review panel of several `reviewer` subagents, each with its **own lens**, run in parallel with fresh context. The operator (the main agent) arbitrates their verdicts.

This is an in-harness procedure built entirely on `reviewer` subagents (read-only, no shared assumptions with the implementer). It realizes **Task Loop step 11 — "Agent review" (medium/high risk)**: for a High-risk change, several reviewers each take one angle, so coverage is broad and the verdicts are independent.

## When to Use

- **Post-implementation** — before a PR/merge for medium/high-risk changes (especially money / access / security paths).
- **ExecPlan review** — before implementing a High-risk plan (the lenses shift to scope / risk-tier / architecture / verification, instead of security-in-code).

For trivial / low-risk changes the panel is unnecessary — `make review` (Task Loop step 10) is enough.

**See also / when to use which.** `make review` is the fast, single-context gate you run on *every* change. This panel sits **above** the `reviewer` subagent: multi-lens, parallel, independently-arbitrated, and reserved for **High-risk** work. Use it **in addition to** `make review`, not instead of it.

## Lenses

Pick lenses to fit the task; a typical set for code:

1. **Architectural cleanliness** — layer boundaries (`policies/architecture.yaml`, `ARCHITECTURE.md`), logic living in the right layer, interface design, file size (soft limit), idiomatic style, dead code / duplication, clean wiring.
2. **Security** — for money / access paths: is the invariant actually enforced? bypasses? fail-closed on error? idempotency / replay? parameterized SQL? regressions in the touched paths? secret / bearer-token exposure?
3. **Completeness + documentation coverage** — are all plan steps implemented? all read/write paths updated? do the tests cover the stated cases (fail-before / pass-after)? are the docs reconciled with reality (no stale claims — grep for old terms)? are the plan's living sections honest? trackers in sync?

Scale the number of lenses to the change: 2 for a medium change, 3–4 for High-risk. **One lens per subagent** — do not mix them, or reviewers duplicate findings and coverage drops.

## Invocation

Launch the `reviewer` subagents (read-only, fresh context) **in a single message** so they run in parallel. Give each:

- the worktree path and the diff scope (`git diff main` from the worktree);
- its **single** lens, with an instruction not to stray into the others;
- the requirement to read the **real files**, not just the diff.

Each reply returns a verdict — `APPROVED` / `CHANGES_REQUESTED` / `REJECTED` — plus findings with a severity (High / Med / Low) and a `file:line`; for the security lens, a concrete failure scenario.

Pseudocode (via the Agent tool, `subagent_type: reviewer`, one message = parallel):

    Agent(reviewer, "lens: ARCHITECTURE. worktree=<...>. Review `git diff main`. Focus ONLY on architecture; do NOT duplicate security/completeness. Read the real files. Return a verdict + findings with file:line.")
    Agent(reviewer, "lens: SECURITY. worktree=<...>. ...")
    Agent(reviewer, "lens: COMPLETENESS+DOCS. worktree=<...>. ...")

## Arbitration (mandatory — the panel does not self-apply)

The operator arbitrates; it does not rubber-stamp:

1. Read every verdict. **Verify the heaviest / High findings yourself** against the code — a reviewer can be wrong.
2. Valid → fix in code/docs immediately.
3. Overengineering / taste → reject, with an explicit reason.
4. Out of scope → file a ticket in `docs/exec-plans/tech-debt-tracker.md`; do not drag it into the PR.
5. Product / architecture decision → escalate to the user; do not decide silently.
6. After fixes → re-run the gates (`make lint` / `make review` + the project's tests, e.g. `pytest`); for substantial edits, run another round if warranted.

## Pitfalls

1. **Sequential launch** instead of one message — parallelism is lost.
2. **Mixed lenses** — reviewers duplicate findings and coverage drops.
3. **Taking a verdict on faith** — verify High findings against the code yourself.
4. **Silently absorbing out-of-scope** — file a ticket, don't widen the PR.
5. **Skipping arbitration** — verdicts without actions are useless.
