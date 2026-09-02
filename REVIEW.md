# REVIEW.md — Review Policy

The single review policy for this repository. Every review pass — the `reviewer`
subagent (Task Loop step 11), the `harness.review-panel` lenses, and any ad-hoc
diff review — applies these rules, so reviews are consistent and comparable
across sessions.

## Passes

Run four passes over the diff; tag each finding with its pass:

1. **Bugs** — logic errors, broken edge cases, subtle regressions, error paths
   that swallow or mislabel failures.
2. **Security** — injection risks, authentication/authorization gaps, fail-open
   behavior on error, secrets or PII in code/logs/diffs, and — where the project
   has tenant or permission boundaries — scoping bypasses across them.
3. **Compliance** — the change matches its ExecPlan and declared risk tier,
   layer placement conforms to `ARCHITECTURE.md` / `policies/architecture.yaml`,
   Golden Principles hold, watch-path docs are updated, tests cover the stated
   cases.
4. **Code documentation** — the changed code is documented completely and
   honestly:
   - Every public module, class, and function carries a docstring stating its
     purpose and any non-obvious contract (arguments, return, raised errors,
     units, side effects). A missing docstring on a public symbol is a WARNING;
     on a trivial private helper it is a nit.
   - Comments state constraints and invariants the code cannot express — never
     narration of what the next line does, and never a justification of the
     change addressed to the reviewer.
   - **No change-log residue in code**: no "changed/added/removed on <date>",
     "was previously X", "new in this MR", authorship notes, or commented-out
     code. History lives in git; such residue is a WARNING.
   - Docstrings and comments must match current behavior. A stale or wrong
     docstring on a changed symbol is a WARNING (if a doc contradicts code, fix
     the doc in the same commit).

One reviewer runs all four passes; a review panel assigns each pass (lens)
to its own subagent — see `.claude/skills/harness.review-panel/SKILL.md`.

## Severity

- **CRITICAL** — breaks behavior, leaks data, breaches policy or architecture
  (an undeclared architecture deviation is always CRITICAL). Must fix before
  merge.
- **WARNING** — likely bug, missing test, principle violation. Should fix.
- **INFO (nit)** — style, naming, taste. Optional.

Reserve CRITICAL for findings that break behavior, leak data, or breach policy.
Style and naming are nits, never CRITICAL.

## Cap the nits

Report at most **five** INFO/nit findings per review; summarize the rest as a
count. CRITICAL and WARNING findings are never capped.

## Do not report

- Generated files (`docs/generated/`, framework-generated artifacts).
- Anything a deterministic gate already enforces (`make lint`, `make review`,
  CI): formatting, TODO owners, doc-index drift, size budgets. If a gate
  *should* catch it but does not, report the gate gap once instead of the
  instances.
- Vendored/third-party code.

## Feedback rule

When a review flags the same class of mistake **twice**, the correction belongs
in the harness, not in another finding: add a Failure Ledger entry in
`AGENTS.md` (or a linter rule if it is mechanically checkable) in the same
review cycle. Reviews find instances; the harness prevents classes.

## Output contract

Every finding carries: pass tag, severity, `file:line`, a one-line claim, and a
concrete recommendation. The review ends with a verdict — `APPROVE` /
`REQUEST CHANGES` / `NEEDS DISCUSSION` — and no false praise: if everything is
clean, approve and stop.
