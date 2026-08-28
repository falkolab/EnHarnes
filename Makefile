.PHONY: lint-todos lint-src lint-structural lint-yaml lint-ast lint-hooks verify-worktree lint ci check-docs check-entropy review gen-handbook sync-todos sync-skills sync-indexes worktree obs-up obs-down install-hooks

# Python interpreter. Auto-detects python3 then python; override: make lint PYTHON=/path/to/python
PYTHON ?= $(shell command -v python3 2>/dev/null || command -v python 2>/dev/null || echo python3)
S = .claude/skills

# === Linters (CI-blocking) ===

# TODO ownership & placeholder checks (~5s) + misfiled-plan + plan-size checks
lint-todos:
	$(PYTHON) $(S)/harness.linters/scripts/doc-health/todo_linter.py
	$(PYTHON) $(S)/harness.linters/scripts/doc-health/misfiled_plans.py
	$(PYTHON) $(S)/harness.linters/scripts/doc-health/plan_size.py

# Code conventions: bare print, kebab-case, file size + hook-registration check
lint-src:
	$(PYTHON) $(S)/harness.linters/scripts/code-health/code_conventions.py
	$(PYTHON) $(S)/harness.linters/scripts/code-health/hooks_registered.py

# Architecture boundary tests (pytest)
lint-structural:
	pytest $(S)/harness.linters/scripts/architecture-health/test_layer_dependencies.py

# Validate ast-grep rule YAML files
lint-yaml:
	$(PYTHON) $(S)/harness.linters/scripts/code-health/validate_lint_rules.py policies/ast-grep/

# Run ast-grep scan on src/ (rules discovered via sgconfig.yml -> policies/ast-grep/).
# `--rule` takes a single FILE, so pointing it at the directory never ran anything;
# the sgconfig.yml project config is how a directory of rules is loaded.
# Skips gracefully when ast-grep is not installed or the project has no src/ yet.
lint-ast:
	@if ! command -v ast-grep >/dev/null 2>&1; then echo "[lint-ast] SKIP: ast-grep not installed (pip install ast-grep-cli)"; \
	elif [ ! -d src ]; then echo "[lint-ast] SKIP: no src/ directory yet"; \
	else ast-grep scan -c sgconfig.yml src/; fi

# Hook enforcement, two layers: the deny/allow decision table, then the
# end-to-end wire contract — each hook run as a subprocess against a real
# Claude Code payload, because the table alone lets a dead guard look healthy
# (a hook reading the wrong payload keys parses nothing, no-ops, and its unit
# tests stay green).
lint-hooks:
	$(PYTHON) scripts/verify/verify_force_push_guard.py
	$(PYTHON) scripts/verify/verify_hook_contract.py

# worktree_boot's rooting + bridging. NOT in `lint`: it exercises real
# `git worktree add` against a throwaway repo, and the script it guards changes
# a few times a year — a per-commit trigger is blast radius without benefit,
# and running it inside the pre-commit hook leaks GIT_DIR into its fixtures.
# Run it when you touch scripts/harness/ or scripts/verify/ (a path-scoped CI
# job does the same on PRs).
verify-worktree:
	$(PYTHON) scripts/verify/verify_worktree_boot.py

# Composite: all CI-blocking linters (local `make lint` == the CI lint gate)
lint: lint-todos lint-src lint-structural lint-yaml lint-ast lint-hooks

# CI alias
ci: lint

# === Health checks (periodic) ===

# Doc health: stale headers, broken links
check-docs:
	$(PYTHON) $(S)/harness.linters/scripts/doc-health/doc_health_check.py

# Entropy: orphan scripts, blank setpoints
check-entropy:
	$(PYTHON) $(S)/harness.linters/scripts/entropy/entropy_check.py

# === Pre-PR gate ===

# Pre-PR self-review (5 gates: lint + doc-drift + watch-paths + entropy + change-size)
review:
	$(PYTHON) $(S)/harness.linters/scripts/pre_pr_gate.py

# === Generators ===

gen-handbook:
	$(PYTHON) $(S)/harness.generators/scripts/build_handbook.py

sync-todos:
	$(PYTHON) $(S)/harness.generators/scripts/sync_todo_registry.py

sync-skills:
	$(PYTHON) $(S)/harness.generators/scripts/sync_skills_to_agents.py

sync-indexes:
	$(PYTHON) $(S)/harness.generators/scripts/sync_doc_indexes.py

# === Dev tools ===

# Install hooks: git pre-commit (runs make lint) + Claude Code hooks
# (registers validate-bash / validate-edit / post-edit-lint / prompt-validator /
#  post-response-sync / log-agent-usage into the operator-local
#  .claude/settings.json; idempotent).
install-hooks:
	cp scripts/harness/pre-commit .git/hooks/pre-commit
	chmod +x .git/hooks/pre-commit
	@echo "Pre-commit (git) hook installed."
	$(PYTHON) $(S)/harness.generators/scripts/install_claude_hooks.py

# Worktree bootstrap
worktree:
	$(PYTHON) scripts/harness/worktree_boot.py $(TASK)

# Observability
obs-up:
	$(PYTHON) $(S)/harness.generators/scripts/observability/structured_log.py up

obs-down:
	$(PYTHON) $(S)/harness.generators/scripts/observability/structured_log.py down
