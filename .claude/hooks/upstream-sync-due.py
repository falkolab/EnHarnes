#!/usr/bin/env python3
"""SessionStart hook: remind when the monthly upstream-harness sync is due.

Injects a short reminder into session context when the newest note in
docs/upstream-syncs/ is older than the cadence (default 30 days), or when no
pass has ever run. Silent otherwise. Never blocks or errors the session.

Part of the harness-upstream role — see
.claude/skills/harness.upstream/SKILL.md.
"""

import json
import re
from datetime import date
from pathlib import Path

DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})\.md$")
DEFAULT_CADENCE_DAYS = 30


def _cadence_days(root: Path) -> int:
    state = root / ".claude" / "skills" / "harness.upstream" / "state.json"
    try:
        return int(json.loads(state.read_text()).get("cadenceDays", DEFAULT_CADENCE_DAYS))
    except Exception:
        return DEFAULT_CADENCE_DAYS


def _last_pass(root: Path):
    notes = root / "docs" / "upstream-syncs"
    if not notes.is_dir():
        return None
    dates = []
    for p in notes.glob("*.md"):
        m = DATE_RE.search(p.name)
        if m:
            try:
                dates.append(date(int(m[1]), int(m[2]), int(m[3])))
            except ValueError:
                continue
    return max(dates) if dates else None


def main():
    try:
        root = Path(__file__).resolve().parents[2]
        cadence = _cadence_days(root)
        last = _last_pass(root)
        today = date.today()

        if last is None:
            msg = (
                "Harness upstream sync: no pass on record yet. Run /harness-upstream "
                "to reconcile this harness with the EnHarnes template it was forked from "
                "(take upstream fixes, offer ours back)."
            )
        else:
            age = (today - last).days
            if age < cadence:
                return  # not due — stay silent
            msg = (
                f"Harness upstream sync: last pass was {last.isoformat()} ({age} days ago; "
                f"cadence {cadence}d). A pass is due — run /harness-upstream "
                "(inbound adoption + outbound contribution)."
            )

        out = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": msg,
            }
        }
        print(json.dumps(out))
    except Exception:
        # A reminder is never worth disrupting session start.
        return


if __name__ == "__main__":
    main()
