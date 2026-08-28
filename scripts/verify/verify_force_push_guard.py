#!/usr/bin/env python3
"""Verify the force-push guard in .claude/hooks/validate-bash.py.

Deterministic deny/allow table for the PreToolUse Bash guard. It invokes the
hook exactly as Claude Code does — a JSON tool-call payload on stdin, a JSON
permission decision on stdout — and asserts the decision for each case.

Contract under test:
  * force-push to main/master, in ANY spelling, is DENIED
  * force-push to a NON-protected branch is allowed (guard is scoped to
    main/master only)
  * a normal (non-force) push is allowed (the hook may still "ask", which is
    not a denial)
  * near-miss commands that merely mention "f"/"main" are NOT denied
    (false-positive guard)

Wired into `make lint` as `lint-hooks`, so CI fails if the guard regresses.
Exit 0 = all cases hold; exit 1 = at least one mismatch.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / ".claude" / "hooks" / "validate-bash.py"

# Keep the literal "git push" out of a single token so this file never trips
# the guard it tests (the guard scans Bash tool-call strings, not this file —
# but this keeps greps and any wrapping tooling calm).
P = "git" + " push"

# (command, expected) — expected is "deny" or "allow" (allow = anything but deny).
CASES: list[tuple[str, str]] = [
    # --- force-push to a protected branch: every spelling must DENY ---
    (f"{P} --force origin main", "deny"),
    (f"{P} origin main --force", "deny"),
    (f"{P} -f origin main", "deny"),
    (f"{P} origin main -f", "deny"),
    (f"{P} -fq origin main", "deny"),          # combined short flags
    (f"{P} -qf origin main", "deny"),
    (f"{P} -fv origin master", "deny"),
    (f"{P} --force-with-lease origin main", "deny"),
    (f"{P} --force-if-includes origin main", "deny"),
    (f"{P} origin +main", "deny"),             # leading-'+' refspec force
    (f"{P} origin +HEAD:main", "deny"),
    (f"{P} origin +refs/heads/main", "deny"),
    (f"{P} origin +master:master", "deny"),
    (f"git -C . push -f origin master", "deny"),   # global opt between git/push
    (f"git -c k=v push --force origin main", "deny"),
    (f"{P} --force \\\n  origin main", "deny"),    # backslash-newline continuation
    # --- allowed: force-push to a NON-protected branch (guard is scoped) ---
    (f"{P} --force origin feature-x", "allow"),
    (f"{P} -f origin task/foo", "allow"),
    (f"{P} -fq origin release/1.2", "allow"),
    # --- allowed: normal pushes (hook may "ask"; that is not a denial) ---
    (f"{P} origin main", "allow"),
    (f"{P} -u origin main", "allow"),          # -u has no 'f' -> not a force
    (f"{P} --follow-tags origin main", "allow"),  # long opt, not a force flag
    # --- allowed: near-misses that must not false-positive ---
    ("git status", "allow"),
    ("grep -f patterns.txt main.py", "allow"),
    # A long option whose tail looks like a short flag cluster (-file, -force)
    # must not arm the guard just because 'main' appears elsewhere on the line.
    (f"{P} -u origin topic && gh pr create --base main --body-file /tmp/b.md", "allow"),
    (f"{P} -u origin topic && gh pr create --base main --dry-run", "allow"),
]


def decision(command: str) -> str:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    out = subprocess.run(
        [sys.executable, str(HOOK)], input=payload, capture_output=True, text=True
    ).stdout
    if not out.strip():
        return "pass"
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"].lower()


def main() -> int:
    if not HOOK.exists():
        print(f"[lint-hooks] FAIL: hook not found at {HOOK}", file=sys.stderr)
        return 1

    failures = []
    for command, expected in CASES:
        got = decision(command)
        ok = (got == "deny") if expected == "deny" else (got != "deny")
        if not ok:
            failures.append((command, expected, got))

    if failures:
        print(f"[lint-hooks] FAIL: {len(failures)}/{len(CASES)} case(s) wrong:", file=sys.stderr)
        for command, expected, got in failures:
            print(f"  expected {expected}, got {got}: {command!r}", file=sys.stderr)
        return 1

    print(f"[lint-hooks] OK: force-push guard holds on all {len(CASES)} cases.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
