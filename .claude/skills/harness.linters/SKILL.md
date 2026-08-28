---
name: harness-linters
description: Static analysis, quality checks, pre-PR gates, type checking — architecture boundaries, code conventions, doc health, entropy detection. Reads config from policies/.
---

# Harness Linters

Static analysis scripts that enforce code quality, architecture boundaries, documentation health, and pre-PR quality gates.

## Scripts

### architecture-health/
- `test_layer_dependencies.py` — Layer DAG enforcement, cycle detection, cross-cutting imports (Golden Principles 1, 3, 12). Reads `policies/architecture.yaml`.

### code-health/
- `code_conventions.py` — No bare print(), file size limits, kebab-case naming, layer directory validation (Golden Principles 4, 5). Reads `policies/architecture.yaml`.
- `validate_lint_rules.py` — Validates ast-grep rule YAML files in `policies/ast-grep/`.

### doc-health/
- `todo_linter.py` — TODO owner format, template marker checks. Universal, no config needed.
- `misfiled_plans.py` — An ExecPlan whose every Progress box is checked must not still sit in `docs/exec-plans/active/`. Enforces atomic completion.
- `plan_size.py` — A plan declaring `Change-size class: large` must list its milestone PRs in its `## Change-size` section (plan-time half of the change-size guardrails; thresholds in `policies/size-policy.json`).
- `doc_health_check.py` — Stale docs, broken links, orphan files, index coverage. Uses autodiscovery.
- `check_doc_drift.py` — Detects doc/code drift per `policies/risk-policy.json` watch paths.

### entropy/
- `entropy_check.py` — Orphan scripts, blank setpoints detection.

### Top-level
- `lint_runner.py` — Orchestrator: runs todo_linter + code_conventions in sequence. Auto-detects project type.
- `typecheck.py` — Universal type checker with auto-detection (Rust/Node/Python).
- `pre_pr_gate.py` — 5-check pre-PR self-review gate (lint + doc-drift + watch-paths + entropy + change-size).
- `change_size.py` — PR-time half of the change-size guardrails: measures a branch's net diff and staleness vs `policies/size-policy.json`; shared verbatim by `pre_pr_gate.py` and the CI job so both measure identically. Override a hard block with a `SIZE-OVERRIDE: <reason>` commit-message line.

### scripts/verify/ (repo root, not this skill)

Behavioural checks for the harness's own enforcement.

Run by `make lint-hooks` (part of `make lint`):

- `verify_force_push_guard.py` — the force-push-to-main deny/allow table, asserted
  against the hook's `decide()`.
- `verify_hook_contract.py` — runs every hook as a subprocess against hardcoded
  real payloads and asserts each is registered under the right event with a
  matcher covering its own tool set. It guards OUR side only: the shapes are a
  hand-transcribed snapshot of the Claude Code binary's hook builders, so it
  cannot notice an upgrade changing the contract — re-derive them from the binary
  after an upgrade and record the version checked. Exists because a
  decision-function unit test stayed green for months while the guard it covered
  enforced nothing: test the layer that can break, not the one convenient to import.

Run by **`make verify-worktree`**, not by `lint-hooks` — it does real
`git worktree add` work, and running that from the pre-commit hook is what leaked
`GIT_DIR` into its fixtures:

- `verify_worktree_boot.py` — `worktree_boot.py`'s rooting, `.env` bridging,
  per-worktree venv and ignore-refusal, including an end-to-end run of its CLI.
  Wired to a path-scoped CI job on `scripts/harness/**` and `scripts/verify/**`.

After changing a hook, re-run `make lint-hooks`; after changing `worktree_boot.py`,
`make verify-worktree`. When adding a check, confirm it fails if you revert the
code it guards.

## Dependencies

- Requires **harness-core** (for policies/ and docs references)
- All scripts read from `policies/` — no hardcoded project knowledge

## Makefile Targets

```makefile
lint-todos:      todo_linter.py
lint-src:        code_conventions.py
lint-structural: pytest test_layer_dependencies.py
lint-yaml:       validate_lint_rules.py
lint-ast:        ast-grep scan
check-entropy:   entropy_check.py
check-docs:      doc_health_check.py
review:          pre_pr_gate.py
```
