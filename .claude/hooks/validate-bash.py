#!/usr/bin/env python3
"""PreToolUse hook: blocks dangerous bash commands.

Reads the tool payload from stdin (via hook_io — the shared wire contract),
returns a permission decision on stdout.
Blocks: rm -rf /, force pushes to main, dropping databases, etc.

Fails CLOSED at every stage: an unparsable payload, and any other fault while
deciding, blocks rather than waving the command through — this guard is the last
line in front of destructive commands.
"""

import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_io  # noqa: E402, I001  (the path shim above must run before this import)


# A force push to a protected branch can be spelled several ways:
#   --force / --force-with-lease / --force-if-includes       (long flag form)
#   -f, or a combined short cluster containing f (-fq, -qf)   (short flag form)
#   git push origin +main / +HEAD:main / +refs/heads/main    (leading-'+' refspec form)
# It may also carry global options between `git` and `push` (e.g. `git -C . push`),
# and the flag may sit before or after the branch. Match all of these.
# The short-flag alternative must start a token: the (?<![\w-]) lookbehind means
# it fires on ` -f` but never inside a long option, whether the dash follows
# another dash (--force, --follow-tags) or a word character (--body-file,
# gh's --dry-run). Matching mid-option produced false denials on commands that
# merely mentioned a protected branch elsewhere on the line.
_FORCE_FLAG = r"(?:--force(?:-with-lease|-if-includes)?|(?<![\w-])-[a-zA-Z]*f[a-zA-Z]*)\b"
_PUSH = r"\bgit\b[^\n]*?\bpush\b"
# The protected name must be the push DESTINATION, not merely a word on the line.
# A ref token is preceded by a separator (space, '/', ':', '+') and is not glued to
# further branch-name characters. So `main`, `+main`, `HEAD:main` and
# `refs/heads/main` match, while `feature/main-fix`, `feature/domain-main`,
# `release/master-cutover` and `mainline` do not — those are different branches, and
# denying a force push to them protected nothing while training people to route
# around the guard.
_PROTECTED = r"(?<=[\s/:+=\'\"])(?:main|master)(?![\w/-])"

# A shell segment that can only READ. This exists for ONE false positive: a SQL
# keyword appearing inside a search argument, where `git log --grep=<keyword>`
# was denied for reading history. The exemption applies to the keyword rules in
# _EXEMPTIBLE only — never to a root delete, a filesystem format, a device write
# or a fork bomb, because no search argument is worth trading those for a bypass.
# Deliberately strict: the first word must be one of these, `rg --pre` is out
# because it runs a preprocessor, and any substitution disqualifies the segment.
_READ_ONLY_HEADS = (
    ("grep",), ("egrep",), ("fgrep",), ("rg",), ("ag",), ("ack",),
    ("git", "log"), ("git", "grep"), ("git", "show"), ("git", "diff"), ("git", "blame"),
)
_SEGMENT_SPLIT = re.compile(r"(?:\|\||&&|[;|\n])")


def _segment_is_read_only(segment: str) -> bool:
    # A substitution runs its own command first, whatever the outer one is:
    # `git log -- "$(<destructive>)"` executes the inner command before git ever
    # starts. The same goes for backticks and process substitution. Nothing that
    # contains one can be called read-only by looking at its first words.
    if "$(" in segment or "`" in segment or "<(" in segment or ">(" in segment:
        return False
    words = segment.split()
    if not words:
        return True  # an empty segment performs nothing
    if any(w.startswith("--output") for w in words):
        return False  # git log/show/diff can write a file with --output=<path>
    if words[0] in ("rg",) and any(w.startswith("--pre") for w in words):
        return False  # rg --pre runs an external preprocessor
    for head in _READ_ONLY_HEADS:
        if tuple(words[: len(head)]) == head:
            return True
    return False


def _only_reads(command: str) -> bool:
    """True when EVERY segment of the command can only read.

    Segment-by-segment, so `grep x && psql -c '<destructive>'` is still judged:
    one read-only half does not excuse the other.
    """
    return all(_segment_is_read_only(s) for s in _SEGMENT_SPLIT.split(command))

# The only rules the read-only exemption may skip: keywords that a search argument
# can legitimately contain. Everything else in BLOCKED_PATTERNS is always checked.
_EXEMPTIBLE = ("DROP", "truncate")

# Where a throwaway script lives: the session scratchpad and the system temp
# directories it sits under, on both the real and the symlinked macOS spelling.
_TEMP_ROOT = r"(?:/private)?/tmp/|(?:/private)?/var/folders/"
# Nameable, unlike the set of dangerous APIs this rule replaced: leaving half the
# interpreters out would only move the same command one word over. `deno`/`bun`
# take a subcommand before the path; the rest take the path directly.
_INTERPRETER = (
    r"python[\d.]*|bash|sh|zsh|ksh|dash|csh|tcsh|fish|node|perl|ruby|php|lua"
    r"|tclsh|Rscript|pwsh|powershell|osascript|(?:deno|bun)(?:\s+run)?"
)
# A quote is a token boundary like any other — both before the interpreter (which
# is how `sh -c '<interpreter> <temp path>'` nests one inside another) and before
# the path (which is how `python3 "/tmp/x.py"` respells the blocked command). The
# first shape of this rule honoured neither, and a reviewer walked past it with a
# pair of quotation marks.
_TOKEN_START = r"(?:^|[\s;&|(/'\"])"

# Appended to every denial. See main() for why it lives at the exit and not in
# each message.
_NO_WORKAROUND = (
    "This block is a decision, not an obstacle. Do NOT re-spell the command, "
    "split it, encode it, or move it into a file and run that — the guard reads "
    "the text of a Bash call, so a script file is unaudited, and routing around "
    "a block is forbidden (AGENTS.md, DO NOT USE). Three honest responses: the "
    "command should not run; or the guard is wrong — then fix the guard, add the "
    "case to scripts/verify/verify_force_push_guard.py, and have the fix "
    "reviewed; or both are right and the step you need has no sanctioned tool "
    "yet — then build one, in committed code, and use that."
)

_FORCE_MAIN_REASON = (
    "Force push to main/master is blocked — rewriting published history is an "
    "owner-run exception (AGENTS.md). Push a task branch and open a merge request; "
    "if you rebased your own branch, `--force-with-lease` on THAT branch is fine."
)

BLOCKED_PATTERNS = [
    (r"rm\s+-rf\s+/(?!\S)",
     "Refusing to delete the filesystem root. If you meant a path inside the repo, "
     "name it explicitly; `git clean -fd` removes untracked files safely."),
    # flag form, either order (flag before or after the branch)
    # Deliberately per-line. A genuine flag/branch split is a backslash-newline
    # continuation, which decide() collapses before matching; letting `.` cross a
    # bare newline instead made a plain push deny because a LATER, unrelated line
    # happened to contain "-f" and "main".
    (rf"{_PUSH}[^\n]*{_FORCE_FLAG}[^\n]*{_PROTECTED}", _FORCE_MAIN_REASON),
    (rf"{_PUSH}[^\n]*{_PROTECTED}[^\n]*{_FORCE_FLAG}", _FORCE_MAIN_REASON),
    # leading-'+' refspec form (a force push that never says "force")
    (rf"{_PUSH}[^\n]*\+\S*{_PROTECTED}", _FORCE_MAIN_REASON),
    (r"git\s+reset\s+--hard\s+origin/(main|master)",
     "Hard reset onto origin main/master is blocked — it discards your branch's work "
     "silently. To take new main: `git rebase origin/main` on your branch."),
    (r"DROP\s+(DATABASE|TABLE)",
     "Dropping a database or table is blocked. Schema changes belong in a Django "
     "migration, reviewed and applied by the deploy, never in an ad-hoc statement."),
    (r"truncate\s+table",
     "Emptying a table is blocked. If test data needs resetting, do it through a "
     "management command or a migration, not a direct statement."),
    # `()` and `{}` were unescaped here for as long as the rule existed — in
    # Python `re` that is an empty group and a quantifier, so the one string this
    # names could never match it. Found by the review of this change: a rule that
    # reads as protection and matches nothing is worse than no rule, because it
    # stops anyone from writing the real one.
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
     "Fork bomb detected. Nothing in this project's work needs one."),
    (r"mkfs\.", "Formatting a filesystem is blocked. Nothing in this project's work needs it."),
    (r"> /dev/sd[a-z]", "Writing straight to a block device is blocked."),
    # Running a script out of a temp directory is how work leaves this guard's
    # sight entirely: it reads the text of a Bash call, and a file's contents are
    # not in it. Unlike a list of dangerous API names — which Python can always
    # spell another way — this rule is finite, because it is about where the code
    # CAME FROM, not what it does. The only way around it is to put the script in
    # the repository, which is the outcome we want: there it is reviewed.
    (rf"{_TOKEN_START}(?:{_INTERPRETER})\s+(?:-\S+\s+)*['\"]?(?:{_TEMP_ROOT})\S+",
     "Running a script from a temp directory is blocked — the guard cannot read the file, "
     "so the command is unaudited. Put it under scripts/ where it is reviewed, or inline the code."),
]

ASK_PATTERNS = [
    (r"git\s+push", "About to push to remote — confirm?"),
    (r"rm\s+-rf", "Recursive delete — confirm target?"),
    (r"git\s+reset\s+--hard", "Hard reset will discard changes — confirm?"),
    (r"pip\s+install(?!.*-r\s+requirements)", "Installing package outside requirements.txt — confirm?"),
    # Whatever the left side produced is executed unread. The classic remote-code
    # shape (`curl <url> | sh`) passed silently until this was added.
    (r"\|\s*(?:sudo\s+)?(?:ba|z|k|da)?sh\b",
     "Piping output straight into a shell — it runs unread. Confirm the source?"),
]

# `git commit` is branch-aware, not a static pattern: committing on a task
# branch is the normal loop and must stay silent, while committing on a
# protected branch is how a Bash-mediated write (echo / sed / python -c)
# lands on the integration branch past the Edit/Write guard — reproduced by
# review, 2026-07-29: `printf X > src/f.py && git add -A && git commit` on a
# main checkout passed with no ask. ASK, not deny: a human does the merging, and an
# operator working in a main checkout can still confirm a deliberate act rather
# than be refused outright. (This used to cite reconciling the activity log as the
# example; that exemption is gone — `hook_io.ALLOWED_ON_MAIN` is empty as of
# 2026-09-16 and says why — so the ask now covers a human's judgement call, not a
# standing exception.) Residual: shell writes with no `git commit` in the same
# command (or `cd`/`-C` indirection) are not mediatable here — but they no longer
# pass unseen either: `post-bash-main-clean.py` reports them after the fact, and
# the forge's protected branch, with merging only through review, remains the backstop.
_COMMIT = re.compile(r"\bgit\b[^\n|;&]*?\bcommit\b")
PROTECTED_BRANCHES = {"main", "master"}

# `git checkout -- <paths>` and `git restore <paths>` overwrite the working copy from
# the index or a commit. Uncommitted work in those files is gone, with no reflog entry
# and nothing to recover from — the one destructive git operation with no undo.
#
# Written after losing a file to it: a mutation test had gutted a hook, and restoring it
# with `git checkout -- <file>` also discarded the uncommitted review fixes sitting in the
# same file, because the command restores the COMMITTED version, not the pre-mutation one.
# The work was reconstructible from context that time. It usually would not be.
#
# Branch-aware like the commit guard above, and for the same reason: as a static pattern
# this would ask on every restore, most of which discard nothing, and an ask that fires
# constantly is an ask nobody reads. So it asks only when those exact paths actually carry
# uncommitted changes, and names them.
_CHECKOUT_OR_RESTORE = re.compile(r"\bgit\b(?:\s+-C\s+\S+)?\s+(checkout|restore)\b([^\n|;&]*)")


def _restore_targets(command: str) -> list[str] | None:
    """Paths a `git checkout --`/`git restore` in `command` would overwrite, if any.

    Returns None when the command is not the path-overwriting form: a plain
    `git checkout <branch>` (no `--`) changes branches and keeps the working tree, and
    `git restore --staged` only unstages — the worktree copy survives either way.
    """
    match = _CHECKOUT_OR_RESTORE.search(command)
    if match is None:
        return None
    subcommand, rest = match.group(1), match.group(2)
    try:
        args = shlex.split(rest)
    except ValueError:
        return None  # unbalanced quotes: not ours to interpret
    if subcommand == "checkout":
        if "--" not in args:
            return None  # branch switch, not a path overwrite
        paths = args[args.index("--") + 1:]
    else:
        if "--staged" in args and "--worktree" not in args:
            return None  # unstages only; the working copy is untouched
        paths = [a for a in args if not a.startswith("-")]
    return paths or None


def dirty_among(cwd: str, paths: list[str]) -> list[str]:
    """Which of `paths` carry uncommitted changes right now (empty if none or unknown)."""
    try:
        probe = subprocess.run(
            ["git", "status", "--porcelain", "--", *paths],
            capture_output=True, text=True, cwd=cwd or None, timeout=5,
            env=hook_io.git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if probe.returncode != 0:
        return []
    found = []
    for line in probe.stdout.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:].strip().strip('"')
        if entry:
            found.append(entry)
    return found


def repo_branch(cwd: str) -> str | None:
    """Branch of the repo at `cwd`, git's location vars stripped. None if unknowable."""
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=cwd or None, timeout=5,
            env=hook_io.git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0:
        return None
    return probe.stdout.strip()


def decide(command: str):
    """("deny"|"ask", reason) for a dangerous command, or None to stay silent."""
    # Shell joins backslash-newline continuations into one logical line; collapse
    # them so a wrapped command can't slip a force flag past a per-line matcher.
    command = re.sub(r"\\\r?\n", " ", command)

    # A command that can only read executes none of the acts the rules below
    # describe. Checked per segment, so a read-only half never excuses the other.
    reads_only = _only_reads(command)

    for pattern, reason in BLOCKED_PATTERNS:
        if reads_only and pattern.startswith(_EXEMPTIBLE):
            continue  # a keyword inside a search argument, not an act
        if re.search(pattern, command, re.IGNORECASE):
            return "deny", reason

    for pattern, reason in ASK_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return "ask", reason

    return None


def main():
    event = hook_io.require_event("validate-bash")

    if event.tool_name != "Bash":
        return

    command = event.command
    if not command:
        return

    result = decide(command)
    if result is not None:
        decision, reason = result
        # A denial is the one message the agent is certain to read, so it carries
        # the rule as well as the refusal. Without this the agent knows only that
        # something failed, and the obvious next move — re-spell it, split it,
        # write it to a file and run that — is the one the harness forbids and
        # cannot see. Telling it what to do instead is cheaper than catching it
        # afterwards. Appended at the single exit, so no rule can ship without it.
        if decision == "deny":
            reason = f"{reason}\n\n{_NO_WORKAROUND}"
        hook_io.permission(decision, reason)

    if _COMMIT.search(command):
        branch = repo_branch(event.raw.get("cwd") or "")
        if branch in PROTECTED_BRANCHES:
            hook_io.permission(
                "ask",
                f"`git commit` while this checkout is on '{branch}' — agents do not "
                "commit to the integration branch (a human does the merging). Boot a task "
                "worktree instead, or confirm if this is sanctioned bookkeeping.",
            )

    targets = _restore_targets(command)
    if targets:
        losing = dirty_among(event.raw.get("cwd") or "", targets)
        if losing:
            shown = ", ".join(losing[:5]) + (f" (+{len(losing) - 5} more)" if len(losing) > 5 else "")
            hook_io.permission(
                "ask",
                f"This overwrites {len(losing)} file(s) that have uncommitted changes: "
                f"{shown}. `git checkout --` / `git restore` restores the COMMITTED "
                "version — the uncommitted work in those files is discarded with no "
                "reflog entry and nothing to recover from. If you are undoing a "
                "temporary edit, make sure the file holds nothing else you still need; "
                "a copy taken before the edit is the safe way to undo one. Confirm?",
            )


if __name__ == "__main__":
    # A guard must never fail OPEN. An unexpected payload shape used to raise
    # here, and a traceback exits 1 — which Claude Code does not treat as a
    # block, so the call went through unexamined. SystemExit (raised by
    # hook_io.block/permission) is not an Exception, so a legitimate decision
    # still propagates; only genuine faults land here.
    try:
        main()
    except Exception as exc:
        # The teaching text belongs here too: a guard that crashed is the moment
        # the temptation to "just run it another way" is strongest, and the right
        # move — fix the guard — is the same one it names.
        hook_io.block(
            f"validate-bash: internal error — blocking to fail closed: {exc!r}"
            f"\n\n{_NO_WORKAROUND}"
        )
