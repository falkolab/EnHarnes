#!/usr/bin/env python3
"""TODO linter: checks TODO ownership and EXAMPLE placeholders.

Checks:
  1. TODO markers — every 'TODO:' in markdown must have owner:
     [HUMAN], [AI], or [AI->HUMAN].
  2. EXAMPLE placeholders — every 'EXAMPLE' block must contain
     '(REPLACE ME)' to indicate it's a template.

Runs as part of `make lint-todos` and `make lint` (composite).
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
OWNER_RE = re.compile(r"TODO:\s*\[(HUMAN|AI|AI->HUMAN)\]")

errors: list[str] = []

for file_path in ROOT.rglob("*.md"):
    # Skip .git and NESTED task worktrees — a .claude/worktrees/<task>/ directory
    # BELOW this root is another branch's full checkout, whose files belong to that
    # task. Match the path RELATIVE to ROOT, never the absolute path: in a
    # bare-top-level layout every checkout, main included, itself lives at
    # <repo>/.claude/worktrees/<name>/, so an absolute substring test matches every
    # file and silently switches this linter off. It did — 73 owner-tagged TODOs
    # reported as zero while `make lint` stayed green, because the check that would
    # have said otherwise was the one disabled.
    rel = file_path.relative_to(ROOT) if file_path.is_relative_to(ROOT) else file_path
    rel_str = str(rel)
    if ".git/" in rel_str or ".claude/worktrees/" in rel_str:
        continue

    for idx, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
        if "TODO:" in line and not OWNER_RE.search(line):
            rel = file_path.relative_to(ROOT)
            errors.append(
                f"{rel}:{idx}: TODO without owner marker. "
                f"Fix: add [HUMAN], [AI], or [AI->HUMAN] after 'TODO:'. "
                f"Example: TODO: [AI] Implement feature X"
            )
        if "EXAMPLE" in line and "REPLACE ME" not in line:
            rel = file_path.relative_to(ROOT)
            errors.append(
                f"{rel}:{idx}: EXAMPLE without '(REPLACE ME)'. "
                f"Fix: add '(REPLACE ME)' to indicate this is a template placeholder."
            )

if errors:
    print("Documentation lint errors:")
    for err in errors:
        print(f"  [ERROR] {err}")
    sys.exit(1)
else:
    print("[todo-linter] OK: all checks passed.")
