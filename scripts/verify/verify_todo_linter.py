#!/usr/bin/env python3
"""Verify that todo_linter.py actually looks at this checkout's own files.

It once did not. A nested-worktree skip taken from the upstream template matched
`.claude/worktrees/` in the ABSOLUTE path, and in a bare-top-level layout every
checkout — `main` included — lives at `<repo>/.claude/worktrees/<name>/`. So the
skip fired on every file: 73 owner-tagged TODOs were reported as zero, and
`make lint` stayed green, because the only check that would have objected was
the one switched off. A linter reporting "all checks passed" over an empty file
set is indistinguishable from a healthy one; that is the failure this pins.

Two cases, fabricated at the exact depth the linter derives its ROOT from:
a file in the checkout's own tree must be examined, and a file inside a worktree
NESTED below it must still be skipped (that is another branch's checkout, and
its markers belong to that task).

Hermetic — a temp directory, no git, no network. Runs in `make lint-todos`.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Scrub git's exported environment before any subprocess: run inside the
# pre-commit hook these are set, and a fixture must not inherit them.
for _var in [k for k in os.environ if k.startswith("GIT_")]:
    del os.environ[_var]

ROOT = Path(__file__).resolve().parents[2]
LINTER = ROOT / ".claude/skills/harness.linters/scripts/doc-health/todo_linter.py"

# The linter derives its ROOT as parents[5] of its own file, so the fixture must
# reproduce that depth exactly or the test would prove nothing about the real one.
SCRIPT_REL = Path(".claude/skills/harness.linters/scripts/doc-health/todo_linter.py")

UNOWNED = "TODO" + ": an unowned marker"


def run_fixture(tmp: Path) -> subprocess.CompletedProcess:
    # The fixture checkout must itself sit under `.claude/worktrees/<name>/`, or
    # this proves nothing: the defect is that the skip matched the checkout's OWN
    # absolute path, and a fixture rooted anywhere else never reproduces it. (The
    # first version of this file was rooted in a plain temp dir and passed with
    # the bug reinstated — vacuous.)
    tmp = tmp / ".claude/worktrees/main"
    tmp.mkdir(parents=True)

    script = tmp / SCRIPT_REL
    script.parent.mkdir(parents=True)
    script.write_text(LINTER.read_text(encoding="utf-8"), encoding="utf-8")

    # This checkout's own file — MUST be examined.
    (tmp / "own.md").write_text(f"- {UNOWNED} in our own tree\n", encoding="utf-8")

    # Another branch's checkout, nested below us — MUST be skipped.
    nested = tmp / ".claude/worktrees/other-task"
    nested.mkdir(parents=True)
    (nested / "theirs.md").write_text(f"- {UNOWNED} belonging to another task\n", encoding="utf-8")

    return subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, cwd=str(tmp)
    )


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="verify-todo-linter-") as d:
        proc = run_fixture(Path(d))
        out = proc.stdout + proc.stderr

        if "own.md" not in out:
            failures.append(
                "the linter did not report the unowned marker in its own tree — "
                f"it is looking at nothing (rc={proc.returncode}, output={out.strip()[:200]!r})"
            )
        if proc.returncode == 0:
            failures.append(
                f"an unowned marker must fail the linter, but it exited 0 (output={out.strip()[:200]!r})"
            )
        if "theirs.md" in out:
            failures.append(
                "the linter reported a marker inside a NESTED worktree — that is "
                "another branch's checkout and must be skipped"
            )

    if failures:
        print("[verify-todo-linter] FAIL")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("[verify-todo-linter] OK: own files examined, nested worktrees skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
