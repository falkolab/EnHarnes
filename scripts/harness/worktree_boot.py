#!/usr/bin/env python3
"""worktree_boot.py — Create an isolated git worktree for an agent task.

Steps:
  1. Creates worktree at <main-root>/.claude/worktrees/<task-name> on branch
     task/<task-name>, based on origin/main (fetched first).
  2. Bridges the local environment into it (.env symlinked from the main
     checkout; a dedicated .venv created per worktree)
  3. Auto-detects project type (Python/Node/Rust) and installs dependencies
  4. Runs smoke check (make lint-todos or make lint)

Worktrees live inside the repo under .claude/worktrees/ (git-ignored). This
keeps every task's checkout under the workspace root (so editor links into its
files resolve) rather than as a sibling of the repo.

"Under the repo" means the MAIN checkout, resolved via `git worktree list`, not
whatever directory the script was invoked from. Booting a task from inside
another task's worktree used to nest the new one underneath it, producing paths
like .claude/worktrees/task-a/.claude/worktrees/task-b.

Environment bridging exists because a fresh worktree is a bare checkout: no
`.env`, no virtualenv. Both are git-ignored, so they cannot arrive with the branch
and had to be set up by hand. `.env` is symlinked from the main checkout, so a
secret rotated once is picked up by every task worktree; the virtualenv is created
per worktree and never shared.

Usage:
    python scripts/harness/worktree_boot.py <task-name>

Example:
    python scripts/harness/worktree_boot.py fix-auth-bug
    # Creates .claude/worktrees/fix-auth-bug on branch task/fix-auth-bug

Cleanup:
    git worktree remove .claude/worktrees/<task-name>
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path


# Git's repo-location variables, and only those. Git exports GIT_DIR /
# GIT_INDEX_FILE / GIT_WORK_TREE to the hooks it runs, and a child process
# inherits them; they outrank `cwd`, so every git call below would target
# whatever repo owns that GIT_DIR and this script would create worktrees in the
# wrong repository.
#
# Not the whole `GIT_*` namespace: GIT_SSH_COMMAND, GIT_ASKPASS,
# GIT_TERMINAL_PROMPT and GIT_CONFIG_* carry the credentials and remote config
# that `git fetch` needs — dropping those breaks authenticated fetches, and
# `resolve_base_ref()` then falls back to a stale local `origin/main` — a
# worktree branched off an outdated integration tip, which surfaces much later
# as a PR that no longer fast-forwards.
_GIT_LOCATION_VARS = (
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX",
)


def clean_env() -> dict[str, str]:
    """Environment with git's repo-location overrides removed, credentials intact."""
    return {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}


def run(cmd: list[str], cwd: Path | None = None, check: bool = False) -> int:
    result = subprocess.run(cmd, cwd=cwd, env=clean_env())
    if check and result.returncode != 0:
        sys.exit(result.returncode)
    return result.returncode


def main_worktree_root(start: Path) -> Path:
    """Filesystem root of the MAIN checkout, whatever worktree we were invoked from.

    `git worktree list --porcelain` always lists the main worktree first; every
    linked worktree follows. Using the invoking checkout's root instead would
    nest a new task worktree inside the current one.
    """
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True, text=True, cwd=start, env=clean_env(),
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                return Path(line[len("worktree "):].strip())

    # No fallback to cwd. That fallback WAS the nesting bug, and it would fire
    # precisely when git is misbehaving — the moment least likely to be noticed in
    # scrolling boot output. Stop instead.
    print(
        "[ERROR] Could not resolve the main worktree from "
        f"{start} — `git worktree list` failed"
        f"{': ' + result.stderr.strip() if result.stderr.strip() else ''}.\n"
        "        Refusing to guess: falling back to the current directory is how task "
        "worktrees end up nested inside one another.\n"
        "        Run this from a healthy checkout of the repository.",
        file=sys.stderr,
    )
    sys.exit(1)


def is_git_ignored(worktree_dir: Path, name: str) -> bool:
    """Whether `name` is covered by the repo's ignore rules inside this worktree."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", name],
        cwd=worktree_dir, capture_output=True, env=clean_env(),
    )
    return result.returncode == 0


def link(source: Path, target: Path, label: str) -> bool:
    """Symlink target -> source unless something is already there.

    Refuses to create a link the repo does not ignore: a bridged symlink's blob is
    the absolute path of this machine, so `git add -A` commits that path into the
    branch, and it is a dangling link anywhere else. Beware that a pattern with a
    trailing slash (`.env/`) does NOT cover a symlink — git sees a link, not a
    directory. Fix the ignore rule rather than bypassing this.
    """
    if not source.exists() or target.exists() or target.is_symlink():
        return False
    if not is_git_ignored(target.parent, target.name):
        print(
            f"  [ERROR] refusing to bridge {label}: it is not covered by .gitignore, so "
            f"linking it would commit this machine's absolute path into the branch.\n"
            f"          Add '{target.name}' to .gitignore — with NO trailing slash, since "
            f"a slash matches only directories, not symlinks — then re-run.",
            file=sys.stderr,
        )
        return False
    try:
        target.symlink_to(source, target_is_directory=source.is_dir())
        print(f"  linked {label} -> {source}")
        return True
    except OSError as exc:
        print(f"  [WARN] could not link {label}: {exc}")
        return False


def bridge_environment(main_root: Path, worktree_dir: Path) -> bool:
    """Give a fresh worktree the git-ignored local environment it needs.

    `.env` and `.venv` are untracked, so they cannot arrive with the branch and a
    new worktree starts without either.

    `.env` is symlinked from the main checkout: one source of truth, so a secret
    rotated once is picked up everywhere.

    `.venv` is **never shared** — this worktree gets its own. A symlinked
    virtualenv is shared mutable state between independent checkouts: one task's
    `pip install` mutates the environment every other worktree and the main
    checkout run against, so an unrelated task starts failing with nothing in its
    own diff to explain why.

    Returns False when dependencies must NOT be installed, so the caller does not
    fall back to whatever `pip` is on PATH.
    """
    link(main_root / ".env", worktree_dir / ".env", ".env")

    target_venv = worktree_dir / ".venv"
    if target_venv.exists() or target_venv.is_symlink():
        return True

    print("  creating a dedicated virtualenv")
    if run([sys.executable, "-m", "venv", str(target_venv)], cwd=worktree_dir) != 0:
        # Not the ambient pip: that is very likely the global environment, which
        # AGENTS.md forbids installing into, and it would contaminate the main
        # checkout. Better to leave the worktree without dependencies and say so.
        print(
            "  [ERROR] could not create a virtualenv for this worktree — skipping "
            "dependency installation.\n"
            "          Create one manually, then re-run this script.",
            file=sys.stderr,
        )
        return False

    return True

def venv_bin(worktree_dir: Path, program: str) -> str | None:
    """Path to `program` inside the worktree's (bridged) virtualenv, if present."""
    candidate = worktree_dir / ".venv" / "bin" / program
    return str(candidate) if candidate.exists() else None


def resolve_base_ref(repo_root: Path) -> str:
    """Ref to base a new worktree on: origin/main after a best-effort fetch,
    falling back to local 'main' when the remote is unreachable (offline).

    Basing on origin/main rather than the invoking checkout's HEAD keeps the new
    branch on the integration tip, so its PR stays fast-forwardable.
    """
    fetch_rc = run(["git", "fetch", "origin", "main"], cwd=repo_root)
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "origin/main"],
        capture_output=True, text=True, cwd=repo_root, env=clean_env(),
    )
    if probe.returncode == 0:
        if fetch_rc != 0:
            # A cached origin/main exists but this fetch failed (auth, network,
            # remote down). Basing on it is reasonable — it is still the best
            # known integration tip — but it may be behind, so say so rather than
            # letting a stale base look like a fresh one.
            print(
                "[WARN] 'git fetch origin main' failed; basing on the CACHED origin/main, "
                "which may be stale. Re-run once connectivity/credentials are back if the "
                "branch turns out behind."
            )
        return "origin/main"
    print("[WARN] origin/main not found (offline?). Basing new worktree on local 'main'.")
    return "main"


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: worktree_boot.py <task-name>")
        return 1

    task_name = sys.argv[1]
    repo_root = main_worktree_root(Path.cwd())
    worktree_dir = repo_root / ".claude" / "worktrees" / task_name
    branch_name = f"task/{task_name}"

    print(f"=== Worktree Boot: {task_name} ===")
    if repo_root != Path.cwd():
        print(f"Main checkout: {repo_root}")

    # 1. Create worktree — under .claude/worktrees/ (git-ignored), based on
    #    origin/main so it never inherits a stale HEAD.
    if worktree_dir.exists():
        print(f"Worktree {worktree_dir} already exists. Reusing.")
    else:
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)
        base_ref = resolve_base_ref(repo_root)
        print(f"Creating worktree at {worktree_dir} on branch {branch_name} (base: {base_ref})...")
        run(["git", "worktree", "add", str(worktree_dir), "-b", branch_name, base_ref], cwd=repo_root, check=True)

    # 2. Bridge the git-ignored local environment (.env / .venv) into the worktree
    print("\n-- Bridging local environment --")
    deps_safe = bridge_environment(repo_root, worktree_dir)

    # 3. Install dependencies (auto-detect runtime), preferring the bridged venv
    print("\n-- Installing dependencies --")
    if not deps_safe:
        print("  skipped (no isolated environment to install into — see the error above)")
    elif (worktree_dir / "requirements.txt").exists():
        pip = venv_bin(worktree_dir, "pip") or shutil.which("pip") or "pip"
        rc = run([pip, "install", "-r", "requirements.txt"], cwd=worktree_dir)
        if rc != 0:
            run([pip, "install", "pytest"], cwd=worktree_dir)
    elif (worktree_dir / "package.json").exists():
        if shutil.which("bun"):
            run(["bun", "install"], cwd=worktree_dir)
        elif shutil.which("npm"):
            run(["npm", "install"], cwd=worktree_dir)
    elif (worktree_dir / "Cargo.toml").exists():
        run(["cargo", "build"], cwd=worktree_dir)
    else:
        print("No dependency file detected. Installing pytest for structural tests.")
        pip = venv_bin(worktree_dir, "pip") or shutil.which("pip") or "pip"
        run([pip, "install", "pytest"], cwd=worktree_dir)

    # 4. Run smoke check
    print("\n-- Running smoke check --")
    make = shutil.which("make")
    if make and (worktree_dir / "Makefile").exists():
        rc = run([make, "lint-todos"], cwd=worktree_dir)
        if rc != 0:
            run([make, "lint"], cwd=worktree_dir)
    else:
        print("[WARN] make not available or no Makefile. Skipping smoke check.")

    print(f"\n=== Worktree ready at {worktree_dir} (branch: {branch_name}) ===")
    print(f"To clean up: git worktree remove {worktree_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
