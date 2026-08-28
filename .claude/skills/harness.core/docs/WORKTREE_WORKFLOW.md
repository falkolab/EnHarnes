# Worktree Workflow

Each agent task runs in an isolated git worktree. No shared state between tasks.

## Create and Boot

```bash
# One command: creates worktree, installs deps, runs smoke
python scripts/harness/worktree_boot.py feature-x
```

This creates `.claude/worktrees/feature-x` on branch `task/feature-x`, based on
`origin/main`. Worktrees live inside the repo under `.claude/worktrees/` (which is
git-ignored). Keeping them under the workspace root means editor links into a
task's files resolve.

## Manual Steps

```bash
# Create worktree manually — always base off origin/main, never the current HEAD
git fetch origin main
git worktree add .claude/worktrees/feature-x -b task/feature-x origin/main

# Enter and work
cd .claude/worktrees/feature-x
make lint-todos

# When done
cd -
git worktree remove .claude/worktrees/feature-x
```

## Local Environment

A fresh worktree is a bare checkout: `.env` and `.venv` are git-ignored, so they
cannot arrive with the branch. `worktree_boot.py` symlinks **`.env`** from the MAIN
checkout, so a secret rotated once is picked up everywhere.

The virtualenv is **never shared** — each worktree creates its own. A symlinked
`.venv` is mutable state shared between independent checkouts: one task's
`pip install` changes the environment every other worktree and the main checkout
run against, so an unrelated task starts failing with nothing in its own diff to
explain it. One `pip install` per boot is the price, and it is the right one.

Anything bridged **must be covered by `.gitignore`, with no trailing slash.** A
trailing slash matches only a directory, and these are symlinks — git sees a link,
so `.venv/` does not cover `.venv`. An uncovered symlink gets committed and leaks
the absolute path of whoever booted it. The boot script refuses to bridge a name
it cannot confirm is ignored.

## Naming Convention

| Task type | Worktree path | Branch |
|-----------|---------------|--------|
| Feature | `.claude/worktrees/feature-x` | `task/feature-x` |
| Hotfix | `.claude/worktrees/hotfix-123` | `task/hotfix-123` |
| Entropy cleanup | `.claude/worktrees/entropy-2026-02` | `task/entropy-2026-02` |

## Rules

- Each parallel agent task gets its own worktree.
- Never share a worktree between concurrent tasks.
- Always base a new worktree on `origin/main`, never the invoking checkout's HEAD
  (the boot script does this via `resolve_base_ref()`; a stale HEAD produces a
  non-fast-forward PR — see the Failure Ledger in `AGENTS.md`).
- Clean up worktrees after merge: `git worktree remove <path>`.
- The boot script auto-detects runtime and installs dependencies.
- `worktree_boot.py` can be run from inside any checkout of the repo — it resolves
  the MAIN checkout itself (`git worktree list`), so booting from inside another
  task's worktree cannot nest the new one underneath it.
- Worktrees created under the old `../worktree_<task>` convention keep working as-is; only new ones go under `.claude/worktrees/`.
- Lint/registry scripts skip `.claude/worktrees/`, so a nested worktree's files never pollute another task's `make lint`.
