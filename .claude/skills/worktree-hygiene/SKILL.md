---
name: worktree-hygiene
description: Retire finished git worktrees and branches without destroying anything that lives outside git — Claude session transcripts, gitignored companion files, another session's live checkout. Also finds which session produced a branch and what that session is called in the editor's session list. Use when asked to clean up worktrees or branches, when deciding whether a branch is stale, or when a resumed session fails to launch.
---

# Worktree and session hygiene

A worktree directory holds three kinds of thing. Git knows about one of them.

1. **Committed work** — recoverable from the repository, so deleting the folder costs nothing.
2. **Gitignored companions** — translations, `.env`, notes. Deleting the folder destroys the only copy.
3. **State outside the repository entirely** — the Claude session transcripts whose recorded
   working directory *is* that folder. Deleting the folder makes those sessions unresumable.

The whole skill is about the second and third. Git's own safety checks cover neither.

## Part 1 — Decide what is actually stale

"Stale" is not "old" and not "behind main". A branch is stale when **its content is already on
`main` by some other route**, or when what it builds has been superseded. Measure, do not guess.

    # every branch: how far ahead, is it on the server, which worktree holds it
    git for-each-ref --format='%(refname:short)' refs/heads | while read -r b; do
      ahead=$(git rev-list --count origin/main.."$b")
      remote=$(git rev-parse --verify -q "origin/$b" >/dev/null && echo yes || echo no)
      echo "$b ahead=$ahead on-server=$remote"
    done

`ahead=0` means every commit is reachable from `origin/main` — merged, safe. But a branch merged
by **squash or rebase** keeps `ahead>0` forever while its content is fully landed, so never stop at
that number. The decisive test compares only the files the branch itself wrote:

    mb=$(git merge-base origin/main "$b")
    git diff --name-only -z "$mb" "$b" |
      xargs -0 git diff --stat origin/main "$b" --      # empty output = fully landed

Comparing whole trees instead (`git diff origin/main "$b"`) reports everything `main` gained
since the branch forked, which looks like a huge difference and means nothing.

**This check fails into green, so get the plumbing right.** "No output" is the answer that means
"safe to delete", and every way of getting the file list wrong also produces no output:

- **zsh does not word-split an unquoted `$files`.** `git diff … -- $files` passes the whole list as
  one argument, it matches no path, the diff is empty, and the branch is pronounced landed. bash
  splits and works; the same line is wrong in the shell this repository is driven from.
- **`--pathspec-from-file` is not accepted by `git diff`** on every version — it prints usage to
  stderr and exits, which again leaves you holding an empty string.
- **`xargs -d` is GNU-only** and fails on BSD/macOS. Pair `git diff --name-only -z` with `xargs -0`;
  that form is portable and also survives a path containing a space.

Either drive it from a language that passes an argument list (`subprocess.run(["git", …, *files])`)
and treat a non-zero exit as fatal, or check a single known path by hand before believing a sweep:
`git diff --stat origin/main "$b" -- one/file/the/branch/wrote`. A file the branch added must show
as added; if it does not, the check is broken, not the branch.

Two more signals worth printing per branch, because they separate "stale" from "just old":

- **A file the branch touches that is now GONE from `main`** — the plan was retired to `done/`, or
  a directory moved. Gone-because-retired means landed; gone-because-moved means a heavy rebase.
- **The branch's own claims about blockers.** A branch that says "blocked on a credential nobody
  has" may be describing a world that no longer exists. Grep `main` for the blocker before trusting
  the branch's own status.

## Part 2 — Pre-flight before deleting a folder

Run all four. Each one has caught a real loss.

**a. Who is live in it?** A worktree touched in the last few hours belongs to a running session.
Leave it alone regardless of merge state.

    find "$WT" -type f -not -path '*/.venv/*' -not -path '*/.git/*' \
      -exec stat -f '%m %N' {} + | sort -rn | head -1

**b. What gitignored files are unique to it?** Hash every candidate across all worktrees; anything
whose hash appears in exactly one place, and whose English original still exists on `main`, must be
copied out first — next to that original, at `main`'s *current* path, since directories move.

    find "$BASE" -name '*.ru.md' -not -path '*/.venv/*' -exec md5 -q {} \; -print | paste - - | sort

**c. Which sessions record it as their working directory?**

    grep -l "worktrees/$WT\"" ~/.claude/projects/*<Project>*/*.jsonl | wc -l

Non-zero means deleting the folder breaks that many sessions. See Part 4 before proceeding.

**d. Is it even a registered worktree?** `git worktree list` is the authority. A directory under
`.claude/worktrees/` that git does not list is an orphan — its `.git` file points at an admin
directory that no longer exists. `git worktree remove` cannot touch it, and `rm -rf` on directories
is forbidden by `AGENTS.md`. Report it and let the owner remove it.

## Part 3 — Remove safely

    git worktree remove "$BASE/$WT"   # refuses on modifications or untracked files; never add --force
    git branch -d "$b"                # refuses if unmerged; -D only after the Part 1 file-level test
    git worktree prune

**To delete a branch whose worktree must survive** — because a session still points at that folder —
detach the worktree instead of removing it. The files do not change and the branch name is freed:

    before=$(git -C "$BASE/$WT" rev-parse HEAD)
    git -C "$BASE/$WT" switch --detach
    [ "$before" = "$(git -C "$BASE/$WT" rev-parse HEAD)" ] || echo "WARNING: commit moved"
    git branch -D "$b"

Afterwards, tidy the emptied session directories with `rmdir` (which refuses a non-empty directory,
so it cannot destroy anything). Remove a stray `.DS_Store` by name first — never with a recursive
delete.

## Part 4 — The trap: a session remembers a folder that no longer exists

Moving a transcript into the root project directory is **not enough**. Each session records a `cwd`
in its own entries. When it is resumed, the process is spawned in that directory; if the directory
is gone the spawn fails, and the editor reports it as something else entirely:

> Claude Code native binary … exists but failed to launch. This usually means the binary does not
> match this system's libc …

That message is about the binary and the real cause is the missing directory. Confirm it by reading
the recorded paths out of the transcript:

    grep -o '"cwd":"[^"]*"' session.jsonl | sed 's/"cwd":"//;s/"$//' | sort -u | while read -r c; do
      [ -d "$c" ] && echo "ok   $c" || echo "GONE $c"
    done

So: **decide the folder's fate before moving the transcripts.** If one is already gone, the repair
is to **put the directory back, not to edit the transcript**:

    git worktree add "$BASE/$WT" <the branch that session should finish>
    # or, when its branch is merged and deleted:
    git worktree add --detach "$BASE/$WT" origin/main
    ln -sfn "$REPO/.env" "$BASE/$WT/.env"

This changes nothing the person owns, it is reversible with `git worktree remove`, and the session
reopens where it left off. Checking out the branch it still has to finish is better than a bare
checkout: the session resumes directly into its own work. Rewriting a recorded path inside a
transcript is the last resort, not the first — it edits the owner's history, so it needs their
explicit consent and a backup.

A session usually records several directories; restore every one it references, not just the last.

## Part 5 — Which session produced this branch, and what is it called?

`scripts/session_map.py` in this skill does all of the below; the manual recipes are here because
the script cannot cover a repository it was not pointed at.

**Session storage.** One directory per working directory, under `~/.claude/projects/`, named by the
slugified absolute path. A worktree therefore gets its own directory, separate from the repository
root's. One `<uuid>.jsonl` per session, plus a `<uuid>/` sidecar directory that must move with it.

**The display name** — what the editor's session list shows — is, in order of preference:

    {"type":"custom-title", …, "customTitle":"…"}   the name a person set (authoritative)
    {"type":"ai-title",     …, "aiTitle":"…"}       the generated name    (what you usually see)
    the first user message                          fallback when neither exists

**A user message's `content` is normally a list of typed blocks, not a string.** Measured on this
machine: 1,873 list-shaped against 19 string-shaped. Read the `text` of the first `{"type":"text"}`
block; handling only the string case loses the fallback for almost every session and reports them
all as untitled — which reads as "these were never named" rather than as a bug, so nobody checks.
The rare string case is usually an injected caveat, not a person's words.

**Read `aiTitle`, not `title`.** The `ai-title` record spells its field `aiTitle`; reading `title`
finds nothing and reports every session as untitled — the failure looks like "these sessions were
never named" rather than a bug. Check one file by hand before trusting a sweep:

    grep -o '"type":"ai-title"[^}]*}' session.jsonl | tail -1

Two more traps when sweeping many files. **A session id can appear in two project directories** —
renaming a session writes a small `custom-title` stub where it was renamed from, while the transcript
stays where the work happened. Combine them by *title priority and record timestamp*, never by which
file you read first: a repository's root project directory always sorts before its worktree
directories, so an order-dependent merge discards the same side every time — in practice the human's
rename. Keeping the larger file loses it too. And **a resumed session's first user message is a machine-written recap** beginning "This
session is being continued from a previous conversation" — skip it or every resumed session is
titled with the same sentence.

**Three ways to link a branch to a session, strongest first.**

1. **By working directory.** Exact when the branch was worked on inside its own worktree:
   a session recording `"cwd":".../worktrees/<name>"` is the one. Useless for a branch created from
   the root checkout with `git -C`, which is how most agent sessions operate.
2. **By title.** Often decisive on its own: a branch named `task/prod-deploy` and a session titled
   "production-deploy track reconciliation and M4 runbook" are the same work. Cheap, so try it early.
3. **By commit timing.** Take each commit instant unique to the branch (`git log --format=%cI
   origin/main..<branch>`) and find sessions holding a `"timestamp"` in the same hour. Ranks
   candidates rather than proving one, because parallel sessions overlap — use it to shortlist, then
   confirm with the title.

**To find the session behind a review request, do not search for its number at all.** A session
records the pull or merge requests it opened as their own entries:

    grep -h '"type":"pr-link"' ~/.claude/projects/*<Project>*/*.jsonl |
      grep -oE '(pull|merge_requests)/[0-9]+'

That is exact. Counting mentions of `#60` or `!60` is not: an audit that merely lists open requests
outscores the session that opened them, and a bare number also matches inside longer ones. The
record only exists for a request opened from inside a session — one created by pushing a branch
with the forge's push options, or from the web interface, leaves no `pr-link`, so an empty answer
means "not opened from a session here", never "no such request".

**Do not rank by how often the branch name is mentioned.** Any session that listed branches — an
audit, a status report — outscores the session that actually built it.

## Report honestly

Say which links are confirmed and which are inferred from timing alone. Say plainly when a deletion
you performed is what broke something. Both were asked for directly, and a tidy summary that hides
either is worse than no cleanup.
