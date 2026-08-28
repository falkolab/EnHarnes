#!/usr/bin/env python3
"""change_size.py — PR-time half of the change-size guardrails.

Measures a branch's change size (net changed lines + files vs the integration
branch) and its staleness (commits behind + wall-clock age), compares them to
`policies/size-policy.json`, and reports warn/block. Shared verbatim by
`pre_pr_gate.py` (local `make review`) and the PR CI job so both
measure identically.

A hard-budget block is converted to a recorded exception by a
`SIZE-OVERRIDE: <reason>` line in any commit message in the range
`<base>..HEAD` (checked the same way locally and in CI — the PR *description*
is not a dependable CI variable, a commit message is).

Pure git + stdlib. Run stand-alone in CI:
    python .claude/skills/harness.linters/scripts/change_size.py
    # honours $CHANGE_SIZE_BASE_SHA (any CI: export the PR's base SHA into it,
    # e.g. github.event.pull_request.base.sha) and GitLab's
    # $CI_MERGE_REQUEST_DIFF_BASE_SHA when present
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
POLICY_PATH = ROOT / "policies" / "size-policy.json"

BIG = 10**9  # "no threshold set" sentinel


# Git's repo-location variables. Inherited (e.g. from a git hook) they point
# every git call at whatever repo exported them, so this script would measure
# the wrong repository. Credential/config vars (GIT_SSH_COMMAND, GIT_ASKPASS,
# GIT_CONFIG_*) are deliberately kept — same policy as hook_io.git_env().
_GIT_LOCATION_VARS = (
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX",
)


def _git_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}


def _git(args: list[str], default: str = "") -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True, cwd=ROOT, env=_git_env())
    return r.stdout if r.returncode == 0 else default


def load_policy() -> dict:
    if POLICY_PATH.exists():
        try:
            return json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def merge_base(default_branch: str, base_sha: str | None = None) -> str:
    """The commit HEAD diverged from. Prefer an explicit base_sha (from the CI
    env, see __main__); else merge-base with origin/<default>, else with local
    <default>."""
    if base_sha:
        return base_sha.strip()
    for ref in (f"origin/{default_branch}", default_branch):
        mb = _git(["merge-base", ref, "HEAD"]).strip()
        if mb:
            return mb
    return ""


def _excluded(path: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in globs)


def measure_diff(base: str, policy: dict) -> dict:
    """Net changed lines (added+removed) and file count on HEAD since `base`,
    after exclusion globs. Three-dot: only changes introduced on the HEAD side."""
    if not base:
        return {"lines": 0, "files": 0}
    globs = policy.get("diff", {}).get("excludeGlobs", [])
    lines = files = 0
    for row in _git(["diff", "--numstat", f"{base}...HEAD"]).splitlines():
        parts = row.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if _excluded(path, globs):
            continue
        lines += (int(added) if added.isdigit() else 0)
        lines += (int(removed) if removed.isdigit() else 0)
        files += 1
    return {"lines": lines, "files": files}


def measure_staleness(default_branch: str) -> dict:
    """Commits HEAD is behind origin/<default>, and wall-clock age in days of
    HEAD's first unique commit (0 if the branch has no unique commits)."""
    ref = f"origin/{default_branch}"
    behind_s = _git(["rev-list", "--count", f"HEAD..{ref}"]).strip()
    behind = int(behind_s) if behind_s.isdigit() else 0
    first = _git(["log", "--reverse", "--format=%ct", f"{ref}..HEAD"]).splitlines()
    if first and first[0].strip().isdigit():
        age_days = max(0, int((time.time() - int(first[0].strip())) // 86400))
    else:
        age_days = 0
    return {"behind": behind, "age_days": age_days}


def override_reason(base: str, token: str) -> str | None:
    """Reason from the first commit message in base..HEAD carrying the override
    token (line-anchored), else None."""
    if not base:
        return None
    for line in _git(["log", "--format=%B", f"{base}..HEAD"]).splitlines():
        s = line.strip()
        if s.startswith(token):
            return s[len(token):].strip() or "(no reason given)"
    return None


def run_report(base_sha: str | None = None) -> int:
    """Measure, print a report, return 0 (ok / warn / overridden) or 1 (block)."""
    policy = load_policy()
    if not policy:
        print("  [size] no policies/size-policy.json — skipping change-size checks.")
        return 0

    default_branch = policy.get("branch", {}).get("defaultBranch", "main")
    token = policy.get("overrideToken", "SIZE-OVERRIDE:")
    d = policy.get("diff", {})
    b = policy.get("branch", {})

    base = merge_base(default_branch, base_sha)
    if not base:
        # Could not resolve the diff base (no origin/<default> locally, unfetched
        # clone, or a differently-named remote). Say so — do NOT report "0 lines"
        # as if measured, which would be a silent false-green. Non-blocking.
        print(f"  [size] could not resolve origin/{default_branch} — change-size "
              f"not measured (run `git fetch origin {default_branch}`).")
        return 0

    diff = measure_diff(base, policy)
    stale = measure_staleness(default_branch)
    reason = override_reason(base, token)

    soft: list[str] = []
    hard: list[str] = []

    if diff["lines"] >= d.get("blockLines", BIG) or diff["files"] >= d.get("blockFiles", BIG):
        hard.append(f"diff {diff['lines']} lines / {diff['files']} files "
                    f"(hard {d.get('blockLines')} / {d.get('blockFiles')})")
    elif diff["lines"] >= d.get("warnLines", BIG) or diff["files"] >= d.get("warnFiles", BIG):
        soft.append(f"diff {diff['lines']} lines / {diff['files']} files "
                    f"(soft {d.get('warnLines')} / {d.get('warnFiles')})")

    if stale["behind"] >= b.get("blockCommitsBehind", BIG) or stale["age_days"] >= b.get("blockAgeDays", BIG):
        hard.append(f"branch {stale['behind']} behind {default_branch}, age {stale['age_days']}d "
                    f"(hard {b.get('blockCommitsBehind')} behind / {b.get('blockAgeDays')}d)")
    elif stale["behind"] >= b.get("warnCommitsBehind", BIG) or stale["age_days"] >= b.get("warnAgeDays", BIG):
        soft.append(f"branch {stale['behind']} behind {default_branch}, age {stale['age_days']}d "
                    f"(soft {b.get('warnCommitsBehind')} behind / {b.get('warnAgeDays')}d)")

    if not soft and not hard:
        print(f"  size OK: diff {diff['lines']} lines / {diff['files']} files; "
              f"branch {stale['behind']} behind {default_branch}, age {stale['age_days']}d.")
        return 0

    for s in soft:
        print(f"  [warn] over soft budget: {s}")

    if hard:
        for h in hard:
            print(f"  {'[warn]' if reason else '[BLOCK]'} over HARD budget: {h}")
        if reason:
            print(f"  SIZE-OVERRIDE accepted: {reason}")
            return 0
        print(f"  Blocked. Add a commit whose message contains "
              f"'{token} <reason>', or split the branch / rebase onto {default_branch}.")
        return 1

    return 0  # soft-only → warn, do not block


if __name__ == "__main__":
    sys.exit(run_report(
        os.environ.get("CHANGE_SIZE_BASE_SHA")
        or os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA")
        or None
    ))
