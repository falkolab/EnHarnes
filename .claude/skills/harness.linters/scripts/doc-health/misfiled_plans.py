#!/usr/bin/env python3
"""Misfiled-plan linter: a completed ExecPlan must not sit in active/.

`docs/exec-plans/active/` holds in-flight plans; `done/` holds finished ones.
The dev-loop relocates a plan to `done/` on completion, but that was an inferred
convention, not a codified step — easy to drop right when attention is on
verify/review/commit (see docs/harness-feedback.md, 2026-06-17).

This turns the convention into a deterministic check. The completion signal is a
fully-checked Progress list: at least one checkbox and zero unchecked `- [ ]`
boxes. Active plans carry open boxes, so a plan claiming every milestone done
while sitting in `active/` is self-contradictory state.

A filled `## Outcomes & Retrospective` section is a second, independent signal.
It sharpens the message but is NOT a precondition. It used to be one, and that
made the whole check opt-in: `_section_body()` returned None for a plan without
the section, the loop skipped it, and a plan with every box ticked could sit in
`active/` indefinitely by never adding a section nothing required — caught by a
human rather than by CI.

Placeholders meaning "Outcomes not written yet":
  - "(To be filled in at completion.)"
  - "To be written when the task closes ..."
  - "(REPLACE ME)"
  - the template guidance line from OPENAI_PLANS.md
  - whitespace only

A plan that is genuinely still in flight must SAY so with an unchecked box for the
work that remains. Do NOT add a box for merging the PR: the plan travels inside
the branch, so the merge is what files it under `done/` — a merge checkbox can
never be ticked before the merge, which would force a second closing PR just to
tick it and move the file. The plan moves to `done/` in the SAME PR that delivers
the work.

Runs via `make lint-todos` / `make lint` and standalone.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
ACTIVE_DIR = ROOT / "docs" / "exec-plans" / "active"

SECTION_RE = re.compile(r"^##\s+Outcomes\s*&\s*Retrospective\s*$", re.IGNORECASE)
NEXT_SECTION_RE = re.compile(r"^##\s+")

# Lines that, alone, mean the section is still a placeholder (case-insensitive,
# punctuation/whitespace-insensitive substring match).
PLACEHOLDER_SIGNATURES = [
    "to be filled in at completion",
    "to be written when the task closes",
    "to be written",
    "replace me",
    "summarize outcomes, gaps, and lessons learned",
    "tbd",
]
UNCHECKED_RE = re.compile(r"^\s*[-*]\s+\[ \]")
CHECKED_RE = re.compile(r"^\s*[-*]\s+\[[xX]\]")


def _section_body(text: str) -> list[str] | None:
    """Return the lines of the Outcomes & Retrospective section, or None."""
    lines = text.splitlines()
    body: list[str] = []
    in_section = False
    for line in lines:
        if not in_section:
            if SECTION_RE.match(line):
                in_section = True
            continue
        if NEXT_SECTION_RE.match(line):
            break
        body.append(line)
    return body if in_section else None


def _is_filled(body: list[str]) -> bool:
    for raw in body:
        stripped = raw.strip()
        if not stripped:
            continue
        normalized = re.sub(r"[^a-z0-9 ]", "", stripped.lower())
        if any(sig in normalized for sig in PLACEHOLDER_SIGNATURES):
            continue
        # Any other non-blank, non-placeholder line counts as real content.
        return True
    return False


def _progress_done(text: str) -> bool:
    """True when the plan has checkbox milestones and none are unchecked."""
    has_checked = any(CHECKED_RE.match(line) for line in text.splitlines())
    has_unchecked = any(UNCHECKED_RE.match(line) for line in text.splitlines())
    return has_checked and not has_unchecked


def main() -> int:
    if not ACTIVE_DIR.is_dir():
        print("[misfiled-plans] OK: no active/ directory.")
        return 0

    offenders: list[str] = []
    for plan in sorted(ACTIVE_DIR.glob("*.md")):
        text = plan.read_text(encoding="utf-8")
        if not _progress_done(text):
            continue  # open boxes (or no boxes at all) — legitimately in flight
        body = _section_body(text)
        # A missing Outcomes section is absence of evidence, not evidence of
        # in-flight work: it must not excuse a plan whose every box is ticked.
        outcomes_filled = body is not None and _is_filled(body)
        offenders.append((plan.name, outcomes_filled))

    if offenders:
        print("Misfiled ExecPlan errors:")
        for name, outcomes_filled in offenders:
            why = (
                "'Outcomes & Retrospective' is filled in and every Progress box is "
                "checked, so the plan is complete"
                if outcomes_filled
                else "every Progress box is checked, so the plan claims to be complete"
            )
            print(
                f"  [ERROR] docs/exec-plans/active/{name}: {why} but it is still in "
                f"active/. Fix: git mv it to docs/exec-plans/done/ — or, if work "
                f"really remains, add an unchecked box for it. Never a box for "
                f"merging: the plan ships with the branch."
            )
        return 1

    print("[misfiled-plans] OK: no completed plans left in active/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
