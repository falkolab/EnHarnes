#!/usr/bin/env python3
"""PostToolUse hook: lint the file that was just edited, and say so immediately.

Turns "remember to run `make lint` after each change" (a rule an agent has to
hold in its head) into a deterministic loop that reports a violation next to the
edit that caused it, instead of at the pre-PR gate several steps later.

Scope is deliberately narrow so the loop stays fast:
  * only Edit / Write / NotebookEdit
  * only `.py` files under `src/` (where the ast-grep rule set applies)
  * only the single edited file — never a repo-wide scan

Never blocks. Exit 2 on PostToolUse is feedback, not a veto (the write already
happened), and any infrastructure problem — missing ast-grep, timeout, unexpected
output — exits 0 silently. A linter that wedges the edit loop is worse than one
that occasionally stays quiet; `make lint` remains the authority.
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_io  # noqa: E402  (path shim above must run first)

# NotebookEdit is absent on purpose: it targets .ipynb, and the .py filter below
# would make it permanently inert here.
EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}
TIMEOUT_SECONDS = 15
MAX_FINDINGS_SHOWN = 10


def repo_root(cwd: str | None) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, cwd=cwd or None, timeout=5,
            env=hook_io.git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def scan(root: Path, rel_path: str) -> list[dict]:
    """ast-grep findings for one file, or [] if the scan could not run."""
    if not (root / "sgconfig.yml").exists():
        return []
    try:
        result = subprocess.run(
            ["ast-grep", "scan", "-c", "sgconfig.yml", "--json=compact", rel_path],
            capture_output=True, text=True, cwd=root, timeout=TIMEOUT_SECONDS,
            env=hook_io.git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    payload = result.stdout.strip()
    if not payload:
        return []
    try:
        findings = json.loads(payload)
    except json.JSONDecodeError:
        return []
    return findings if isinstance(findings, list) else []


def format_findings(findings: list[dict]) -> str:
    lines = ["Lint findings in the file you just edited:"]
    for item in findings[:MAX_FINDINGS_SHOWN]:
        location = item.get("range", {}).get("start", {}).get("line")
        line_no = location + 1 if isinstance(location, int) else "?"
        lines.append(
            f"  {item.get('file', '?')}:{line_no}  [{item.get('ruleId', '?')}] "
            f"{item.get('message', '').strip()}"
        )
        note = (item.get("note") or "").strip()
        if note:
            lines.append(f"      {note}")
    remaining = len(findings) - MAX_FINDINGS_SHOWN
    if remaining > 0:
        lines.append(f"  ... and {remaining} more.")
    lines.append("Fix these now — `make lint` blocks the commit on them.")
    return "\n".join(lines)


def main() -> None:
    # Non-blocking by contract: an unreadable payload is not this hook's problem.
    event = hook_io.read_event()
    if event is None or event.tool_name not in EDIT_TOOLS:
        return

    path = event.file_path
    if not path.endswith(".py"):
        return

    root = repo_root(event.raw.get("cwd"))
    if root is None:
        return

    try:
        rel_path = str(Path(path).resolve().relative_to(root.resolve()))
    except ValueError:
        return  # edited a file outside the repo

    if not rel_path.startswith("src/"):
        return

    findings = scan(root, rel_path)
    if findings:
        hook_io.block(format_findings(findings))  # exit 2 == feedback here, not a veto


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # never blocks, never wedges the edit loop — make lint remains the authority
