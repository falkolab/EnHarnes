#!/usr/bin/env python3
"""Plan-size linter: enforce the decomposition contract on large ExecPlans.

The harness used to classify task *risk* by kind of change, never by *size*. This
is the plan-time half of the change-size guardrails: it makes a plan that
declares itself large prove it has been cut into independently mergeable
milestones, at the earliest possible moment — before any code is written.

Every ExecPlan should carry a `## Change-size` section holding a single line:

    Change-size class: small | medium | large

Detection is anchored to that section and to line starts, so the tokens this
linter looks for (`Change-size class:`, the milestone ship token, `SIZE-OVERRIDE:`)
can appear freely in a plan's narrative prose or indented example transcripts
elsewhere without tripping it — a plan that *documents* this feature must be able
to mention them.

Rules (high precision — a false positive blocks CI on a legitimate plan):

  - No `## Change-size` section, or no `Change-size class:` line in it -> WARNING
    (non-blocking nudge; existing plans predate the convention).
  - class == large: the section must list at least `minMilestonesWhenLarge`
    milestone bullet lines, each ending with the ship token (default `→ PR`).
    If it does not -> ERROR (exit 1), UNLESS the section also carries a
    `SIZE-OVERRIDE: <reason>` line, which downgrades it to a WARNING echoing the
    reason.
  - class in {small, medium}: no milestone list required.

Only ERRORs fail the build. Runs via `make lint-todos` / `make lint` and stand-alone.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
ACTIVE_DIR = ROOT / "docs" / "exec-plans" / "active"
POLICY_PATH = ROOT / "policies" / "size-policy.json"

DEFAULTS = {
    "sectionHeading": "## Change-size",
    "classMarker": "Change-size class:",
    "milestoneShipToken": "→ PR",
    "minMilestonesWhenLarge": 3,
    "overrideToken": "SIZE-OVERRIDE:",
}

CLASS_RE = re.compile(r"^Change-size class:\s*(small|medium|large)\s*$", re.IGNORECASE)
OVERRIDE_RE = re.compile(r"^SIZE-OVERRIDE:\s*(.+)$")
BULLET_RE = re.compile(r"^\s*[-*]\s+")
HEADING_RE = re.compile(r"^##\s+")


def _load_plan_policy() -> dict:
    policy = dict(DEFAULTS)
    if POLICY_PATH.exists():
        try:
            data = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return policy
        policy.update({k: v for k, v in data.get("plan", {}).items() if k in DEFAULTS})
        if "overrideToken" in data:
            policy["overrideToken"] = data["overrideToken"]
    return policy


def _section_lines(text: str, heading: str) -> list[str] | None:
    """Return the body lines of the section whose heading matches `heading`
    exactly (e.g. '## Change-size'), or None if there is no such section."""
    lines = text.splitlines()
    body: list[str] = []
    in_section = False
    for line in lines:
        # Anchor to column 0 (rstrip, not strip): a genuine ATX heading is never
        # indented, so an indented `## Change-size` inside an example transcript
        # cannot hijack the section boundary.
        if not in_section:
            if line.rstrip() == heading:
                in_section = True
            continue
        if HEADING_RE.match(line):
            break
        body.append(line)
    return body if in_section else None


def _class_of(section: list[str]) -> str | None:
    # rstrip, not strip: the marker must sit at column 0, so an indented mention
    # inside a code/example block within the section does not count.
    for line in section:
        m = CLASS_RE.match(line.rstrip())
        if m:
            return m.group(1).lower()
    return None


def _override_reason(section: list[str]) -> str | None:
    for line in section:
        m = OVERRIDE_RE.match(line.rstrip())
        if m:
            return m.group(1).strip()
    return None


def _count_milestones(section: list[str], ship_token: str) -> int:
    return sum(
        1 for line in section
        if BULLET_RE.match(line) and line.rstrip().endswith(ship_token)
    )


def main() -> int:
    if not ACTIVE_DIR.is_dir():
        print("[plan-size] OK: no active/ directory.")
        return 0

    policy = _load_plan_policy()
    heading = policy["sectionHeading"]
    ship_token = policy["milestoneShipToken"]
    min_milestones = int(policy["minMilestonesWhenLarge"])

    errors: list[str] = []
    warnings: list[str] = []

    for plan in sorted(ACTIVE_DIR.glob("*.md")):
        rel = f"docs/exec-plans/active/{plan.name}"
        text = plan.read_text(encoding="utf-8")
        section = _section_lines(text, heading)

        if section is None:
            warnings.append(
                f"{rel}: no '{heading}' section. Add one with a "
                f"'{policy['classMarker']} small|medium|large' line."
            )
            continue

        cls = _class_of(section)
        if cls is None:
            warnings.append(
                f"{rel}: '{heading}' section has no "
                f"'{policy['classMarker']} small|medium|large' line."
            )
            continue

        if cls != "large":
            continue

        milestones = _count_milestones(section, ship_token)
        if milestones >= min_milestones:
            continue

        reason = _override_reason(section)
        detail = (
            f"class is 'large' but the '{heading}' section lists {milestones} "
            f"milestone(s) ending in '{ship_token}' (need >= {min_milestones})"
        )
        if reason:
            warnings.append(f"{rel}: {detail}. Overridden: {reason}")
        else:
            errors.append(
                f"  [ERROR] {rel}: {detail}. Fix: decompose into milestones each "
                f"ending '{ship_token}', or add 'SIZE-OVERRIDE: <reason>' to the section."
            )

    for w in warnings:
        print(f"  [warn] {w}")

    if errors:
        print("Plan-size decomposition errors:")
        for e in errors:
            print(e)
        return 1

    print("[plan-size] OK: all active plans satisfy the decomposition contract.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
