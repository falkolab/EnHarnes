#!/usr/bin/env python3
"""upstream_sync.py — Compare this harness against the upstream template it was forked from.

This harness was copied from a template repository, named in
.claude/skills/harness.upstream/state.json. Forks rot in both directions:
upstream ships fixes we never see, and we fix template-owned code upstream still
has broken. This tool answers, deterministically and offline-testably:

  inbound   What has upstream changed since our baseline, and what can we take?
  outbound  What have we changed in template-owned files — the PR candidates?
  status    Where is the baseline, where is upstream, how stale is the pass?
  diff      How does one tracked file differ from upstream — the text itself?

Only `set-baseline` writes anything; the reporting subcommands are read-only.

Inbound classification, per file, from three contents (ours / upstream at the
baseline / upstream at the tip):

  ADOPT      upstream changed it, our copy still matches the baseline -> take it
  CONVERGED  upstream changed it, our copy already matches the tip -> nothing to do
  CONFLICT   upstream changed it and so did we, differently -> merge by hand
  LOCAL      path is on the deliberate-divergence list -> report only, never take
  IGNORED    path is not template-owned (product code, handoffs, intake) -> skip

Usage (from the repo root):

    python scripts/harness/upstream_sync.py status
    python scripts/harness/upstream_sync.py inbound [--json] [--offline]
    python scripts/harness/upstream_sync.py outbound [--json] [--offline]
    python scripts/harness/upstream_sync.py diff <path> [<path> ...] [--offline]
    python scripts/harness/upstream_sync.py set-baseline <sha> [--date YYYY-MM-DD]

`--root <dir>` points state, cache and working tree at another directory; that is
what lets scripts/verify/verify_upstream_sync.py exercise the classifier against
fabricated repositories with no network.

Procedure and boundaries: .claude/skills/harness.upstream/SKILL.md
Reasoning and cadence:    docs/upstream-sync-protocol.md
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

STATE_REL = Path(".claude/skills/harness.upstream/state.json")
CACHE_REL = Path(".harness-cache/upstream.git")

# Directories never walked when collecting our own tracked files.
SKIP_DIRS = {
    ".git",
    ".harness-cache",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    ".claude/worktrees",
}

ADOPT, CONVERGED, CONFLICT, LOCAL, IGNORED = (
    "ADOPT",
    "CONVERGED",
    "CONFLICT",
    "LOCAL",
    "IGNORED",
)


class SyncError(Exception):
    """A failure the caller should see as a message, not a traceback."""


# --- globbing -----------------------------------------------------------------


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob to a regex.

    '**/' matches any number of leading directories, '**' matches anything,
    '*' and '?' stop at a path separator. Everything else is literal.
    """
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(out) + "$")


class PathMatcher:
    """Tracked-path globs plus the deliberate-divergence list."""

    def __init__(self, tracked: list[str], localized: list[dict]):
        self._tracked = [_glob_to_regex(p) for p in tracked]
        self._localized = [(_glob_to_regex(e["path"]), e.get("reason", "")) for e in localized]

    def is_tracked(self, path: str) -> bool:
        return any(rx.match(path) for rx in self._tracked)

    def local_reason(self, path: str) -> str | None:
        for rx, reason in self._localized:
            if rx.match(path):
                return reason
        return None


# --- git ----------------------------------------------------------------------


def _git(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SyncError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _blob(cache: Path, commit: str, path: str) -> bytes | None:
    """File content at a commit, or None when the file is absent there."""
    proc = subprocess.run(
        ["git", "-C", str(cache), "show", f"{commit}:{path}"],
        capture_output=True,
    )
    return proc.stdout if proc.returncode == 0 else None


# `git clone --bare` writes a remote with a URL and NO fetch refspec — unlike a
# normal clone, which gets `+refs/heads/*:refs/remotes/origin/*`. Without one,
# `git fetch origin` updates FETCH_HEAD and nothing else, so every branch in the
# mirror stays frozen at the moment it was cloned. The tool then reported "up to
# date" for as long as the cache existed, whatever upstream did: a silent wrong
# answer from the one command whose entire job is to notice movement. Setting the
# refspec explicitly (rather than cloning `--mirror`) keeps push semantics off a
# cache we only ever read.
_MIRROR_REFSPEC = "+refs/heads/*:refs/heads/*"


def refresh_cache(cache: Path, url: str, offline: bool) -> None:
    """Clone or update the bare mirror of upstream under .harness-cache/."""
    if not cache.exists():
        if offline:
            raise SyncError(f"no upstream cache at {cache} and --offline was given")
        cache.parent.mkdir(parents=True, exist_ok=True)
        try:
            _git(["clone", "--bare", "--quiet", url, str(cache)])
        except SyncError as exc:
            raise SyncError(f"cannot reach upstream {url} — {exc}") from exc
    if offline:
        return
    # Idempotent, and it repairs a cache cloned before this was understood.
    _git(["config", "remote.origin.fetch", _MIRROR_REFSPEC], cwd=cache)
    try:
        _git(["fetch", "--quiet", "--prune", "--force", "origin"], cwd=cache)
    except SyncError as exc:
        raise SyncError(
            f"cannot refresh upstream cache from {url} (offline? VPN?) — {exc}"
        ) from exc


# --- state --------------------------------------------------------------------


def load_state(root: Path) -> dict:
    path = root / STATE_REL
    if not path.exists():
        raise SyncError(
            f"missing state file {path} — copy state.example.json beside it and fill in "
            "the upstream URL and the baseline commit this fork started from"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(root: Path, state: dict) -> None:
    path = root / STATE_REL
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def last_pass_date(root: Path) -> date | None:
    """Newest docs/upstream-syncs/YYYY-MM-DD.md, or None."""
    notes = root / "docs" / "upstream-syncs"
    if not notes.is_dir():
        return None
    found = []
    for note in notes.glob("*.md"):
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})\.md$", note.name)
        if m:
            try:
                found.append(date(int(m[1]), int(m[2]), int(m[3])))
            except ValueError:
                continue
    return max(found) if found else None


# --- the two reports ----------------------------------------------------------


def classify_inbound(root: Path, cache: Path, state: dict, tip: str) -> list[dict]:
    """One verdict per file touched between the baseline and the upstream tip."""
    base = state["baseline"]["commit"]
    matcher = PathMatcher(state["trackedPaths"], state["localizedPaths"])
    rows = []
    raw = _git(["diff", "--name-status", "-z", base, tip], cwd=cache)
    fields = [f for f in raw.split("\0") if f]
    i = 0
    while i < len(fields):
        status = fields[i]
        # Renames/copies carry two paths; treat the destination as the change.
        if status[0] in ("R", "C"):
            path = fields[i + 2]
            i += 3
        else:
            path = fields[i + 1]
            i += 2

        reason = matcher.local_reason(path)
        if reason is not None:
            # Say so when a refused path happens to already hold upstream's
            # content — "we fixed this ourselves" reads very differently from
            # "we are declining this", and the pass note must not blur them.
            ours_file = root / path
            if ours_file.is_file() and ours_file.read_bytes() == _blob(cache, tip, path):
                reason = f"{reason} [content already matches upstream]"
            rows.append({"path": path, "status": status, "verdict": LOCAL, "reason": reason})
            continue
        if not matcher.is_tracked(path):
            rows.append(
                {
                    "path": path,
                    "status": status,
                    "verdict": IGNORED,
                    "reason": "not template-owned",
                }
            )
            continue

        ours_file = root / path
        ours = ours_file.read_bytes() if ours_file.is_file() else None
        at_base = _blob(cache, base, path)
        at_tip = _blob(cache, tip, path)

        if ours == at_tip:
            verdict, reason = CONVERGED, "our copy already matches upstream"
        elif ours == at_base:
            verdict, reason = ADOPT, "we never touched it — upstream's version applies cleanly"
        else:
            verdict, reason = CONFLICT, "changed upstream and here — merge by hand"
        rows.append({"path": path, "status": status, "verdict": verdict, "reason": reason})
    return rows


def _ignored_paths(root: Path) -> set[str]:
    """Paths git ignores under root, or an empty set when root is not a git tree.

    The walk below reads the filesystem, and the filesystem holds things git was
    told to forget: private ledgers, local settings, caches. Offering one of those
    as a contribution to a public template is the kind of mistake that is only
    ever noticed after the fact — a gitignored private file did appear in this
    list once. So the walk asks git what it ignores and leaves those alone.
    """
    probe = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return set()
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        capture_output=True, text=True,
    )
    if listing.returncode != 0:
        return set()
    return {p for p in listing.stdout.split("\0") if p}


def _walk_tracked(root: Path, matcher: PathMatcher) -> list[str]:
    """Every file in our tree that a tracked glob claims and git does not ignore."""
    ignored = _ignored_paths(root)
    found = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in ignored:
            continue
        if any(rel == d or rel.startswith(d + "/") for d in SKIP_DIRS):
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if matcher.is_tracked(rel):
            found.append(rel)
    return sorted(found)


def classify_outbound(root: Path, cache: Path, state: dict, tip: str) -> list[dict]:
    """Template-owned files *we* changed — the contribution candidates.

    A file that differs from the tip only because upstream moved it (ours still
    equals the baseline) is inbound's problem, not a contribution; it is skipped
    here so the candidate list stays honest.
    """
    base = state["baseline"]["commit"]
    matcher = PathMatcher(state["trackedPaths"], state["localizedPaths"])
    rows = []
    for rel in _walk_tracked(root, matcher):
        if matcher.local_reason(rel) is not None:
            continue
        ours = (root / rel).read_bytes()
        at_tip = _blob(cache, tip, rel)
        at_base = _blob(cache, base, rel)
        if ours == at_tip:
            continue
        if at_tip is None:
            if ours == at_base:
                continue  # upstream deleted it — an inbound decision
            rows.append({"path": rel, "kind": "NEW", "reason": "exists here, not upstream"})
        elif ours == at_base:
            continue  # upstream changed it, we did not
        else:
            rows.append({"path": rel, "kind": "MODIFIED", "reason": "differs from upstream tip"})
    return rows


# --- presentation -------------------------------------------------------------


def _print_rows(rows: list[dict], key: str, empty: str) -> None:
    if not rows:
        print(f"  {empty}")
        return
    width = max(len(r[key]) for r in rows)
    for row in rows:
        print(f"  {row[key]:<{width}}  {row['path']}")
        if row.get("reason"):
            print(f"  {'':<{width}}  └─ {row['reason']}")


def cmd_status(root: Path, state: dict, tip: str | None, args) -> int:
    base = state["baseline"]
    last = last_pass_date(root)
    cadence = int(state.get("cadenceDays", 30))
    print(f"upstream : {state['upstream']['name']} <{state['upstream']['url']}> ref {state['upstream']['ref']}")
    print(f"baseline : {base['commit'][:9]}  ({base['date']}, adopted {base['adoptedOn']})")
    if tip:
        ahead = _git(["rev-list", "--count", f"{base['commit']}..{tip}"], cwd=root / CACHE_REL).strip()
        print(f"tip      : {tip[:9]}  ({ahead} commit(s) ahead of the baseline)")
    else:
        print("tip      : unknown (offline)")
    if last is None:
        print(f"last pass: never  (cadence {cadence}d — a pass is due)")
    else:
        age = (date.today() - last).days
        due = "due" if age >= cadence else f"next in {cadence - age}d"
        print(f"last pass: {last.isoformat()} ({age}d ago; cadence {cadence}d — {due})")
    return 0


def cmd_inbound(root: Path, state: dict, tip: str, args) -> int:
    cache = root / CACHE_REL
    base = state["baseline"]["commit"]
    commits = [
        line
        for line in _git(
            ["log", "--reverse", "--pretty=%h %ad %s", "--date=short", f"{base}..{tip}"],
            cwd=cache,
        ).splitlines()
        if line
    ]
    rows = classify_inbound(root, cache, state, tip)

    if args.json:
        print(json.dumps({"baseline": base, "tip": tip, "commits": commits, "files": rows}, indent=2))
        return 0

    print(f"inbound: {base[:9]} .. {tip[:9]} — {len(commits)} commit(s)")
    if not commits:
        print("  up to date — nothing upstream since the baseline")
        return 0
    for line in commits:
        print(f"  · {line}")
    print()
    print("files:")
    _print_rows(rows, "verdict", "(no files)")
    print()
    counts = {v: sum(1 for r in rows if r["verdict"] == v) for v in (ADOPT, CONFLICT, CONVERGED, LOCAL, IGNORED)}
    print(
        "summary: "
        + ", ".join(f"{v.lower()} {counts[v]}" for v in (ADOPT, CONFLICT, CONVERGED, LOCAL, IGNORED))
    )
    return 0


def cmd_outbound(root: Path, state: dict, tip: str, args) -> int:
    rows = classify_outbound(root, root / CACHE_REL, state, tip)
    if args.json:
        print(json.dumps({"tip": tip, "candidates": rows}, indent=2))
        return 0
    print(f"outbound: our tree vs upstream {tip[:9]} — {len(rows)} candidate(s)")
    print("(template-owned files we changed; triage for genuineness before contributing)")
    print()
    _print_rows(rows, "kind", "none — our copy matches upstream everywhere it is tracked")
    return 0


def cmd_diff(root: Path, state: dict, tip: str, args) -> int:
    """Print the unified diff between upstream's copy of a file and ours.

    The triage step this tool exists to support says "read the diff, do not
    assume". Until this subcommand existed there was no way to obey it from a
    worktree-isolated session, where reaching into the bare cache with a raw
    `git --git-dir` is refused — so the only available move was to guess, which
    is exactly what the protocol forbids.
    """
    cache = root / CACHE_REL
    matcher = PathMatcher(state["trackedPaths"], state.get("localizedPaths", []))
    failures = 0
    for rel in args.paths:
        # removeprefix, not lstrip: lstrip takes a character SET, so "./" would
        # also eat the leading dot of a path like .claude/hooks/x.py.
        rel = rel.removeprefix("./")
        if not matcher.is_tracked(rel):
            print(f"# {rel}: not template-owned (see trackedPaths) — nothing to compare", file=sys.stderr)
            failures += 1
            continue
        theirs = _blob(cache, tip, rel)
        ours_file = root / rel
        ours = ours_file.read_bytes() if ours_file.is_file() else None
        if theirs is None and ours is None:
            print(f"# {rel}: absent both here and upstream", file=sys.stderr)
            failures += 1
            continue
        lines = difflib.unified_diff(
            (theirs or b"").decode(errors="replace").splitlines(keepends=True),
            (ours or b"").decode(errors="replace").splitlines(keepends=True),
            fromfile=f"upstream/{rel}" + ("" if theirs is not None else " (absent)"),
            tofile=f"ours/{rel}" + ("" if ours is not None else " (absent)"),
        )
        body = "".join(lines)
        print(body if body else f"# {rel}: identical to upstream {tip[:9]}")
    return 1 if failures else 0


def cmd_set_baseline(root: Path, state: dict, tip: str | None, args) -> int:
    cache = root / CACHE_REL
    sha = _git(["rev-parse", args.sha], cwd=cache).strip()
    when = args.date or _git(["show", "-s", "--format=%ad", "--date=short", sha], cwd=cache).strip()
    old = state["baseline"]["commit"]
    state["baseline"] = {
        "commit": sha,
        "date": when,
        "adoptedOn": date.today().isoformat(),
        "note": args.note or f"Advanced from {old[:9]} by an upstream-sync pass.",
    }
    save_state(root, state)
    print(f"baseline: {old[:9]} -> {sha[:9]} ({when})")
    return 0


# --- entry point --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None, help="repo root (default: this script's repo)")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("status", "baseline, upstream tip, cadence"),
        ("inbound", "what upstream changed since the baseline"),
        ("outbound", "what we changed in template-owned files"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--json", action="store_true", help="machine-readable output")
        p.add_argument("--offline", action="store_true", help="use the cached clone, do not fetch")

    p = sub.add_parser("diff", help="unified diff of one tracked file, upstream vs ours")
    p.add_argument("paths", nargs="+", help="repo-relative path(s) of tracked file(s)")
    p.add_argument("--offline", action="store_true", help="use the cached clone, do not fetch")
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    p = sub.add_parser("set-baseline", help="record a new baseline commit (writes state.json)")
    p.add_argument("sha", help="upstream commit to record")
    p.add_argument("--date", default=None, help="commit date YYYY-MM-DD (default: read from the commit)")
    p.add_argument("--note", default=None, help="why this baseline was adopted")
    p.add_argument("--offline", action="store_true", help="use the cached clone, do not fetch")
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[2]

    try:
        state = load_state(root)
        cache = root / CACHE_REL
        refresh_cache(cache, state["upstream"]["url"], args.offline)
        tip = _git(["rev-parse", state["upstream"]["ref"]], cwd=cache).strip()
    except SyncError as exc:
        if args.command == "status":
            # status stays useful without upstream — report what we know.
            try:
                return cmd_status(root, load_state(root), None, args)
            except SyncError:
                pass
        print(f"[upstream-sync] {exc}", file=sys.stderr)
        return 2

    handlers = {
        "status": cmd_status,
        "inbound": cmd_inbound,
        "outbound": cmd_outbound,
        "diff": cmd_diff,
        "set-baseline": cmd_set_baseline,
    }
    try:
        return handlers[args.command](root, state, tip, args)
    except SyncError as exc:
        print(f"[upstream-sync] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
