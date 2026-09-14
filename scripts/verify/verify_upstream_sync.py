#!/usr/bin/env python3
"""Verify the classifier in scripts/harness/upstream_sync.py.

The upstream-sync tool decides, per file, whether an upstream change can be
taken as-is, has already been made here, must be merged by hand, is refused on
purpose, or is none of our business — and, in the other direction, which of our
files are contribution candidates. Those five verdicts are the whole value of
the tool, so they get a deterministic table.

The fixture is hermetic: a throwaway upstream repository with two commits and a
throwaway project tree seeded to produce exactly one file of each verdict. No
network, no reference to the real repository, nothing left behind.

Wired into `make lint` as `lint-upstream`, so CI fails if the classifier
regresses. Exit 0 = every verdict holds; exit 1 = at least one mismatch.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Scrub git's exported environment BEFORE any subprocess call. Run inside the
# pre-commit hook, GIT_DIR / GIT_INDEX_FILE / GIT_WORK_TREE are exported and the
# fixture repositories below would operate on the real worktree instead.
# (AGENTS.md Failure Ledger — the verify_change_size.py incident.)
for _var in [k for k in os.environ if k.startswith("GIT_")]:
    del os.environ[_var]

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "scripts" / "harness" / "upstream_sync.py"

# path -> (content at the upstream baseline, content at the upstream tip)
UPSTREAM = {
    "tracked/adopt.txt": ("v1", "v2"),
    "tracked/converged.txt": ("v1", "v2"),
    "tracked/conflict.txt": ("v1", "v2"),
    "local/localized.txt": ("v1", "v2"),
    "untracked/other.txt": ("v1", "v2"),
}

# path -> content in our tree
OURS = {
    "tracked/adopt.txt": "v1",       # untouched here      -> ADOPT
    "tracked/converged.txt": "v2",   # same fix, made here -> CONVERGED
    "tracked/conflict.txt": "ours",  # both changed        -> CONFLICT
    "local/localized.txt": "ours",   # deliberate divergence -> LOCAL
    "untracked/other.txt": "ours",   # not template-owned  -> IGNORED
    "tracked/new.txt": "ours",       # our own addition    -> outbound NEW
    "tracked/private.txt": "secret", # gitignored -> never a candidate
}

EXPECT_INBOUND = {
    "tracked/adopt.txt": "ADOPT",
    "tracked/converged.txt": "CONVERGED",
    "tracked/conflict.txt": "CONFLICT",
    "local/localized.txt": "LOCAL",
    "untracked/other.txt": "IGNORED",
}

EXPECT_OUTBOUND = {
    "tracked/conflict.txt": "MODIFIED",
    "tracked/new.txt": "NEW",
    # tracked/private.txt is deliberately absent: it matches a tracked glob and
    # exists only here, but git ignores it, so it is nobody's contribution.
    # adopt.txt is deliberately absent: it differs from the tip only because
    # upstream moved, which is inbound's problem, not a contribution.
}


def git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def build_upstream(path: Path) -> tuple[str, str]:
    """Two-commit throwaway upstream. Returns (baseline sha, tip sha)."""
    path.mkdir(parents=True)
    git(["init", "--quiet", "--initial-branch=main"], path)
    git(["config", "user.email", "fixture@example.invalid"], path)
    git(["config", "user.name", "fixture"], path)
    for rel, (before, _) in UPSTREAM.items():
        write(path, rel, before)
    git(["add", "-A"], path)
    git(["commit", "--quiet", "-m", "baseline"], path)
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(path), capture_output=True, text=True, check=True
    ).stdout.strip()
    for rel, (_, after) in UPSTREAM.items():
        write(path, rel, after)
    git(["add", "-A"], path)
    git(["commit", "--quiet", "-m", "upstream moves"], path)
    tip = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(path), capture_output=True, text=True, check=True
    ).stdout.strip()
    return baseline, tip


def build_project(path: Path, upstream: Path, baseline: str) -> None:
    for rel, text in OURS.items():
        write(path, rel, text)
    # A real project is a git tree with things git is told to forget. The walk
    # must ask git, not the filesystem — a gitignored private file was once
    # offered as a contribution to a public template.
    git(["init", "--quiet", "--initial-branch=main"], path)
    write(path, ".gitignore", "tracked/private.txt")
    state = {
        "cadenceDays": 30,
        "upstream": {"name": "fixture", "url": str(upstream), "ref": "main"},
        "baseline": {"commit": baseline, "date": "2020-01-01", "adoptedOn": "2020-01-01", "note": "fixture"},
        "trackedPaths": ["tracked/**", "local/**"],
        "localizedPaths": [{"path": "local/**", "reason": "fixture divergence"}],
        "contributions": [],
    }
    state_path = path / ".claude" / "skills" / "harness.upstream" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def run_tool(root: Path, *args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--root", str(root), *args, "--json"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"FAIL: upstream_sync.py {' '.join(args)} exited {proc.returncode}\n{proc.stderr}")
    return json.loads(proc.stdout)


def run_text(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a subcommand whose output is text, not JSON (`diff`)."""
    return subprocess.run(
        [sys.executable, str(TOOL), "--root", str(root), *args],
        capture_output=True,
        text=True,
    )


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="upstream-sync-verify-") as tmp:
        tmpdir = Path(tmp)
        upstream = tmpdir / "upstream"
        project = tmpdir / "project"
        baseline, tip = build_upstream(upstream)
        build_project(project, upstream, baseline)

        inbound = run_tool(project, "inbound")
        if inbound["tip"] != tip:
            failures.append(f"inbound resolved tip {inbound['tip'][:9]}, expected {tip[:9]}")
        if len(inbound["commits"]) != 1:
            failures.append(f"inbound saw {len(inbound['commits'])} commit(s), expected 1")
        got_in = {row["path"]: row["verdict"] for row in inbound["files"]}
        for path, expected in EXPECT_INBOUND.items():
            actual = got_in.get(path)
            if actual != expected:
                failures.append(f"inbound {path}: got {actual}, expected {expected}")
        for path in got_in.keys() - EXPECT_INBOUND.keys():
            failures.append(f"inbound reported an unexpected path: {path}")

        outbound = run_tool(project, "outbound")
        got_out = {row["path"]: row["kind"] for row in outbound["candidates"]}
        for path, expected in EXPECT_OUTBOUND.items():
            actual = got_out.get(path)
            if actual != expected:
                failures.append(f"outbound {path}: got {actual}, expected {expected}")
        for path in got_out.keys() - EXPECT_OUTBOUND.keys():
            failures.append(f"outbound reported an unexpected candidate: {path}")

        # diff is what makes the protocol's "read the diff, do not assume" step
        # performable. Three cases: a real difference is shown with both sides,
        # a file that matches upstream says so instead of printing nothing, and
        # a path outside trackedPaths is refused rather than silently compared.
        shown = run_text(project, "diff", "tracked/conflict.txt", "--offline")
        if shown.returncode != 0:
            failures.append(f"diff on a changed file exited {shown.returncode}")
        if "-v2" not in shown.stdout or "+ours" not in shown.stdout:
            failures.append(f"diff did not show both sides: {shown.stdout!r}")
        same = run_text(project, "diff", "tracked/converged.txt", "--offline")
        if "identical to upstream" not in same.stdout:
            failures.append(f"diff on an identical file did not say so: {same.stdout!r}")
        refused = run_text(project, "diff", "untracked/other.txt", "--offline")
        if refused.returncode == 0 or "not template-owned" not in refused.stderr:
            failures.append("diff compared a path that is not template-owned")

        # set-baseline is the only writing subcommand — prove it lands and that
        # a re-run then reports nothing inbound.
        subprocess.run(
            [sys.executable, str(TOOL), "--root", str(project), "set-baseline", tip, "--offline"],
            capture_output=True,
            text=True,
            check=True,
        )
        after = json.loads(
            (project / ".claude" / "skills" / "harness.upstream" / "state.json").read_text(encoding="utf-8")
        )
        if after["baseline"]["commit"] != tip:
            failures.append("set-baseline did not record the new commit in state.json")
        if run_tool(project, "inbound")["commits"]:
            failures.append("inbound still reports commits after the baseline advanced")

    if failures:
        print("[verify-upstream-sync] FAIL")
        for line in failures:
            print(f"  - {line}")
        return 1
    total = len(EXPECT_INBOUND) + len(EXPECT_OUTBOUND) + 8
    print(
        f"[verify-upstream-sync] OK: {total} assertions hold "
        "(5 inbound verdicts, 2 outbound kinds, 4 diff cases, baseline advance)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
