#!/usr/bin/env python3
"""Verify plan_size.py — the plan-time half of the change-size guardrails.

Deterministic pass/fail table for `plan_size.main`. It writes fabricated
ExecPlans into a temp `active/` dir (never touching the real repo), points the
module at it, and asserts the verdict for each case:

  * no `## Change-size` section            -> WARNING, non-blocking (0)
  * class: small                           -> OK (0)
  * class: large + 3 `→ MR` milestones     -> OK (0)
  * class: large + 2 `→ MR` milestones     -> ERROR (1)
  * class: large + 2 + SIZE-OVERRIDE line  -> WARNING, non-blocking (0)

Plus the two regressions that lock the column-0 anchoring fix (an early draft
used `.strip()`, so an *indented* `## Change-size` example transcript could
hijack the section boundary — the one real governance bypass review found):

  * an under-decomposed large plan whose ONLY well-formed milestone list sits in
    an INDENTED `## Change-size` transcript must still ERROR (the indented block
    must not rescue it)                     -> ERROR (1)
  * a correctly-decomposed large plan that also CONTAINS an indented
    `## Change-size` transcript (a plan documenting this feature) must still pass
    (the indented block must not sabotage it) -> OK (0)

Wired into `make lint` as part of `lint-size`. Exit 0 = all cases hold.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_HEALTH_DIR = REPO_ROOT / ".claude" / "skills" / "harness.linters" / "scripts" / "doc-health"
sys.path.insert(0, str(DOC_HEALTH_DIR))

import plan_size  # noqa: E402  (path injected above)


def _check(plan_body: str) -> int:
    """Write `plan_body` as the sole plan in a temp active/ dir and run plan_size."""
    with tempfile.TemporaryDirectory() as d:
        active = Path(d) / "active"
        active.mkdir()
        (active / "plan.md").write_text(plan_body, encoding="utf-8")
        plan_size.ACTIVE_DIR = active
        plan_size.POLICY_PATH = Path(d) / "no-policy.json"  # missing -> DEFAULTS
        with contextlib.redirect_stdout(io.StringIO()):
            return plan_size.main()


def _check_with_policy(plan_body: str, plan_policy: dict) -> int:
    """Same, but with a real policy file — the path a fork takes to narrow the default."""
    with tempfile.TemporaryDirectory() as d:
        active = Path(d) / "active"
        active.mkdir()
        (active / "plan.md").write_text(plan_body, encoding="utf-8")
        policy = Path(d) / "size-policy.json"
        policy.write_text(json.dumps({"plan": plan_policy}), encoding="utf-8")
        plan_size.ACTIVE_DIR = active
        plan_size.POLICY_PATH = policy
        with contextlib.redirect_stdout(io.StringIO()):
            return plan_size.main()


NO_SECTION = "# Plan\n\nSome prose, no size section.\n"

SMALL = "# Plan\n\n## Change-size\n\nChange-size class: small\n"

LARGE_OK = (
    "# Plan\n\n## Change-size\n\nChange-size class: large\n\n"
    "- M1 do a thing → MR\n- M2 do another → MR\n- M3 finish → MR\n"
)

LARGE_SHORT = (
    "# Plan\n\n## Change-size\n\nChange-size class: large\n\n"
    "- M1 do a thing → MR\n- M2 do another → MR\n"
)

LARGE_SHORT_OVERRIDE = (
    "# Plan\n\n## Change-size\n\nChange-size class: large\n\n"
    "- M1 do a thing → MR\n- M2 do another → MR\n\n"
    "SIZE-OVERRIDE: two milestones is genuinely the whole change\n"
)

# Regression A: the real (col-0) section declares large with 1 milestone -> ERROR.
# An INDENTED `## Change-size` transcript earlier lists 3 — with `.strip()` this
# would hijack the section and wrongly pass. Column-0 anchoring must ignore it.
INDENTED_HIJACK = (
    "# Plan\n\n"
    "Here is an example of a decomposed section:\n\n"
    "    ## Change-size\n\n"
    "    Change-size class: large\n\n"
    "    - fake A → MR\n    - fake B → MR\n    - fake C → MR\n\n"
    "## Change-size\n\nChange-size class: large\n\n"
    "- M1 the only real milestone → MR\n"
)

# Regression B: a correctly-decomposed large plan that also documents the feature
# with an indented `## Change-size` example listing only 1 milestone. The indented
# block must not sabotage the real (col-0) 3-milestone section -> OK.
INDENTED_BENIGN = (
    "# Plan\n\n"
    "## Change-size\n\nChange-size class: large\n\n"
    "- M1 real → MR\n- M2 real → MR\n- M3 real → MR\n\n"
    "For example a smaller plan might show:\n\n"
    "    ## Change-size\n\n    Change-size class: large\n\n    - only one → MR\n"
)

# The ship token is the one place the harness spoke a single forge's dialect. A
# plan written on GitHub stopped linting on GitLab and the other way round, and
# every fork inherited a one-line divergence from the template for no reason but
# vocabulary. Both tokens are accepted, including inside one plan — a document
# that quotes the other forge's spelling in an example is not thereby broken.
LARGE_OK_PR = (
    "# Plan\n\n## Change-size\n\nChange-size class: large\n\n"
    "- M1 do a thing → PR\n- M2 do another → PR\n- M3 finish → PR\n"
)

LARGE_OK_MIXED = (
    "# Plan\n\n## Change-size\n\nChange-size class: large\n\n"
    "- M1 do a thing → MR\n- M2 do another → PR\n- M3 finish → MR\n"
)

LARGE_SHORT_PR = (
    "# Plan\n\n## Change-size\n\nChange-size class: large\n\n"
    "- M1 do a thing → PR\n- M2 do another → PR\n"
)

CASES = [
    ("no section -> warn, non-blocking", NO_SECTION, 0),
    ("large + 3 milestones, PR spelling -> OK", LARGE_OK_PR, 0),
    ("large + 3 milestones, both spellings in one plan -> OK", LARGE_OK_MIXED, 0),
    ("large + 2 milestones, PR spelling -> ERROR", LARGE_SHORT_PR, 1),
    ("class small -> OK", SMALL, 0),
    ("large + 3 milestones -> OK", LARGE_OK, 0),
    ("large + 2 milestones -> ERROR", LARGE_SHORT, 1),
    ("large + 2 + SIZE-OVERRIDE -> warn", LARGE_SHORT_OVERRIDE, 0),
    ("col-0 regression: indented block must NOT rescue -> ERROR", INDENTED_HIJACK, 1),
    ("col-0 regression: indented block must NOT sabotage -> OK", INDENTED_BENIGN, 0),
]


# A project that wants exactly one house spelling still gets it, and the old
# single-string config keeps working — a fork that already set one must not have
# its gate silently widened by this change.
POLICY_CASES = [
    ("policy narrows to one spelling: the other ERRORs",
     LARGE_OK_PR, {"milestoneShipToken": "→ MR"}, 1),
    ("policy narrows to one spelling: that one still passes",
     LARGE_OK, {"milestoneShipToken": "→ MR"}, 0),
    ("policy may also give a list",
     LARGE_OK_MIXED, {"milestoneShipToken": ["→ MR", "→ PR"]}, 0),
]


def main() -> int:
    failures = 0
    for label, body, plan_policy, expected in POLICY_CASES:
        try:
            rc = _check_with_policy(body, plan_policy)
            ok = rc == expected
        except Exception as exc:
            ok = False
            print(f"  [ERROR] {label}: {type(exc).__name__}: {exc}")
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
        if not ok:
            failures += 1
    for label, body, expected in CASES:
        try:
            rc = _check(body)
            ok = rc == expected
        except Exception as exc:
            ok = False
            print(f"  [ERROR] {label}: {type(exc).__name__}: {exc}")
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
        if not ok:
            failures += 1
    if failures:
        print(f"[verify-plan-size] {failures} case(s) failed.")
        return 1
    print(f"[verify-plan-size] OK: all {len(CASES) + len(POLICY_CASES)} cases hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
