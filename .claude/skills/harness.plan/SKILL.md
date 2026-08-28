---
name: harness-planner
description: Create self-contained execution plans (ExecPlans) for medium/high risk tasks. No code modifications during planning. Read OPENAI_PLANS.md for the canonical format, then author and validate a plan in docs/exec-plans/active/.
---

# Harness Planner

**No code modifications during planning.**

## How to Create an ExecPlan

1. **Read `OPENAI_PLANS.md`** (this directory) — the canonical format. Follow the skeleton verbatim. Every section listed there is mandatory.
2. Research affected source files — understand current state before proposing changes.
3. Create the plan at `docs/exec-plans/active/YYYY-MM-DD-<slug>.md`.

Autonomy tiers, quality gates, doc-drift rules, and the full task loop are defined in `AGENTS.md` — do not duplicate them in the plan.

## Size Guidelines — RULE (enforced)

Every plan MUST declare its size in a `## Change-size` section (see the skeleton
in `OPENAI_PLANS.md`) with one line: `Change-size class: small | medium | large`.
Classify by files touched:

| Complexity | Milestones | Tasks per Milestone |
|---|---|---|
| Small (1-3 files) | 1-2 | 2-4 |
| Medium (4-10 files) | 2-3 | 3-6 |
| Large (10+ files) | 3-5 | 4-8 |

A `large` plan MUST also list its milestones in that section, one bullet per
independently mergeable PR, each line ending `→ PR`, at least three of them —
otherwise `plan_size.py` (run by `make lint-todos`) blocks the build. Genuinely
unsplittable large work carries a `SIZE-OVERRIDE: <reason>` line in the section to
record the exception. This is the plan-time half of the change-size guardrails; a
measured diff/branch-age budget backstops it at PR time (`policies/size-policy.json`).

## Validate Before Presenting

- [ ] Self-contained: a novice with only the plan + repo can succeed
- [ ] Every file referenced by full repo-relative path
- [ ] Acceptance criteria are observable behaviors, not code attributes
- [ ] Risk-policy watch paths identified, affected docs listed
- [ ] `## Change-size` section present with a class marker (and milestone PRs if large)
- [ ] `Progress` has no box for merging the PR — the plan travels inside the branch,
      so it moves to `docs/exec-plans/done/` in the SAME PR that delivers the work.
      A merge box can never be ticked pre-merge and would force a second, closing PR.
- [ ] `make lint-todos` passes
