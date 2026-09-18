#!/usr/bin/env python3
"""End-to-end check that every hook actually fires on a real Claude Code payload.

Why this exists
---------------
In a sibling project both settings-wired hooks read `toolName`/`toolInput` while
Claude Code sends `tool_name`/`tool_input` — every payload fell through an early
return and the guards enforced nothing, silently, for months. Downstream the
hooks were then verified alive, but only the force-push deny table was covered:
nothing exercised the other hooks end-to-end, and nothing asserted that each hook
is registered under the right event with a matcher covering its tool set — the
second half of "looks installed, enforces nothing".

So this script tests the layer that can break: it runs each hook as a
subprocess, writes a real payload to its stdin, and asserts on exit code and
stdout — the same contract Claude Code uses. Payload shapes are transcribed from
the shipped binary's own hook builders (transcribed at v2.1.211, re-verified
unchanged at v2.1.220, 2026-08-03):

    {hook_event_name: "PreToolUse",      tool_name, tool_input, tool_use_id}
    {hook_event_name: "PostToolUse",     tool_name, tool_input, tool_response, ...}
    {hook_event_name: "UserPromptSubmit", prompt}

A decision is read from `hookSpecificOutput.permissionDecision`; a block is exit
code 2 with the reason on stderr.

What this does NOT do
---------------------
It does **not** read the installed Claude Code binary. Those payload shapes are a
hand-transcribed snapshot (v2.1.211, re-verified unchanged at v2.1.220 on
2026-08-03), hardcoded here. So a green run is a regression guard for *our own*
hooks against *this* contract — it is NOT evidence that the contract still holds
after a Claude Code upgrade. After any upgrade, re-derive the shapes from the
binary by hand (the way the original investigation did) and update the version
recorded here; the checks below cannot tell you the wire format drifted.
(Correction, 2026-07-30 — an earlier claim that this defends against version
drift was wrong.)

    python scripts/verify/verify_hook_contract.py
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS = REPO_ROOT / ".claude" / "hooks"

PY = sys.executable or "python3"


def clean_env() -> dict[str, str]:
    """Environment with git's per-invocation variables stripped.

    `make lint` also runs from the pre-commit hook, where git exports GIT_DIR,
    GIT_INDEX_FILE and friends. Inherited by a subprocess, they point every git
    command at the REAL repo no matter its cwd — so the throwaway fixtures below
    would operate on this repository's index. Caught by pre-commit on first run.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


results: list[tuple[bool, str, str]] = []


def record(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"[{'OK ' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def run_hook(hook: str, payload, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Invoke a hook exactly as Claude Code does: argv-less, payload on stdin."""
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [PY, str(HOOKS / hook)],
        input=stdin, capture_output=True, text=True,
        cwd=str(cwd or REPO_ROOT), timeout=30, env=clean_env(),
    )


def decision_of(proc: subprocess.CompletedProcess) -> str | None:
    """The permission decision a hook emitted, or None if it stayed silent."""
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    return data.get("hookSpecificOutput", {}).get("permissionDecision")


def prompt_decision(proc: subprocess.CompletedProcess) -> str | None:
    """The top-level `decision` a UserPromptSubmit hook emitted, or None.

    Parsed, not substring-matched: `'"block"' in stdout` would also pass on any
    output that merely mentions the word.
    """
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out).get("decision")
    except json.JSONDecodeError:
        return None


def pre_tool_use(tool_name: str, tool_input: dict, cwd: str | None = None) -> dict:
    payload = {
        "session_id": "verify",
        "transcript_path": "/dev/null",
        "cwd": cwd or str(REPO_ROOT),
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": "toolu_verify",
    }
    return payload


def post_tool_use(tool_name: str, tool_input: dict, cwd: str) -> dict:
    return {
        "session_id": "verify",
        "cwd": cwd,
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_response": {"type": "text", "text": "ok"},
        "tool_use_id": "toolu_verify",
        "duration_ms": 1,
    }


def git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                   check=True, env=clean_env())


def make_repo(tmp: Path) -> Path:
    """A throwaway git repo on `main` with one commit."""
    repo = tmp / "repo"
    repo.mkdir()
    git(["init", "-b", "main"], repo)
    git(["config", "user.email", "verify@example.com"], repo)
    git(["config", "user.name", "verify"], repo)
    (repo / "README.md").write_text("probe\n")
    git(["add", "."], repo)
    git(["commit", "-m", "init"], repo)
    return repo


# --------------------------------------------------------------------------
# validate-bash.py — the guard that was silently dead
# --------------------------------------------------------------------------
def check_validate_bash() -> None:
    proc = run_hook("validate-bash.py", pre_tool_use("Bash", {"command": "git push --force origin main"}))
    record(decision_of(proc) == "deny", "validate-bash: force-push to main is DENIED on a real payload",
           f"decision={decision_of(proc)!r} stdout={proc.stdout[:200]!r}")

    proc = run_hook("validate-bash.py", pre_tool_use("Bash", {"command": "git push -f origin feature/foo"}))
    record(decision_of(proc) == "ask", "validate-bash: feature-branch force-push is ASK",
           f"decision={decision_of(proc)!r}")

    proc = run_hook("validate-bash.py", pre_tool_use("Bash", {"command": "ls -la"}))
    record(decision_of(proc) is None and proc.returncode == 0,
           "validate-bash: harmless command passes silently",
           f"rc={proc.returncode} stdout={proc.stdout[:120]!r}")

    # Tolerance for the legacy camelCase spelling — a rename must not silently disarm the guard.
    legacy = {"hook_event_name": "PreToolUse", "toolName": "Bash",
              "toolInput": {"command": "git push --force origin main"}}
    proc = run_hook("validate-bash.py", legacy)
    record(decision_of(proc) == "deny", "validate-bash: camelCase payload still DENIED (alias tolerance)",
           f"decision={decision_of(proc)!r}")

    # --- Fail-closed contract. These two cases were absent from this file, which is
    # why the guard could fail OPEN unnoticed: a traceback exits 1, and Claude Code
    # treats anything other than 2 as non-blocking, so the command ran unexamined.
    # Stricter than the template's own assertion (which accepts 0 or 2) because our
    # entry point now blocks outright.

    proc = run_hook("validate-bash.py", pre_tool_use("Bash", {"command": 123}))
    record(proc.returncode == 2,
           "validate-bash: non-string command fails CLOSED, never OPEN via exit 1",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")

    # Every denial must carry the rule, not only the refusal: the block is the one
    # message the agent is certain to read, and the obvious next move — re-spell it,
    # or write it to a file and run that — is the one the harness forbids and cannot
    # see. Asserted here because the deny/allow table checks the verdict, not the text.
    temp_script = "python3 /tmp/probe.py"
    proc = run_hook("validate-bash.py", pre_tool_use("Bash", {"command": temp_script}))
    reason = ""
    try:
        reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    except Exception:
        pass
    record(decision_of(proc) == "deny" and "not an obstacle" in reason,
           "validate-bash: a denial states the rule against routing around it",
           f"decision={decision_of(proc)!r} reason={reason[:120]!r}")
    # Assert on the rule's OWN text, not the whole reason: the appended paragraph
    # mentions scripts/verify/… itself, so a bare `"scripts/" in reason` passed for
    # every deny in the file and pinned nothing. Caught by review.
    own_text = reason.split("\n\n")[0]
    record("Put it under scripts/" in own_text,
           "validate-bash: a denial names what to do instead, in its own message",
           f"own_text={own_text[:160]!r}")
    # The appended paragraph must offer all THREE responses, the third being "the
    # step has no sanctioned tool yet — build one". It said two for a while: a rebase
    # conflict was resolved with `git checkout --ours` on the whole file, which
    # discards everything else that commit changed there, and the loss was invisible
    # because nothing asserted the text. An agent offered only "stop" or "change the
    # guard" has been handed a dead end, which is when routing around starts to look
    # reasonable — the exact behaviour this paragraph exists to prevent.
    appended = reason.split("\n\n", 1)[-1]
    record("Three honest responses" in appended and "no sanctioned tool" in appended,
           "validate-bash: a denial offers the third response, not a dead end",
           f"appended={appended[-200:]!r}")

    # Fails closed rather than waving the command through.
    proc = run_hook("validate-bash.py", "this is not json")
    record(proc.returncode == 2, "validate-bash: unparsable payload fails CLOSED (exit 2)",
           f"rc={proc.returncode}")

    proc = run_hook("validate-bash.py", pre_tool_use("Read", {"file_path": "README.md"}))
    record(proc.returncode == 0 and decision_of(proc) is None,
           "validate-bash: ignores non-Bash tools", f"rc={proc.returncode}")


# --------------------------------------------------------------------------
# validate-bash.py commit guard — a Bash-mediated write (echo / sed / python -c)
# lands on the integration branch via `git commit`, the one choke point shell
# writes share. Branch-aware: silent on a task branch, ASK on main.
# (Review finding, 2026-07-29 — reproduced bypass of the edit-on-main story.)
# --------------------------------------------------------------------------
def check_commit_guard(tmp: Path) -> None:
    repo = make_repo(tmp / "commitguard")
    write_and_commit = {"command": "printf X > src/f.py && git add -A && git commit -m owned"}

    proc = run_hook("validate-bash.py", pre_tool_use("Bash", write_and_commit, cwd=str(repo)), cwd=repo)
    record(decision_of(proc) == "ask" and "integration branch" in proc.stdout,
           "validate-bash: `git commit` on main ASKS (Bash-write choke point)",
           f"decision={decision_of(proc)!r} stdout={proc.stdout[:200]!r}")

    git(["checkout", "-q", "-b", "task/probe"], repo)
    proc = run_hook("validate-bash.py", pre_tool_use("Bash", write_and_commit, cwd=str(repo)), cwd=repo)
    record(decision_of(proc) is None and proc.returncode == 0,
           "validate-bash: `git commit` on a task branch stays silent",
           f"decision={decision_of(proc)!r} rc={proc.returncode}")

    git(["checkout", "-q", "main"], repo)
    proc = run_hook("validate-bash.py", pre_tool_use("Bash", {"command": "git status"}, cwd=str(repo)), cwd=repo)
    record(decision_of(proc) is None and proc.returncode == 0,
           "validate-bash: non-commit git command unaffected by the commit guard",
           f"decision={decision_of(proc)!r} rc={proc.returncode}")


# --------------------------------------------------------------------------
# validate-bash.py — the one destructive git operation with no undo
#
# `git checkout -- <paths>` / `git restore <paths>` overwrite the working copy from the
# index. Uncommitted work in those files is gone, with no reflog entry. The guard is
# state-aware rather than textual: it asks only when the named paths actually carry
# uncommitted changes, because an ask that fires on every restore is an ask nobody reads.
# Every case below therefore sets up real repository state instead of asserting on a
# command string, and each must go red if the guard is removed.
# --------------------------------------------------------------------------
def check_restore_guard(tmp: Path) -> None:
    base = tmp / "restoreguard"
    base.mkdir(parents=True, exist_ok=True)
    repo = make_repo(base)
    (repo / "src").mkdir(exist_ok=True)
    tracked = repo / "src" / "keep.py"
    tracked.write_text("original\n")
    other = repo / "src" / "other.py"
    other.write_text("other original\n")
    git(["add", "."], repo)
    git(["commit", "-m", "seed"], repo)

    def ask_for(command):
        proc = run_hook("validate-bash.py",
                        pre_tool_use("Bash", {"command": command}, cwd=str(repo)), cwd=repo)
        return decision_of(proc), proc.stdout

    # Every case pairs its silence with a control that must still fire, because "expect no
    # decision" is also what a REMOVED guard produces — the trap this suite already caught
    # once in the sibling hook. Each record below goes red if the guard is taken out.

    # 1. Clean vs dirty, same command. Restoring a clean file discards nothing and must be
    #    silent; the moment the file holds work, the same command must ask and name it.
    clean_decision, _ = ask_for("git checkout -- src/keep.py")
    tracked.write_text("uncommitted work that would be lost\n")
    dirty_decision, dirty_out = ask_for("git checkout -- src/keep.py")
    record(clean_decision is None and dirty_decision == "ask" and "src/keep.py" in dirty_out,
           "validate-bash: a restore is silent on a clean file and asks on a dirty one",
           f"clean={clean_decision!r} dirty={dirty_decision!r} stdout={dirty_out[:200]!r}")

    # 2. Only the NAMED paths count: dirt elsewhere is not at risk and must not fire, while
    #    naming that same dirty file must.
    tracked.write_text("original\n")          # keep.py clean again
    other.write_text("dirty but not named\n")
    unnamed_decision, _ = ask_for("git checkout -- src/keep.py")
    named_decision, named_out = ask_for("git checkout -- src/other.py")
    record(unnamed_decision is None and named_decision == "ask" and "src/other.py" in named_out,
           "validate-bash: dirt is judged per named path, not per repository",
           f"unnamed={unnamed_decision!r} named={named_decision!r} stdout={named_out[:160]!r}")

    # 3. A branch switch keeps the working tree, so it is not this act — but the path form
    #    of the very same verb, on the very same dirty file, is.
    branch_decision, _ = ask_for("git checkout main")
    path_decision, _ = ask_for("git checkout -- src/other.py")
    record(branch_decision is None and path_decision == "ask",
           "validate-bash: `git checkout <branch>` is not a restore, `git checkout --` is",
           f"branch={branch_decision!r} path={path_decision!r}")

    # 4. `--staged` only unstages — the working copy survives — while the plain form of the
    #    same command overwrites it. A guard that cannot tell them apart is either noise or
    #    useless; this pins both halves.
    git(["add", "src/other.py"], repo)
    staged_decision, _ = ask_for("git restore --staged src/other.py")
    worktree_decision, worktree_out = ask_for("git restore src/other.py")
    record(staged_decision is None and worktree_decision == "ask"
           and "src/other.py" in worktree_out,
           "validate-bash: `git restore --staged` is silent, `git restore` is not",
           f"staged={staged_decision!r} worktree={worktree_decision!r} "
           f"stdout={worktree_out[:160]!r}")


# --------------------------------------------------------------------------
# prompt-validator.py — secret scanner, same class of bug (`userPrompt`)
# --------------------------------------------------------------------------
def check_prompt_validator() -> None:
    leak = {"hook_event_name": "UserPromptSubmit",
            "prompt": "deploy with ghp_" + "a" * 36}
    proc = run_hook("prompt-validator.py", leak)
    blocked = prompt_decision(proc) == "block" or proc.returncode == 2
    record(blocked, "prompt-validator: GitHub token in a prompt is BLOCKED",
           f"rc={proc.returncode} stdout={proc.stdout[:200]!r}")

    clean = {"hook_event_name": "UserPromptSubmit", "prompt": "please refactor the basket view"}
    proc = run_hook("prompt-validator.py", clean)
    record(proc.returncode == 0 and not proc.stdout.strip(),
           "prompt-validator: clean prompt passes silently (stdout is injected into context)",
           f"rc={proc.returncode} stdout={proc.stdout[:120]!r}")


# --------------------------------------------------------------------------
# validate-edit.py — new guard: no file edits on main
#
# Exit 2 alone is ambiguous here: the guard blocks for five different reasons
# (on-main, detached HEAD, no resolvable path, unparsable payload, git
# unavailable) and all of them exit 2. A check that means to pin ONE of them
# must read the reason on stderr — an exit-code-only NotebookEdit check stayed
# green over a reverted notebook_path alias, because control merely fell into
# the no-path branch (2026-07-29 follow-up).
# --------------------------------------------------------------------------
ON_MAIN_MARKER = "agents do not write to the integration branch"
NO_PATH_MARKER = "no resolvable target path"


def blocked_on_main(proc: subprocess.CompletedProcess) -> bool:
    """Blocked specifically for being on main — not via another exit-2 path."""
    return (proc.returncode == 2
            and ON_MAIN_MARKER in proc.stderr
            and NO_PATH_MARKER not in proc.stderr)


def check_validate_edit(tmp: Path) -> None:
    repo = make_repo(tmp)
    target = str(repo / "src" / "thing.py")

    proc = run_hook("validate-edit.py", pre_tool_use("Edit", {"file_path": target}, cwd=str(repo)), cwd=repo)
    record(blocked_on_main(proc), "validate-edit: editing on main is BLOCKED for being on main",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")

    proc = run_hook("validate-edit.py", pre_tool_use("Write", {"file_path": target}, cwd=str(repo)), cwd=repo)
    record(blocked_on_main(proc), "validate-edit: Write on main is BLOCKED too",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")

    # NotebookEdit carries `notebook_path`, not `file_path` — the alias in
    # hook_io must not let notebooks dodge the guard.
    # blocked_on_main(), not returncode==2: with the alias reverted this block
    # still exits 2 via the no-path branch, and the check must NOT stay green.
    notebook = str(repo / "analysis.ipynb")
    proc = run_hook("validate-edit.py",
                    pre_tool_use("NotebookEdit", {"notebook_path": notebook}, cwd=str(repo)), cwd=repo)
    record(blocked_on_main(proc),
           "validate-edit: NotebookEdit on main is BLOCKED for being on main (notebook_path resolved)",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")

    # A matched edit tool with no resolvable target must fail CLOSED, not shrug.
    proc = run_hook("validate-edit.py", pre_tool_use("Edit", {}, cwd=str(repo)), cwd=repo)
    record(proc.returncode == 2 and NO_PATH_MARKER in proc.stderr,
           "validate-edit: matched tool with no path fails CLOSED (no-path reason)",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")

    # Nothing is exempt on main any more (operator decision, 2026-09-16 —
    # hook_io.ALLOWED_ON_MAIN is empty and says why). The activity log used to be, so it
    # is the case worth pinning: the exception is gone, not merely unused.
    log = repo / "docs" / "activity-log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("log\n")
    proc = run_hook("validate-edit.py", pre_tool_use("Edit", {"file_path": str(log)}, cwd=str(repo)), cwd=repo)
    record(blocked_on_main(proc), "validate-edit: the activity log is no longer exempt on main",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")

    git(["checkout", "-q", "-b", "task/probe"], repo)
    proc = run_hook("validate-edit.py", pre_tool_use("Edit", {"file_path": target}, cwd=str(repo)), cwd=repo)
    record(proc.returncode == 0, "validate-edit: editing on a task branch is ALLOWED",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")

    proc = run_hook("validate-edit.py",
                    pre_tool_use("NotebookEdit", {"notebook_path": notebook}, cwd=str(repo)), cwd=repo)
    record(proc.returncode == 0, "validate-edit: NotebookEdit on a task branch is ALLOWED",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")

    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True, env=clean_env()).stdout.strip()
    git(["checkout", "-q", head], repo)
    proc = run_hook("validate-edit.py", pre_tool_use("Edit", {"file_path": target}, cwd=str(repo)), cwd=repo)
    record(proc.returncode == 2 and "detached HEAD" in proc.stderr,
           "validate-edit: detached HEAD fails CLOSED (detached-HEAD reason)",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")

    proc = run_hook("validate-edit.py", "not json", cwd=repo)
    record(proc.returncode == 2 and "could not parse" in proc.stderr,
           "validate-edit: unparsable payload fails CLOSED (parse reason)",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")

    # Back on main: the checks below claim to test the guard against a repo on
    # main, and reading the block reason (not just exit 2) is what caught this —
    # left detached, the GIT_DIR case below blocks for the WRONG reason and an
    # exit-code-only assertion cannot tell.
    git(["checkout", "-q", "main"], repo)

    # --- Reproduced bypasses (review, 2026-07-28). Both were exploitable when the
    # --- branch was read from `cwd` instead of from the target file's own repo.

    # A task worktree lives INSIDE the main checkout, so an absolute path (or one
    # `../` too many) reaches a file on main while cwd reports a safe branch.
    main_repo = make_repo(tmp / "cross")
    task_wt = main_repo / ".claude" / "worktrees" / "task-x"
    task_wt.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "-q", str(task_wt), "-b", "task/x"], main_repo)
    on_main = str(main_repo / "src" / "important.py")
    proc = run_hook("validate-edit.py",
                    pre_tool_use("Edit", {"file_path": on_main}, cwd=str(task_wt)), cwd=task_wt)
    record(blocked_on_main(proc),
           "validate-edit: cwd on a task branch cannot authorize editing a file on main",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")

    # ...and the mirror image: a file inside the task worktree stays editable
    # even though cwd's *parent* repo is on main.
    in_worktree = str(task_wt / "src" / "safe.py")
    proc = run_hook("validate-edit.py",
                    pre_tool_use("Edit", {"file_path": in_worktree}, cwd=str(main_repo)), cwd=main_repo)
    record(proc.returncode == 0,
           "validate-edit: a file inside a task worktree stays editable",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")

    # An inherited GIT_DIR must not redirect the branch check at another repo.
    safe_repo = make_repo(tmp / "safe")
    git(["checkout", "-q", "-b", "task/safe"], safe_repo)
    env = clean_env()
    env["GIT_DIR"] = str(safe_repo / ".git")
    env["GIT_WORK_TREE"] = str(safe_repo)
    proc = subprocess.run(
        [PY, str(HOOKS / "validate-edit.py")],
        input=json.dumps(pre_tool_use("Edit", {"file_path": str(repo / "src" / "x.py")}, cwd=str(repo))),
        capture_output=True, text=True, cwd=str(repo), timeout=30, env=env,
    )
    record(blocked_on_main(proc),
           "validate-edit: an inherited GIT_DIR cannot disarm the guard",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")

    # git unavailable → must block, not error out with a non-blocking exit 1.
    nogit = tmp / "nogit"
    nogit.mkdir(exist_ok=True)
    (nogit / "python3").symlink_to(PY)
    stripped = clean_env()
    stripped["PATH"] = str(nogit)
    proc = subprocess.run(
        [PY, str(HOOKS / "validate-edit.py")],
        input=json.dumps(pre_tool_use("Edit", {"file_path": str(repo / "src" / "x.py")}, cwd=str(repo))),
        capture_output=True, text=True, cwd=str(repo), timeout=30, env=stripped,
    )
    record(proc.returncode == 2 and "could not run git" in proc.stderr,
           "validate-edit: git unavailable fails CLOSED (exit 2 with the git-unavailable reason)",
           f"rc={proc.returncode} stderr={proc.stderr[:200]!r}")


    # A JSON-valid but non-string target path must fail CLOSED (exit 2), not crash
    # with exit 1, which Claude Code reads as a non-blocking error.
    proc = run_hook("validate-edit.py",
                    pre_tool_use("Edit", {"file_path": 123}, cwd=str(repo)), cwd=repo)
    record(proc.returncode == 2,
           "validate-edit: a non-string target path fails CLOSED (exit 2, not a crash)",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")


# --------------------------------------------------------------------------
# post-edit-lint.py — feedback on the file just edited
# --------------------------------------------------------------------------

def check_post_edit_lint(tmp: Path) -> None:
    if shutil.which("ast-grep") is None:
        record(True, "post-edit-lint: SKIPPED (ast-grep not installed)")
        return

    repo = make_repo(tmp / "lint")
    shutil.copy(REPO_ROOT / "sgconfig.yml", repo / "sgconfig.yml")
    # Mirror every rule directory sgconfig.yml names, not a hardcoded one: the
    # hook scans through that config, and ast-grep refuses to start when a listed
    # directory is missing — which makes the hook return no findings at all, and
    # this case fail for a reason that has nothing to do with the hook.
    for line in (REPO_ROOT / "sgconfig.yml").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and not stripped.startswith("- #"):
            rule_dir = stripped[2:].strip()
            if (REPO_ROOT / rule_dir).is_dir():
                shutil.copytree(REPO_ROOT / rule_dir, repo / rule_dir)

    src = repo / "src" / "services"
    src.mkdir(parents=True)

    bad = src / "bad.py"
    bad.write_text("def f():\n    try:\n        pass\n    except:\n        pass\n")
    proc = run_hook("post-edit-lint.py", post_tool_use("Edit", {"file_path": str(bad)}, str(repo)), cwd=repo)
    record(proc.returncode == 2 and "no-bare-except" in proc.stderr,
           "post-edit-lint: reports a violation in the edited file",
           f"rc={proc.returncode} stderr={proc.stderr[:200]!r}")

    good = src / "good.py"
    good.write_text("def f() -> int:\n    return 1\n")
    proc = run_hook("post-edit-lint.py", post_tool_use("Edit", {"file_path": str(good)}, str(repo)), cwd=repo)
    record(proc.returncode == 0 and not proc.stderr.strip(),
           "post-edit-lint: silent on a clean file",
           f"rc={proc.returncode} stderr={proc.stderr[:200]!r}")

    outside = repo / "scripts" / "tool.py"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("def f():\n    try:\n        pass\n    except:\n        pass\n")
    proc = run_hook("post-edit-lint.py", post_tool_use("Edit", {"file_path": str(outside)}, str(repo)), cwd=repo)
    record(proc.returncode == 0, "post-edit-lint: ignores files outside src/", f"rc={proc.returncode}")

    md = repo / "src" / "notes.md"
    md.write_text("# notes\n")
    proc = run_hook("post-edit-lint.py", post_tool_use("Write", {"file_path": str(md)}, str(repo)), cwd=repo)
    record(proc.returncode == 0, "post-edit-lint: ignores non-Python files", f"rc={proc.returncode}")


# --------------------------------------------------------------------------
# post-bash-main-clean.py — the write that no PreToolUse guard can see
#
# `validate-edit` refuses Edit/Write on `main`. A shell command reaches the same
# files by a route it cannot inspect, so this hook watches the effect instead of
# the command. The cases below therefore never assert on a command string: they
# dirty the checkout by hand and ask whether the hook noticed.
#
# EVERY case here must go red if the hook body is replaced by `return`. That is not
# a stylistic preference: a hook that exits 0 is indistinguishable from a hook that
# is dead, so a bare "expect silence" assertion passes against a corpse. The first
# draft of this suite had five such cases out of seven — all of them green against a
# gutted hook, all of them claiming to prove an exclusion worked. Each "silence" case
# below is therefore PAIRED with a positive one in the same fixture: silence for the
# excluded thing, a report for a control that sits right beside it. If you add a case,
# gut `main()` and confirm yours fails before believing it.
# --------------------------------------------------------------------------
def check_post_bash_main_clean(tmp: Path) -> None:
    base = tmp / "bashclean"
    base.mkdir()
    repo = make_repo(base)          # a repo whose only worktree is on `main`
    (repo / "docs").mkdir()
    (repo / "docs" / "activity-log.md").write_text("# log\n")
    git(["add", "."], repo)
    git(["commit", "-m", "log"], repo)

    payload = post_tool_use("Bash", {"command": "echo hello"}, str(repo))
    state = repo / ".git" / "main-clean-baseline.json"

    # 1. The first look over a DIRTY tree must say what it adopted. Silence here was a
    #    real hole (review, 2026-09-15): the first Bash call after the state file is
    #    lost is also the call that could have made the mess, so adopting it quietly
    #    swallows precisely the write this guard exists to catch.
    (repo / "README.md").write_text("pre-existing dirt\n")
    proc = run_hook("post-bash-main-clean.py", payload, cwd=repo)
    baseline_written = state.exists() and "README.md" in state.read_text(encoding="utf-8")
    record(proc.returncode == 2 and "README.md" in proc.stderr and baseline_written,
           "post-bash-main-clean: a dirty first look names what it adopted as baseline",
           f"rc={proc.returncode} state_exists={state.exists()} stderr={proc.stderr[:200]!r}")

    # 1b. A first look over a CLEAN tree has nothing to say, and must not invent it.
    clean = tmp / "bashclean-clean"
    clean.mkdir()
    repo_clean = make_repo(clean)
    proc_clean = run_hook("post-bash-main-clean.py",
                          post_tool_use("Bash", {"command": "echo hello"}, str(repo_clean)),
                          cwd=repo_clean)
    (repo_clean / "ROADMAP.md").write_text("now something appears\n")
    proc_after = run_hook("post-bash-main-clean.py",
                          post_tool_use("Bash", {"command": "echo hello"}, str(repo_clean)),
                          cwd=repo_clean)
    record(proc_clean.returncode == 0 and not proc_clean.stderr.strip()
           and proc_after.returncode == 2 and "ROADMAP.md" in proc_after.stderr,
           "post-bash-main-clean: a clean first look is quiet, the next write is not",
           f"first_rc={proc_clean.returncode} after_rc={proc_after.returncode} "
           f"stderr={proc_after.stderr[:160]!r}")

    # 2. Pre-existing dirt, now the baseline, does not fire again — while a file
    #    added in the same breath DOES. One fixture, both halves: the silence is
    #    only meaningful next to a control the hook still reports.
    proc = run_hook("post-bash-main-clean.py", payload, cwd=repo)
    quiet_on_known = proc.returncode == 0
    (repo / "ROADMAP.md").write_text("written by a script\n")
    proc = run_hook("post-bash-main-clean.py", payload, cwd=repo)
    record(quiet_on_known and proc.returncode == 2
           and "ROADMAP.md" in proc.stderr and "README.md" not in proc.stderr,
           "post-bash-main-clean: known dirt stays quiet, a new write does not",
           f"quiet={quiet_on_known} rc={proc.returncode} stderr={proc.stderr[:200]!r}")

    # 3. The report names the remedy. A notice nobody can act on is the failure
    #    mode this hook exists to avoid — I was given no remedy and reverted by hand.
    record("checkout --" in proc.stderr and "worktree_boot" in proc.stderr,
           "post-bash-main-clean: the report names the remedy",
           f"stderr={proc.stderr[:200]!r}")

    # 4. Nothing is exempt on `main` any more (hook_io.ALLOWED_ON_MAIN is empty and says
    #    why). The activity log was the last exception, so it is the one worth pinning:
    #    a write to it must now be reported like any other. Dirtied TOGETHER with an
    #    ordinary file so the case still proves the hook reports what it sees rather than
    #    merely staying quiet.
    (repo / "docs" / "activity-log.md").write_text("# log\n- entry\n")
    (repo / "AGENTS.md").write_text("ordinary file\n")
    proc = run_hook("post-bash-main-clean.py", payload, cwd=repo)
    record(proc.returncode == 2
           and "AGENTS.md" in proc.stderr and "activity-log" in proc.stderr,
           "post-bash-main-clean: the activity log has no exemption left either",
           f"rc={proc.returncode} stderr={proc.stderr[:200]!r}")

    # 5. Not our tool. Same dirt, two payloads: a Read is ignored, the Bash that
    #    follows reports it. The pair proves the silence was tool-specific rather
    #    than universal.
    (repo / "Makefile").write_text("# new\n")
    proc_read = run_hook("post-bash-main-clean.py",
                         post_tool_use("Read", {"file_path": str(repo / "Makefile")}, str(repo)),
                         cwd=repo)
    proc_bash = run_hook("post-bash-main-clean.py", payload, cwd=repo)
    record(proc_read.returncode == 0 and not proc_read.stderr.strip()
           and proc_bash.returncode == 2 and "Makefile" in proc_bash.stderr,
           "post-bash-main-clean: a non-Bash tool is ignored, the same dirt via Bash is not",
           f"read_rc={proc_read.returncode} bash_rc={proc_bash.returncode} "
           f"bash_stderr={proc_bash.stderr[:160]!r}")

    # 6. A repository that never had a `main` checkout has nothing to protect, and
    #    says nothing — but prove the silence is about that, not a dead hook: the same
    #    repository reports the moment `main` is checked out and a write appears.
    detached = tmp / "detached"
    detached.mkdir()
    repo2 = make_repo(detached)
    git(["checkout", "--detach"], repo2)
    (repo2 / "README.md").write_text("dirty but unowned\n")
    payload2 = post_tool_use("Bash", {"command": "echo hello"}, str(repo2))
    proc_detached = run_hook("post-bash-main-clean.py", payload2, cwd=repo2)
    git(["checkout", "main"], repo2)                 # re-attach: now there IS a main checkout
    run_hook("post-bash-main-clean.py", payload2, cwd=repo2)   # first look adopts the dirt
    (repo2 / "ROADMAP.md").write_text("now it matters\n")
    proc_attached = run_hook("post-bash-main-clean.py", payload2, cwd=repo2)
    record(proc_detached.returncode == 0 and not proc_detached.stderr.strip()
           and proc_attached.returncode == 2 and "ROADMAP.md" in proc_attached.stderr,
           "post-bash-main-clean: quiet where main was never seen, live once main is back",
           f"detached_rc={proc_detached.returncode} attached_rc={proc_attached.returncode} "
           f"stderr={proc_attached.stderr[:160]!r}")

    # 7. Going blind must be announced. A `main` checkout that WAS protected and then
    #    detaches (history archaeology, a bisect) used to drop protection in perfect
    #    silence, indistinguishable from "all clear", and restore it just as quietly —
    #    review finding, 2026-09-15. It is said once, not on every command.
    git(["checkout", "--detach"], repo2)
    blind1 = run_hook("post-bash-main-clean.py", payload2, cwd=repo2)
    blind2 = run_hook("post-bash-main-clean.py", payload2, cwd=repo2)
    record(blind1.returncode == 2 and "blind" in blind1.stderr.lower()
           and blind2.returncode == 0,
           "post-bash-main-clean: a lapse in protection is announced once, not repeatedly",
           f"first_rc={blind1.returncode} second_rc={blind2.returncode} "
           f"stderr={blind1.stderr[:160]!r}")


# --------------------------------------------------------------------------
# Registration: a hook that exists but is not wired up enforces nothing — and
# neither does one wired to the wrong event, or behind a matcher that misses the
# tools it guards. Substring-matching the filename anywhere in settings.json
# cannot tell those apart: moving validate-edit.py to a Stop hook still passed.
# (Review finding, 2026-07-28.)
# --------------------------------------------------------------------------
EXPECTED_WIRING = [
    # (hook file, event, tool names its matcher must cover)
    ("validate-bash.py", "PreToolUse", ["Bash"]),
    ("validate-edit.py", "PreToolUse", ["Edit", "Write", "MultiEdit", "NotebookEdit"]),
    ("post-edit-lint.py", "PostToolUse", ["Edit", "Write", "MultiEdit"]),
    ("post-bash-main-clean.py", "PostToolUse", ["Bash"]),
    ("prompt-validator.py", "UserPromptSubmit", []),
    # Hooks wired long before the contract test existed, asserted here so a
    # settings.json refactor cannot silently drop or re-home them.
    ("log-agent-usage.py", "PostToolUse", ["Task"]),
    ("post-response-sync.py", "Stop", []),
    ("upstream-sync-due.py", "SessionStart", []),
]


def matcher_covers(matcher: str, tool: str) -> bool:
    """Whether a settings.json matcher selects `tool`.

    Matchers are regex with `|` (and, since 2.1.191, `,`) alternation; an empty
    matcher means every invocation of the event.
    """
    if not matcher:
        return True
    return tool in {part.strip() for part in matcher.replace(",", "|").split("|")}


def load_hook_module(filename: str):
    """Import a hook by path so its declared constants can be asserted against."""
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_"), HOOKS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Each edit tool's own name for its target. They are NOT interchangeable: a guard
# handling one spelling silently ignores every tool that uses the other.
TOOL_TARGET_KEY = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}


def check_edit_tool_coverage(tmp: Path) -> None:
    """Every tool `validate-edit.py` claims must be provably guarded, one by one.

    Listing a tool in `EDIT_TOOLS` and in the settings.json matcher is a claim.
    Nothing checked that claim against the tool's actual payload — which is how
    NotebookEdit sat in the set, in the matcher AND in the registration assertion
    while being unguarded, because it names its target `notebook_path`.

    The loop is derived from the hook's own constant, so adding a tool without
    adding its target key and exercising it turns this red instead of silently
    widening a claim nothing tests.
    """
    guard = load_hook_module("validate-edit.py")
    declared = set(guard.EDIT_TOOLS)

    unmapped = sorted(declared - set(TOOL_TARGET_KEY))
    record(not unmapped,
           "every tool in validate-edit.EDIT_TOOLS has a known target key",
           f"no target key mapped for {unmapped}")

    repo = make_repo(tmp / "coverage")
    for tool in sorted(declared & set(TOOL_TARGET_KEY)):
        key = TOOL_TARGET_KEY[tool]
        suffix = ".ipynb" if key == "notebook_path" else ".py"
        target = str(repo / f"probe_{tool.lower()}{suffix}")
        proc = run_hook("validate-edit.py",
                        pre_tool_use(tool, {key: target}, cwd=str(repo)), cwd=repo)
        # Must block *for being on main*, not via the fail-closed no-path branch —
        # that also exits 2 and would mask an unhandled key.
        resolved = NO_PATH_MARKER not in proc.stderr
        record(proc.returncode == 2 and resolved,
               f"{tool} on main is BLOCKED for being on main (via {key})",
               f"rc={proc.returncode} stderr={proc.stderr[:180]!r}")


def check_registration() -> None:
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    hooks = settings.get("hooks", {})

    for hook_file, event, tools in EXPECTED_WIRING:
        entries = [
            entry for entry in hooks.get(event, [])
            if any(hook_file in inner.get("command", "") for inner in entry.get("hooks", []))
        ]
        if not entries:
            record(False, f"{hook_file} is registered under {event}", f"absent from hooks.{event}")
            continue

        missing = [
            tool for tool in tools
            if not any(matcher_covers(entry.get("matcher", ""), tool) for entry in entries)
        ]
        record(
            not missing,
            f"{hook_file} runs on {event} for {', '.join(tools) if tools else 'every prompt'}",
            f"matcher does not cover {missing}",
        )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for sub in ("lint", "cross", "safe", "commitguard", "coverage"):
            (tmp / sub).mkdir()
        check_validate_bash()
        check_commit_guard(tmp)
        check_restore_guard(tmp)
        check_prompt_validator()
        check_validate_edit(tmp)
        check_post_edit_lint(tmp)
        check_post_bash_main_clean(tmp)
        check_edit_tool_coverage(tmp)
        check_registration()

    failures = [(name, detail) for ok, name, detail in results if not ok]
    print()
    if failures:
        print(f"[verify-hook-contract] {len(failures)} FAILURE(S):")
        for name, detail in failures:
            print(f"  {name}: {detail}")
        return 1
    print(f"[verify-hook-contract] OK: all {len(results)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
