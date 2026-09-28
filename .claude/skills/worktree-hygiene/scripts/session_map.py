#!/usr/bin/env python3
"""session_map.py — link Claude sessions to the repository work they produced.

Claude stores one directory per working directory under ~/.claude/projects/, named by
the slugified absolute path, holding one <uuid>.jsonl per session. Nothing in git points
at them, so a worktree cleanup can strand a session without any warning. This reads them
back and answers three questions:

    sessions            what sessions exist, what are they called, where do they point
    broken              which sessions record a working directory that no longer exists
    branch <name>       which session most likely produced this branch, and why

Read-only. It never writes to a transcript.

Usage:
    python session_map.py sessions [--repo PATH]
    python session_map.py broken   [--repo PATH]
    python session_map.py branch task/foo [--repo PATH]
    python session_map.py branches [--repo PATH]      # every local branch at once
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys

PROJECTS = os.path.expanduser("~/.claude/projects")

# Parsing whole lines as JSON is correct but slow on a 100 MB transcript, so lines are
# pre-filtered on a cheap substring test first.
#
# `title"` matches the TYPE TAG, not the field name: the tags are `"type":"custom-title"`
# and `"type":"ai-title"`, both of which end in `title"`. The fields themselves are spelled
# `customTitle` and `aiTitle` (capital T) and would NOT match this entry. Keep the tag form:
# it is what actually admits the line.
_WANTED = ('"cwd"', '"gitBranch"', 'title"', '"timestamp"', '"type":"user"')
_TS = re.compile(r'"timestamp":"(\d{4}-\d{2}-\d{2}T\d{2})')

# A resumed session opens with a machine-written recap, and a local command injects a
# caveat block. Neither is something a person typed.
_NOT_A_PROMPT = ("This session is being continued", "Caveat:", "<")

# Title sources, best first. A person's own name beats a generated one, which beats the
# first thing they typed. Kept as separate fields rather than one collapsed string so two
# files of the same session can be merged by priority instead of by processing order.
_TITLE_FIELDS = ("custom", "ai", "first_prompt")


def _text_of(content: object) -> str | None:
    """The human-readable text of a message, whatever shape `content` arrives in.

    Nearly every user record stores `content` as a list of typed blocks; a plain string is
    the rare case. Handling only the string silently loses the first-message fallback for
    almost every session, which shows up as "(untitled)" rather than as an error.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    return text
    return None


class Session:
    """One Claude session, read out of one transcript file."""

    __slots__ = ("sid", "path", "mtime", "size", "cwds", "last_cwd", "last_cwd_at",
                 "branches", "last_branch", "last_branch_at", "hours",
                 "custom", "ai", "first_prompt")

    def __init__(self, path: str) -> None:
        self.path = path
        self.sid = os.path.basename(path)[:-6]
        self.mtime = dt.datetime.fromtimestamp(os.path.getmtime(path))
        self.size = os.path.getsize(path)
        self.cwds: set[str] = set()
        self.last_cwd = ""
        self.last_cwd_at = ""
        self.branches: collections.Counter[str] = collections.Counter()
        self.last_branch = ""
        self.last_branch_at = ""
        self.hours: set[str] = set()
        self.custom: str | None = None
        self.ai: str | None = None
        self.first_prompt: str | None = None

        with open(path, errors="replace") as fh:
            for line in fh:
                stamp = _TS.search(line)
                if stamp:
                    self.hours.add(stamp.group(1))
                if not any(k in line for k in _WANTED):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                self._absorb_record(rec)

    def _absorb_record(self, rec: dict) -> None:
        kind = rec.get("type")
        when = rec.get("timestamp") or ""

        if kind == "custom-title":
            self.custom = rec.get("customTitle") or self.custom
        elif kind == "ai-title":
            # The field is `aiTitle`; `title` is a fallback only. Reading `title` alone
            # finds nothing and reports every session as untitled.
            self.ai = rec.get("aiTitle") or rec.get("title") or self.ai
        elif self.first_prompt is None and kind == "user":
            msg = rec.get("message")
            if isinstance(msg, dict):
                body = _text_of(msg.get("content"))
                if body and body.strip() and not body.startswith(_NOT_A_PROMPT):
                    self.first_prompt = " ".join(body.split())[:70]

        cwd = rec.get("cwd")
        if isinstance(cwd, str) and cwd:
            self.cwds.add(cwd)
            if when >= self.last_cwd_at:
                self.last_cwd, self.last_cwd_at = cwd, when
        branch = rec.get("gitBranch")
        if isinstance(branch, str) and branch and branch != "HEAD":
            self.branches[branch] += 1
            if when >= self.last_branch_at:
                self.last_branch, self.last_branch_at = branch, when

    @property
    def title(self) -> str:
        return self.custom or self.ai or self.first_prompt or "(untitled)"

    def absorb(self, other: Session) -> None:
        """Fold another file of the same session into this one.

        Merged by title priority and by record timestamp — never by which file happened to
        be read first. The processing order is not arbitrary: a repository's root project
        directory always sorts before its worktree directories, so an order-dependent merge
        would discard the same side every time, which is exactly the loss it claims to fix.
        """
        self.cwds |= other.cwds
        self.hours |= other.hours
        self.branches.update(other.branches)
        for field in _TITLE_FIELDS:
            if getattr(self, field) is None:
                setattr(self, field, getattr(other, field))
        if other.last_cwd and other.last_cwd_at >= self.last_cwd_at:
            self.last_cwd, self.last_cwd_at = other.last_cwd, other.last_cwd_at
        if other.last_branch and other.last_branch_at >= self.last_branch_at:
            self.last_branch, self.last_branch_at = other.last_branch, other.last_branch_at
        self.mtime = max(self.mtime, other.mtime)
        self.size += other.size


def repo_root(start: str) -> str:
    """The repository's top-level path — in a bare-top-level layout, the bare directory.

    Whatever git reports first is the anchor every project-directory slug is derived from,
    which is all this needs; it is not necessarily a checkout you can read files from.
    """
    out = subprocess.run(["git", "worktree", "list", "--porcelain"],
                         cwd=start, capture_output=True, text=True)
    for line in out.stdout.splitlines():
        if line.startswith("worktree "):
            return line.split(" ", 1)[1]
    return os.path.abspath(start)


def project_dirs(repo: str) -> list[str]:
    """Every session directory belonging to this repository, root and worktrees alike.

    Matched on an exact slug plus the worktree suffix, never a bare prefix: a sibling
    project whose name merely extends this one's would otherwise have its sessions folded
    into the report with no error.
    """
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(repo))
    base = slug.split("--claude-worktrees-")[0]
    found = glob.glob(base) + glob.glob(os.path.join(PROJECTS, base))
    found += glob.glob(os.path.join(PROJECTS, base + "--claude-worktrees-*"))
    return sorted({d for d in found if os.path.isdir(d)})


def load(repo: str) -> list[Session]:
    """Every session of this repository, newest first, one entry per session id."""
    files = [f for d in project_dirs(repo) for f in glob.glob(os.path.join(d, "*.jsonl"))]
    if not files:
        sys.exit(f"no session transcripts found under {PROJECTS} for {repo}")
    merged: dict[str, Session] = {}
    for path in files:
        session = Session(path)
        # One session id can span two project directories: renaming a session writes a
        # tiny custom-title stub wherever it is renamed from, while the transcript stays
        # where the work happened. Neither file alone is the session.
        if session.sid in merged:
            session.absorb(merged[session.sid])
        merged[session.sid] = session
    return sorted(merged.values(), key=lambda s: s.mtime, reverse=True)


def short(path: str, repo: str) -> str:
    """A directory shortened for display, relative to the repository."""
    rel = path.replace(repo, "") or "(root)"
    return rel.replace("/.claude/worktrees/", "wt:") or "(root)"


def cmd_sessions(sessions: list[Session], repo: str) -> None:
    """Print every session: when, how big, where it ran, and what it is called."""
    print(f"{'DATE':<12}{'MB':>6}  {'DIRECTORY':<28}{'':<6}{'ENDED ON':<30}{'ID':<10}TITLE")
    for s in sessions:
        ok = "ok" if s.last_cwd and os.path.isdir(s.last_cwd) else "GONE"
        print(f"{s.mtime:%Y-%m-%d}  {s.size / 1e6:>6.1f}  {short(s.last_cwd, repo)[:27]:<28}"
              f"{ok:<6}{s.last_branch[:29]:<30}{s.sid[:8]:<10}{s.title}")


def cmd_broken(sessions: list[Session], repo: str) -> int:
    """Print the sessions whose recorded directory is gone. Returns how many."""
    bad = [s for s in sessions if s.last_cwd and not os.path.isdir(s.last_cwd)]
    if not bad:
        print("every session points at a directory that still exists")
        return 0
    print("These sessions cannot be resumed: the directory they record is gone.")
    print("The editor will blame the native binary; the cause is the missing directory.\n")
    for s in bad:
        print(f"  {s.sid[:8]}  {s.mtime:%Y-%m-%d}  {s.title}")
        print(f"            wants: {s.last_cwd}")
    return len(bad)


def candidates(sessions: list[Session], repo: str, branch: str) -> None:
    """Print the sessions that may have produced `branch`, strongest evidence first."""
    print(f"\n── {branch}")
    git = ["git", "-C", repo]
    instants = subprocess.run(git + ["log", "--format=%cI", f"origin/main..{branch}"],
                              capture_output=True, text=True).stdout.split()
    if not instants:
        print("     no commits of its own against origin/main")
        return

    # 1. by working directory — exact, but only when the work happened inside the worktree
    listing = subprocess.run(git + ["worktree", "list", "--porcelain"],
                             capture_output=True, text=True).stdout
    folder = current = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            current = line.split(" ", 1)[1]
        elif line == f"branch refs/heads/{branch}":
            folder = current
    if folder:
        for s in sessions:
            if folder in s.cwds:
                print(f"     [directory] {s.sid[:8]}  {s.title}")

    # 2. by the branch appearing as the session's own checked-out branch
    for s in sessions:
        if s.branches.get(branch):
            print(f"     [git branch] {s.sid[:8]}  {s.branches[branch]:>5} records  {s.title}")

    # 3. by timing — ranks, never proves; parallel sessions overlap
    score: collections.Counter[str] = collections.Counter()
    for iso in instants:
        when = dt.datetime.fromisoformat(iso).astimezone(dt.UTC)
        for delta in (-1, 0, 1):
            key = (when + dt.timedelta(hours=delta)).strftime("%Y-%m-%dT%H")
            for s in sessions:
                if key in s.hours:
                    score[s.sid] += 1
    by_id = {s.sid: s for s in sessions}
    for sid, n in score.most_common(3):
        print(f"     [timing]     {by_id[sid].sid[:8]}  overlap {n:>2}  {by_id[sid].title}")
    print("     commits at: " + ", ".join(
        dt.datetime.fromisoformat(i).astimezone(dt.UTC).strftime("%m-%d %H:%MZ")
        for i in instants[:3]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["sessions", "broken", "branch", "branches"])
    ap.add_argument("name", nargs="?", help="branch name, for the `branch` command")
    ap.add_argument("--repo", default=".", help="any path inside the repository (default: cwd)")
    args = ap.parse_args()

    repo = repo_root(args.repo)
    sessions = load(repo)

    if args.command == "sessions":
        cmd_sessions(sessions, repo)
    elif args.command == "broken":
        return 1 if cmd_broken(sessions, repo) else 0
    elif args.command == "branch":
        if not args.name:
            ap.error("`branch` needs a branch name")
        candidates(sessions, repo, args.name)
    else:
        out = subprocess.run(["git", "-C", repo, "for-each-ref",
                              "--format=%(refname:short)", "refs/heads"],
                             capture_output=True, text=True).stdout.split()
        for branch in out:
            if branch != "main":
                candidates(sessions, repo, branch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
