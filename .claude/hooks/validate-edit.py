#!/usr/bin/env python3
"""PreToolUse hook: refuse to edit files while their checkout sits on main.

Closes the last hole in the "an agent never writes to main" rule. Pushing to main
and force-pushing were already guarded (validate-bash.py) and merging is a human
step, but nothing stopped an agent from editing files directly in a main
checkout — the change would then land via an ordinary local commit.

Work happens on a task branch in its own worktree:

    python scripts/harness/worktree_boot.py <task-name>

The branch is resolved from **the repository that owns the target file**, never
from the process's working directory. Those differ more often than they look:
task worktrees live *inside* the main checkout (`<main-root>/.claude/worktrees/`),
so an absolute path — or one relative path too many — reaches files on `main`
while `cwd` still reports a safe task branch. A `cwd`-based check waves that
through.

Fails CLOSED — blocks — whenever it cannot establish that the target is on a
non-protected branch: unparsable payload, detached HEAD, git being unavailable,
or a matched edit tool arriving with no resolvable target path. "Cannot tell
where I am" must not read as "must be safe".

**Scope: branch content only.** `git rev-parse --is-inside-work-tree` reports
`false` inside `.git/`, so a write to `.git/hooks/pre-commit` or `.git/config` is
NOT blocked here — which can disarm the pre-commit `make lint` gate without
touching a tracked file. That is deliberate — repository plumbing is not branch
content, and Bash writes are not policed either — but do not read "an agent never
writes to main" as covering it.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_io  # noqa: E402  (path shim above must run first)

EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
PROTECTED_BRANCHES = {"main", "master"}

# Bookkeeping that must stay possible on main. Nothing here is project source:
# these are the harness's own audit trail, which the operator may reconcile in a
# main checkout between tasks.
ALLOWED_ON_MAIN = {
    "docs/activity-log.md",
    "progress.txt",
}

GUIDANCE = (
    "Editing files on '{branch}' is blocked — agents do not write to the integration "
    "branch (AGENTS.md; the human merges PRs).\n"
    "Start a task worktree instead:  python scripts/harness/worktree_boot.py <task-name>\n"
    "Blocked path: {path}"
)


def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess | None:
    """Run git with repo-location env overrides stripped. None if it could not run.

    Without `env=`, an inherited GIT_DIR would redirect these queries at whatever
    repo exported it, and the guard would judge the wrong branch.
    """
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True, text=True, cwd=cwd, timeout=5, env=hook_io.git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None


def anchor_dir(path: str) -> Path | None:
    """Nearest existing directory at or above `path`.

    A Write may target a file — or a whole directory tree — that does not exist
    yet, so git has to be asked about the closest ancestor that does.
    """
    try:
        current = Path(path).resolve().parent
    except (OSError, ValueError, RuntimeError):
        return None
    for candidate in [current, *current.parents]:
        if candidate.is_dir():
            return candidate
    return None


def main() -> None:
    event = hook_io.require_event("validate-edit")

    if event.tool_name not in EDIT_TOOLS:
        return

    path = event.file_path
    if not path:
        # A matched edit tool with no resolvable target means this guard cannot see
        # what is being written — a stop, not a shrug. Reading "no path" as "safe"
        # is what let NotebookEdit through (it names its target `notebook_path`)
        # while the matcher and the registration check both claimed coverage.
        hook_io.block(
            f"validate-edit: {event.tool_name} arrived with no resolvable target path "
            f"(keys: {sorted(event.tool_input)}) — blocking to fail closed."
        )

    anchor = anchor_dir(path)
    if anchor is None:
        hook_io.block(f"validate-edit: cannot resolve a directory for {path} — blocking to fail closed.")

    inside = git(["rev-parse", "--is-inside-work-tree"], anchor)
    if inside is None:
        hook_io.block(
            "validate-edit: could not run git to determine the target's branch — "
            "blocking to fail closed."
        )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return  # outside any git repo — not ours to police

    branch_probe = git(["rev-parse", "--abbrev-ref", "HEAD"], anchor)
    if branch_probe is None or branch_probe.returncode != 0:
        hook_io.block("validate-edit: could not read the target's branch — blocking to fail closed.")

    branch = branch_probe.stdout.strip()
    if not branch or branch == "HEAD":
        hook_io.block(
            f"validate-edit: {path} sits on a detached HEAD — cannot confirm this is not main. "
            "Check out a task branch before editing."
        )

    if branch not in PROTECTED_BRANCHES:
        return

    top = git(["rev-parse", "--show-toplevel"], anchor)
    if top is None or top.returncode != 0:
        hook_io.block(GUIDANCE.format(branch=branch, path=path))

    try:
        relative = str(Path(path).resolve().relative_to(Path(top.stdout.strip()).resolve()))
    except (OSError, ValueError, RuntimeError):
        relative = ""  # cannot place it inside the repo → not on the allowlist

    if relative in ALLOWED_ON_MAIN:
        return

    hook_io.block(GUIDANCE.format(branch=branch, path=path))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # a guard must not fail OPEN via exit 1 on an odd payload shape
        hook_io.block(f"validate-edit: internal error — blocking to fail closed: {exc!r}")
