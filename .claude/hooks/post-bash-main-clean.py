#!/usr/bin/env python3
"""PostToolUse hook: notice when a Bash command has written into the `main` checkout.

`validate-edit.py` stops Edit/Write/MultiEdit from touching the integration branch.
It cannot see a write made by a shell command — `python3 - <<'PY'`, `sed -i`, `tee`,
a redirection, an editor, a compiled binary. Those reach the same files by a path no
PreToolUse guard watches, and the agent gets no signal at all.

This hook closes that gap from the other side: it never looks at the command. It
compares the `main` checkout's working tree before and after, and reports what
appeared. Reading a command's intent is a race the guard always loses — a new way to
write a file is a new bypass. Observing the effect is not a race: whatever wrote the
file, the file is dirty.

Detective, not preventive, and deliberately so. The write has already happened when
this runs, but nothing is committed, the remedy is one `git checkout --`, and the
damage is zero. The failure it exists to catch is a slip, not an attack: an agent
reaching for a script because it is convenient, with no idea it has stepped around a
rule it would have obeyed if asked.

Deliberate scope:
  * The paths an agent may write on `main` are excluded — one shared set,
    `hook_io.ALLOWED_ON_MAIN`, which `validate-edit.py` enforces from the other side.
  * The state lives in the repository's shared git admin directory, so parallel
    sessions share one view.
  * Only paths that are NEW since the last look are reported.

Fails open — no git, an unwritable state file, an unexpected error all exit 0, because
a bug in a notice must not wedge the shell. But two of those silences used to swallow
the very thing this hook exists to catch, and both are now loud (review, 2026-09-15):

  * **A missing or corrupt baseline.** The old code adopted whatever was dirty as the
    new "clean" state and said nothing — so the first Bash call after a fresh clone, or
    after the state file was lost, absorbed exactly the slip it was built to report.
    Worse, the docstring claimed the opposite ("reported once"), which is what would
    stop a reader looking. Now: establishing a baseline over a dirty tree reports what
    it adopted.
  * **A detached `main` checkout.** `git worktree list` stops naming a branch while
    HEAD is detached — an everyday state during history archaeology or a bisect — and
    the hook went fully blind with no signal, then silently un-blind on re-attach. Now:
    if a `main` checkout was seen before and cannot be found, the lapse is reported
    once.

The shape behind both: a notice-only guard must never have a failure mode whose symptom
is silence, because silence is also what "all clear" looks like.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_io  # noqa: E402  (path shim above must run first)

# One shared set with validate-edit.py — see hook_io.ALLOWED_ON_MAIN for why.
ALLOWED_ON_MAIN = hook_io.ALLOWED_ON_MAIN
# No project name in it: this file is template-owned, and the shared-zone rule
# says such a file carries no project nouns. Renaming orphans an existing state
# file, which costs one reported baseline adoption and nothing else.
STATE_FILENAME = "main-clean-baseline.json"
TIMEOUT_SECONDS = 10
MAX_PATHS_SHOWN = 12


def _git(args: list[str], cwd: str | Path | None = None) -> str | None:
    """Run git and return stdout, or None if it could not run or failed."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True, text=True, cwd=str(cwd) if cwd else None,
            timeout=TIMEOUT_SECONDS, env=hook_io.git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def main_checkout(cwd: str | None) -> Path | None:
    """The worktree that has `main` (or `master`) checked out, if any.

    `git worktree list --porcelain` emits, per worktree, a `worktree <path>` line and
    a `branch refs/heads/<name>` line (absent when detached). The bare top-level of
    this repository has no branch, so it is skipped naturally — and so, deliberately,
    is a detached `main` checkout, which the caller turns into a reported lapse rather
    than silence.
    """
    listing = _git(["worktree", "list", "--porcelain"], cwd)
    if listing is None:
        return None
    path: str | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.startswith("branch ") and path:
            branch = line[len("branch "):].strip()
            if branch in ("refs/heads/main", "refs/heads/master"):
                return Path(path)
    return None


def dirty_paths(worktree: Path) -> set[str] | None:
    """Repo-relative paths that are modified, added or untracked in `worktree`."""
    porcelain = _git(["status", "--porcelain", "--untracked-files=normal"], worktree)
    if porcelain is None:
        return None
    paths: set[str] = set()
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:]
        # Renames read "old -> new"; the destination is the one that exists now.
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        entry = entry.strip().strip('"')
        if entry and entry not in ALLOWED_ON_MAIN:
            paths.add(entry)
    return paths


def state_file(cwd: str | None) -> Path | None:
    """State location: the shared git admin directory, so all worktrees agree."""
    common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
    if common is None:
        # Older git without --path-format: fall back to the plain form.
        common = _git(["rev-parse", "--git-common-dir"], cwd)
    if common is None:
        return None
    directory = Path(common.strip())
    if not directory.is_absolute():
        directory = (Path(cwd) / directory).resolve() if cwd else directory.resolve()
    return directory / STATE_FILENAME


def read_state(path: Path) -> dict:
    """The stored state, or `{}` when there is none or it cannot be read.

    `{}` is deliberately distinguishable from a stored empty baseline: the caller
    treats "no baseline at all" as a first look worth reporting, not as "all clear".
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def baseline_of(state: dict) -> set[str] | None:
    known = state.get("dirty")
    return set(known) if isinstance(known, list) else None


def write_state(path: Path, paths: set[str], main_seen: str | None, blind_reported: bool) -> None:
    payload: dict = {"dirty": sorted(paths), "blind_reported": blind_reported}
    if main_seen:
        payload["main_seen"] = main_seen
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass  # bookkeeping only — never worth failing the shell over


def format_first_look(worktree: Path, adopted: set[str]) -> str:
    shown = sorted(adopted)[:MAX_PATHS_SHOWN]
    return "\n".join([
        "The `main` checkout was already dirty the first time this guard looked, so it "
        "has adopted the following as its baseline and will not mention them again:",
        f"Checkout: {worktree}",
        *[f"  {p}" for p in shown],
        *([f"  ... and {len(adopted) - len(shown)} more."] if len(adopted) > len(shown) else []),
        "",
        "Said out loud because staying silent here would hide exactly what this guard "
        "exists to catch: the first Bash call after the state is lost is also the one "
        "that could have made this mess. Check whether these are yours; if they are "
        "unexpected, revert them and redo the work in a task worktree.",
    ])


def format_blind(last_seen: str) -> str:
    return "\n".join([
        "This guard has gone blind: a `main` checkout was protected before "
        f"({last_seen}) and cannot be found now.",
        "The usual cause is a detached HEAD in that checkout — history archaeology, a "
        "bisect, a `git checkout <sha>`. While it lasts, a Bash command writing into "
        "that checkout will NOT be reported.",
        "Re-attach it (`git checkout main` there) to restore the notice. Said once, not "
        "on every command.",
    ])


def format_report(worktree: Path, appeared: set[str]) -> str:
    shown = sorted(appeared)[:MAX_PATHS_SHOWN]
    lines = [
        "A Bash command just wrote into the `main` checkout — the integration branch "
        "agents do not write to (AGENTS.md; a human does the merging).",
        f"Checkout: {worktree}",
        "Changed by this command:",
    ]
    lines += [f"  {p}" for p in shown]
    remaining = len(appeared) - len(shown)
    if remaining > 0:
        lines.append(f"  ... and {remaining} more.")
    revert = " ".join(shlex.quote(p) for p in shown[:3]) or "<files>"
    lines += [
        "",
        "`validate-edit` would have refused this through Edit/Write; a shell command "
        "reaches the same files by a route it cannot see. Nothing is committed, so the "
        "fix is cheap:",
        f"  git -C {shlex.quote(str(worktree))} checkout -- {revert}",
        "then redo the work in a task worktree: "
        "python scripts/harness/worktree_boot.py <task-name>",
        "",
        "If the change was deliberate and sanctioned, keep it and continue — this is a "
        "notice, not a veto.",
    ]
    return "\n".join(lines)


def main() -> None:
    event = hook_io.read_event()
    if event is None or event.tool_name != "Bash":
        return

    cwd = event.raw.get("cwd")
    path = state_file(cwd)
    if path is None:
        return  # not a git repository we can reason about
    state = read_state(path)

    worktree = main_checkout(cwd)
    if worktree is None:
        # Nothing to protect — UNLESS we protected something here before, in which case
        # protection has lapsed (usually a detached HEAD) and silence would be a lie.
        last_seen = state.get("main_seen")
        if last_seen and not state.get("blind_reported"):
            write_state(path, baseline_of(state) or set(), last_seen, blind_reported=True)
            hook_io.block(format_blind(last_seen))
        return

    current = dirty_paths(worktree)
    if current is None:
        return  # git could not answer; the next call tries again

    baseline = baseline_of(state)
    write_state(path, current, str(worktree), blind_reported=False)

    if baseline is None:
        # First look. Adopting a dirty tree in silence would swallow exactly the write
        # this guard exists to catch, so say what was adopted (review, 2026-09-15).
        if current:
            hook_io.block(format_first_look(worktree, current))
        return

    appeared = current - baseline
    if appeared:
        hook_io.block(format_report(worktree, appeared))  # exit 2 == feedback here


if __name__ == "__main__":
    # Fails OPEN by design: this is a notice, and a bug in it must not wedge every
    # shell command. The reason is printed rather than swallowed — a silently broken
    # guard is the failure mode this whole hook exists to make visible.
    try:
        main()
    except Exception as exc:
        print(f"post-bash-main-clean: skipped — {exc!r}", file=sys.stderr)
