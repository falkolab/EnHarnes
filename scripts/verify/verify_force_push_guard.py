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
    ("git -C . push -f origin master", "deny"),   # global opt between git/push
    ("git -c k=v push --force origin main", "deny"),
    (f"{P} --force \\\n  origin main", "deny"),    # backslash-newline continuation
    (f"{P} \\\n  --force-with-lease origin master", "deny"),  # flag on the next line
    # --- allowed: force-push to a NON-protected branch (guard is scoped) ---
    (f"{P} --force origin feature-x", "allow"),
    (f"{P} -f origin task/foo", "allow"),
    (f"{P} -fq origin release/1.2", "allow"),
    # --- allowed: normal pushes (hook may "ask"; that is not a denial) ---
    (f"{P} origin main", "allow"),
    (f"{P} -u origin main", "allow"),          # -u has no 'f' -> not a force
    (f"{P} --follow-tags origin main", "allow"),  # long opt, not a force flag
    # --- the protected name must be the push DESTINATION, not a substring ---
    # These three were DENIED until 2026-09-12: the branch pattern treated '-' and
    # '/' as word boundaries, so `main` inside a longer branch name matched. Denying
    # a force push to a feature branch protects nothing and teaches people to route
    # around the guard, which is how guards die.
    (f"{P} -f origin feature/main-fix", "allow"),
    (f"{P} -f origin feature/domain-main", "allow"),
    (f"{P} -f origin release/master-cutover", "allow"),
    (f"{P} -f origin mainline", "allow"),
    (f"{P} -f origin domain", "allow"),
    (f"{P} -f origin main-backup", "allow"),
    # ...while every real spelling of the destination still denies:
    (f"{P} origin +refs/heads/main", "deny"),
    (f"{P} origin HEAD:main --force", "deny"),

    # --- a command that can only READ executes none of the guarded acts ---
    # `git log --grep=<sql>` was denied for containing a keyword. Searching history
    # destroys nothing; the rule means "this command runs the statement".
    ("git log --grep='" + "DROP" + " TABLE'", "allow"),
    ("grep -rn '" + "DROP" + " TABLE' docs/", "allow"),
    ("git show HEAD:notes.md", "allow"),
    # ...but a read-only segment never excuses the segment beside it:
    ("grep -rn foo docs/ && psql -c '" + "DROP" + " TABLE users'", "deny"),
    ("git log --oneline; " + "rm" + " -rf " + "/", "deny"),
    ("grep x || psql -c '" + "DROP" + " TABLE t'", "deny"),
    # rg can run a preprocessor, so it is not read-only when asked to:
    ("rg --pre=sh pattern && psql -c '" + "DROP" + " TABLE t'", "deny"),

    # --- a substitution runs its own command first; the outer word is irrelevant ---
    # These four were ALLOWED outright — no prompt at all — by the first shape of the
    # read-only exemption, which judged a segment by its opening words. Found by an
    # independent reviewer, not by the author's own probe, which had no case for it.
    ("git log --grep=x -- \"$(psql -c '" + "DROP" + " TABLE users')\"", "deny"),
    ("git log --oneline -- \"$(" + "mkfs" + ".ext4 /dev/sda1)\"", "deny"),
    ("grep x `psql -c '" + "DROP" + " TABLE t'`", "deny"),
    ("grep x <(psql -c '" + "DROP" + " TABLE t')", "deny"),
    # A search tool can still write a file, so it is not read-only when asked to:
    ("git log -1 --output=/etc/passwd && psql -c '" + "DROP" + " TABLE t'", "deny"),

    # --- an ordinary quoted destination is still the destination ---
    (f'{P} --force origin "main"', "deny"),
    (f"{P} --force origin 'master'", "deny"),

    # --- ...but an unrelated later line is not part of the push ---
    (f'{P} origin feature-branch\necho "remove -f flag from main config"', "allow"),

    # --- a script run out of a temp directory is unauditable, so it is refused ---
    # The guard reads the text of a Bash call; a file's contents are not in it.
    # This rule is finite where an API blocklist is not: it constrains where the
    # code came from, not what it does, and the only way around it — commit the
    # script — is the outcome we want.
    ("python3 /private/tmp/claude-501/scratchpad/probe.py", "deny"),
    ("python3 /tmp/fix.py", "deny"),
    ("bash /tmp/install.sh", "deny"),
    ("sh /var/folders/zd/T/x.sh", "deny"),
    ("time python3 /tmp/probe.py", "deny"),
    ("/usr/bin/python3 /private/var/folders/zz/T/p.py", "deny"),
    ("node /tmp/build.js", "deny"),
    # A quote is a token boundary like any other. The first shape of this rule
    # required the temp path to start the token, so the plainest respelling of the
    # blocked command — putting the path in quotes — walked straight past it, and
    # so did a nested `sh -c '<interpreter> <temp path>'`. An independent reviewer
    # found both; neither is an exotic evasion, and a rule defeated by a pair of
    # quotation marks is not a rule.
    ('python3 "/tmp/x.py"', "deny"),
    ("python3 '/tmp/fix.py'", "deny"),
    ("sh -c 'python3 /tmp/x.py'", "deny"),
    ('bash -c "node /var/folders/zd/T/x.js"', "deny"),
    # The interpreter set is small and nameable — unlike the set of dangerous APIs
    # this rule replaced. Leaving half of it out only moves the work one word over.
    ("php /tmp/x.php", "deny"),
    ("pwsh /tmp/x.ps1", "deny"),
    ("Rscript /tmp/a.R", "deny"),
    ("deno run /tmp/x.ts", "deny"),
    # ...while reading such a file, or running one from the repository, is untouched:
    ("cat /tmp/probe.py", "allow"),
    ("grep -rn foo /tmp/out.txt", "allow"),
    ("python3 -c \"print('/tmp/x.py')\"", "allow"),
    # ...and a script committed to the repository runs freely — that is the point:
    ("python3 scripts/verify/verify_plan_size.py", "allow"),
    ("php artisan/does/not/exist.php", "allow"),

    # --- the fork-bomb rule matched nothing for as long as it has existed ---
    # Its pattern spelled `()` and `{}` unescaped, which in Python `re` are an
    # empty group and a quantifier, so the one string it names could never match.
    # Found by the review of this change; the rule reads as protection and was
    # decoration. Composed from fragments here for the same reason the delete and
    # SQL cases are: the literal must not appear in a command line.
    (":" + "(){ :|:& };:", "deny"),
    (":" + "() { :|:& }; :", "deny"),          # the whitespace-tolerant spelling
    ("echo 'smiley :) and a brace }'", "allow"),

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
