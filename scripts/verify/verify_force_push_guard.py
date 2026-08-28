#!/usr/bin/env python3
"""Deterministic check for the force-push-to-main guard in .claude/hooks/validate-bash.py.

Loads the hook's decide() and asserts a deny/allow table. Force-push to main/master must be
DENIED (every flavour); feature-branch force-push and normal pushes must stay allowed (they
surface as "ask", not "deny"). Exits non-zero on any mismatch so it is CI-usable.

    python scripts/verify/verify_force_push_guard.py
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = REPO_ROOT / ".claude" / "hooks" / "validate-bash.py"

# (command, expected_decision) — expected in {"deny", "ask", "allow"}; "allow" == no rule fired.
CASES = [
    # --- force-push to main/master: MUST be denied (all flavours) ---
    ("git push --force origin main", "deny"),
    ("git push -f origin main", "deny"),                        # short flag (missed by old guard)
    ("git push -fq origin main", "deny"),                       # combined short flags
    ("git push origin +main", "deny"),                          # +refspec (missed by old guard)
    ("git push origin +HEAD:main", "deny"),
    ("git push origin +refs/heads/main", "deny"),
    ("git push --force-with-lease origin main", "deny"),
    ("git push --force-if-includes origin master", "deny"),
    ("git -c http.sslVerify=false push -f origin master", "deny"),   # global opt before push
    ("git push -f origin feature:refs/heads/main", "deny"),     # two-sided refspec, dst = main
    # split across lines — must span the break (re.DOTALL), all continuation flavours + bare \n
    ("git push --force \\\n  origin main", "deny"),             # backslash-newline continuation
    ("git push --force \\\r\n  origin main", "deny"),           # CRLF continuation
    ("git push --force \\ \n  origin main", "deny"),            # trailing space before continuation
    ("git push --force $(echo origin\necho main)", "deny"),     # bare newline inside $(...) — no bypass
    # --- must stay allowed (surface as "ask", never "deny") ---
    ("git push -f origin feature/foo", "ask"),                  # feature-branch force-push: allowed
    ("git push origin +feature/foo", "ask"),                    # feature-branch +refspec: allowed
    ("git push origin feature/foo", "ask"),
    ("git push origin main", "ask"),                            # normal (non-force) push to main
    ("git push --follow-tags origin main", "ask"),              # false-positive guard: not a force flag
    # branch names that merely CONTAIN main/master must NOT be treated as the ref (else legit
    # feature-branch force-push gets blocked):
    ("git push -f origin feature/main-fix", "ask"),
    ("git push --force origin feature/domain-main", "ask"),
    ("git push -f origin release/master-cutover", "ask"),
]


def load_decide():
    spec = importlib.util.spec_from_file_location("validate_bash", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.decide


def main() -> int:
    decide = load_decide()
    failures = []
    for command, expected in CASES:
        result = decide(command)
        actual = result[0] if result is not None else "allow"
        status = "OK " if actual == expected else "FAIL"
        if actual != expected:
            failures.append((command, expected, actual))
        print(f"[{status}] expect={expected:5} got={actual:5}  {command!r}")

    if failures:
        print(f"\n[verify-force-push-guard] {len(failures)} FAILURE(S):")
        for command, expected, actual in failures:
            print(f"  {command!r}: expected {expected}, got {actual}")
        return 1
    print(f"\n[verify-force-push-guard] OK: all {len(CASES)} cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
