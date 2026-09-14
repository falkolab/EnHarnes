---
name: harness-upstream
description: Recurring two-way reconciliation between this harness and the template it was forked from — take upstream fixes in, offer generic local fixes back as a pull request. Invoke with /harness-upstream, or when the SessionStart due-check flags a pass is overdue. Monthly cadence; commits locally, never pushes to origin.
---

# harness-upstream

This harness is a fork of a template repository; the upstream name and URL live
in `.claude/skills/harness.upstream/state.json`. Forks rot in both directions:
upstream ships fixes we never see, and we fix template-owned code upstream still
has broken. This role is the recurring pass that stops both.

It is not a merge. Nothing is taken or sent automatically — the tool classifies,
a human or agent decides, and every decision lands in a dated note.

## Cadence & trigger

- **Cadence:** monthly (30 days). Upstream lands a handful of commits a year; a
  weekly nag against a slow template is noise, and noise gets ignored.
- **Trigger (hybrid):**
  - **SessionStart hook** `.claude/hooks/upstream-sync-due.py` reads the newest
    note in `docs/upstream-syncs/`. If the last pass is ≥ `cadenceDays` old (or
    there is none), it injects a reminder. It only reminds; it never runs a pass.
  - **On demand:** `/harness-upstream` runs a pass at any time.

Why a hook and a command rather than a scheduler: an in-session scheduler dies
with the session, and a cloud routine cannot reach a repository that is not
publicly reachable — a self-hosted forge, or one behind a VPN. A committed hook
plus a slash command is durable, local, and makes no surprise commits.

## The tool

`scripts/harness/upstream_sync.py` keeps a gitignored bare clone of upstream
under `.harness-cache/upstream.git` and reads
`.claude/skills/harness.upstream/state.json` for the upstream URL, the baseline
commit, the tracked-path globs and the deliberate-divergence list.

    python scripts/harness/upstream_sync.py status
    python scripts/harness/upstream_sync.py inbound   [--json] [--offline]
    python scripts/harness/upstream_sync.py outbound  [--json] [--offline]
    python scripts/harness/upstream_sync.py set-baseline <sha> [--note "..."]

`make upstream-check` runs the first three together. Only `set-baseline` writes.

Inbound verdicts, per file:

| Verdict | Means | Action |
|---|---|---|
| `ADOPT` | Upstream changed it; our copy still matches the baseline. | Copy upstream's version in. |
| `CONVERGED` | Upstream changed it; our copy already matches the tip. | Nothing — we fixed it independently. |
| `CONFLICT` | Upstream changed it and so did we, differently. | Merge by hand; keep our intent. |
| `LOCAL` | Path is on the deliberate-divergence list. | Never take it. Record what we declined and why. |
| `IGNORED` | Path is not template-owned. | Out of scope. |

Outbound kinds: `MODIFIED` (a template-owned file we changed) and `NEW` (a file
only we have). Files that differ only because *upstream* moved are excluded —
those are inbound's problem, not contributions.

## The pass — step by step

**1. Orient.** Run `make upstream-check`. Read the previous note in
`docs/upstream-syncs/` so the pass continues rather than restarts. If the working
tree is dirty from another session, boot a worktree first.

**2. Inbound — decide every file, not just the easy ones.**
- Apply the `ADOPT` set by copying upstream's version in. Then run `make lint`.
  An adopted file that breaks a gate is a `CONFLICT`, not an `ADOPT` — reclassify
  it and say so in the note.
- Work the `CONFLICT` set by hand. Preserve our intent; take upstream's
  improvement on top of it. Never resolve a conflict by discarding a local fix
  that a linter or verify script depends on.
- For every `LOCAL` row, write one line in the note saying what we are declining
  and why it stays declined. A refusal that is never restated becomes drift
  nobody remembers choosing.
- `CONVERGED` rows need no action, but list them: they are evidence of duplicated
  work and therefore of what should have been contributed sooner.

**3. Outbound — triage, then contribute.** Every candidate is one of:
- **Generic** — the fix makes sense to somebody who has never heard of this
  project. It goes in the pull request.
- **Local by construction** — it names this project, its domain vocabulary, a
  customer or vendor system, an internal host, the layer taxonomy, the forge or
  wiki this project happens to use, or one of its own documents. It never goes
  upstream. If it will *always* be local, add it to `localizedPaths` in
  `state.json` so future passes stop re-asking.

  The forge is a common and easy miss: a link shaped like one host's URLs is
  local even though nothing in it looks project-specific. Its vocabulary usually
  is not, though — "merge request" and "pull request" name the same act, so
  prefer wording that reads on either, and where a tool must match the word,
  make it accept both. A file kept local purely over vocabulary is a divergence
  bought for nothing, and it will have to be re-triaged every pass.
- **Generic but too big for this pass** — record it in the note as queued, with
  a one-line reason. Do not let it silently vanish.

Prefer a small, readable pull request over a comprehensive one. A feature port
deserves its own pull request, not a seat inside a batch of bug fixes.

**4. Open the pull request.**
- Confirm the scope with the owner first. Publishing to GitHub is outward-facing
  and hard to undo; everything before this step is reversible.
- Clone upstream to a scratch directory, branch, copy in **only** the agreed
  files, commit with a message that says where each fix was found, push, and open
  the pull request with `gh`.
- Scrub before pushing: no project-only paths, no internal hostnames, no ticket
  or wiki-page identifiers, no customer or vendor data. Read the diff — with
  `upstream_sync.py diff <path>` — and do not assume.
- Append the pull request to `contributions` in `state.json`.

**5. Advance the baseline.** `set-baseline <tip> --note "<what this pass did>"`.
Do this even when every verdict was `CONVERGED` or `LOCAL` — the baseline records
*reviewed up to here*, not *adopted up to here*.

**6. Write the note.** `docs/upstream-syncs/YYYY-MM-DD.md` from the template
below. Finalize a `docs/activity-log.md` entry.

**7. Commit locally — never push to `origin`.** Stage explicit paths; a sibling
session may share the tree.

## Boundaries (hard rules)

- **Never push to `origin`.** Commit locally (`CLAUDE.md`). Pushing *upstream* to
  GitHub is a separate, owner-confirmed act.
- **Never adopt a `LOCAL` path without an explicit recorded decision.** The
  divergence list exists because somebody decided; overturning it is a decision
  of the same weight.
- **Never weaken a check to make an adoption apply.** If upstream's version drops
  a guard we added, keep ours and contribute ours back instead.
- **Nothing project-specific leaves the building.** No domain vocabulary, vendor
  system details, internal hostnames, forge or wiki identifiers, or credentials
  in an upstream pull request.
- **Idempotent and honest.** If upstream has not moved and nothing is worth
  contributing, still write a short note so the cadence clock advances and the
  trail is unbroken.
- **English artifacts** (`CLAUDE.md`).

## Note template

```markdown
# Upstream harness sync — YYYY-MM-DD

- Cadence: monthly · Last pass: <date or "first"> · Supersedes: <path or "—">
- Upstream: EnHarnes <url> · baseline <sha before> → <sha after>
- Verdict: <one line — what moved in each direction>

## Inbound
<per-file: verdict, path, what was done or why not — including every LOCAL refusal>

## Outbound
- Contributed: <files> → <PR url or "not opened (<reason>)">
- Queued: <candidate> — <why it waits>
- Local by construction: <candidate> — <why it never goes upstream>

## Divergence-list changes
<paths added to or removed from localizedPaths, with reasons>  (or "none")

## Needs human
<decisions, or "none">

## Next pass
<what to pick up>
```

## State

`.claude/skills/harness.upstream/state.json` — per fork, and the only file here
that is. A fresh fork copies `state.example.json` beside it and fills in the
upstream URL and the commit it was copied from; the template itself ships only
the example, because the template is nobody's fork.

- `cadenceDays` — read by the due-check hook.
- `upstream` — name, clone URL, ref, pull-request URL.
- `baseline` — the upstream commit this harness has been reviewed against, its
  date, when we adopted it, and why.
- `trackedPaths` — globs for template-owned files. Everything else is `IGNORED`
  in both directions.
- `localizedPaths` — `{path, reason}` pairs; deliberate divergences, never taken
  and never offered.
- `contributions` — the ledger of pull requests we have opened upstream.

"Last pass" is derived from the newest `docs/upstream-syncs/YYYY-MM-DD.md`, not
stored here.
