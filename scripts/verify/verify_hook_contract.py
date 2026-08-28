#!/usr/bin/env python3
"""End-to-end check that every hook actually fires on a real Claude Code payload.

Why this exists
---------------
`verify_force_push_guard.py` imports `decide()` and asserts a deny table — and it
passed, green, for months while the force-push guard enforced nothing. The guard
read `toolName`/`toolInput` from stdin; Claude Code sends `tool_name`/`tool_input`.
Every payload fell through the early return, so the hook exited without an opinion.
Unit-testing the decision function skipped the layer that was broken.

So this script tests the layer that was broken: it runs each hook as a
subprocess, writes a real payload to its stdin, and asserts on exit code and
stdout — the same contract Claude Code uses. Payload shapes were transcribed by
hand from the shipped binary's hook builders (transcribed at **v2.1.211**, re-verified unchanged at **v2.1.220** (2026-08-03)) and are
hardcoded below. Nothing here reads the installed binary, so this is a regression
guard for OUR side of the contract — it cannot detect Claude Code changing the
contract on an upgrade. After an upgrade, re-derive the shapes from the binary by
hand and update the version recorded here.

    {hook_event_name: "PreToolUse",      tool_name, tool_input, tool_use_id}
    {hook_event_name: "PostToolUse",     tool_name, tool_input, tool_response, ...}
    {hook_event_name: "UserPromptSubmit", prompt}

A decision is read from `hookSpecificOutput.permissionDecision`; a block is exit
code 2 with the reason on stderr.

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
    would operate on this repository's index instead.
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


def decision_field(proc: subprocess.CompletedProcess) -> str | None:
    """A top-level `decision` from a hook's stdout JSON (UserPromptSubmit form)."""
    out = proc.stdout.strip()
    if not out:
        return None
    for line in out.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "decision" in data:
            return data["decision"]
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

    # Fails closed rather than waving the command through.
    proc = run_hook("validate-bash.py", "this is not json")
    record(proc.returncode == 2, "validate-bash: unparsable payload fails CLOSED (exit 2)",
           f"rc={proc.returncode}")

    proc = run_hook("validate-bash.py", pre_tool_use("Read", {"file_path": "README.md"}))
    record(proc.returncode == 0 and decision_of(proc) is None,
           "validate-bash: ignores non-Bash tools", f"rc={proc.returncode}")


# --------------------------------------------------------------------------
# prompt-validator.py — secret scanner, same class of bug (`userPrompt`)
# --------------------------------------------------------------------------
def check_prompt_validator() -> None:
    leak = {"hook_event_name": "UserPromptSubmit",
            "prompt": "deploy with ghp_" + "a" * 36}
    proc = run_hook("prompt-validator.py", leak)
    # Parse the decision rather than substring-matching the word "block" anywhere in
    # stdout — the loose form also passes on output that merely echoes it.
    record(decision_field(proc) == "block" or proc.returncode == 2,
           "prompt-validator: GitHub token in a prompt is BLOCKED",
           f"rc={proc.returncode} decision={decision_field(proc)!r} stdout={proc.stdout[:200]!r}")

    clean = {"hook_event_name": "UserPromptSubmit", "prompt": "please refactor the basket view"}
    proc = run_hook("prompt-validator.py", clean)
    record(decision_field(proc) is None and proc.returncode == 0,
           "prompt-validator: clean prompt passes",
           f"rc={proc.returncode} decision={decision_field(proc)!r}")


# --------------------------------------------------------------------------
# validate-edit.py — new guard: no file edits on main
# --------------------------------------------------------------------------
def check_validate_edit(tmp: Path) -> None:
    repo = make_repo(tmp)
    target = str(repo / "src" / "thing.py")

    proc = run_hook("validate-edit.py", pre_tool_use("Edit", {"file_path": target}, cwd=str(repo)), cwd=repo)
    record(proc.returncode == 2, "validate-edit: editing on main is BLOCKED",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")

    proc = run_hook("validate-edit.py", pre_tool_use("Write", {"file_path": target}, cwd=str(repo)), cwd=repo)
    record(proc.returncode == 2, "validate-edit: Write on main is BLOCKED too", f"rc={proc.returncode}")

    # The audit trail stays writable on main.
    allowed = repo / "docs" / "activity-log.md"
    allowed.parent.mkdir(parents=True, exist_ok=True)
    allowed.write_text("log\n")
    proc = run_hook("validate-edit.py", pre_tool_use("Edit", {"file_path": str(allowed)}, cwd=str(repo)), cwd=repo)
    record(proc.returncode == 0, "validate-edit: activity-log stays editable on main",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")

    git(["checkout", "-q", "-b", "task/probe"], repo)
    proc = run_hook("validate-edit.py", pre_tool_use("Edit", {"file_path": target}, cwd=str(repo)), cwd=repo)
    record(proc.returncode == 0, "validate-edit: editing on a task branch is ALLOWED",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")

    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True, env=clean_env()).stdout.strip()
    git(["checkout", "-q", head], repo)
    proc = run_hook("validate-edit.py", pre_tool_use("Edit", {"file_path": target}, cwd=str(repo)), cwd=repo)
    record(proc.returncode == 2, "validate-edit: detached HEAD fails CLOSED", f"rc={proc.returncode}")

    proc = run_hook("validate-edit.py", "not json", cwd=repo)
    record(proc.returncode == 2, "validate-edit: unparsable payload fails CLOSED", f"rc={proc.returncode}")

    # --- Both of these were exploitable while the branch was read from `cwd`
    # --- instead of from the target file's own repository.

    # A task worktree lives INSIDE the main checkout, so an absolute path (or one
    # `../` too many) reaches a file on main while cwd reports a safe branch.
    main_repo = make_repo(tmp / "cross")
    task_wt = main_repo / ".claude" / "worktrees" / "task-x"
    task_wt.parent.mkdir(parents=True, exist_ok=True)
    git(["worktree", "add", "-q", str(task_wt), "-b", "task/x"], main_repo)
    on_main = str(main_repo / "src" / "important.py")
    proc = run_hook("validate-edit.py",
                    pre_tool_use("Edit", {"file_path": on_main}, cwd=str(task_wt)), cwd=task_wt)
    record(proc.returncode == 2,
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
    record(proc.returncode == 2,
           "validate-edit: an inherited GIT_DIR cannot disarm the guard",
           f"rc={proc.returncode} stderr={proc.stderr[:160]!r}")

    # NotebookEdit names its target `notebook_path`, so a guard reading only
    # `file_path` sees an empty path and allows notebook edits on main while the
    # matcher and check_registration() both claim coverage. These two cases keep
    # that shut.
    #
    # Assert on WHY it blocked, not just that it did. Exit 2 alone is not evidence
    # the notebook was understood: the fail-closed "no resolvable path" branch also
    # exits 2, so a check that only reads the code stays green even with the alias
    # reverted. Require the on-main guidance — proof the path was resolved and
    # its branch was judged.
    proc = run_hook("validate-edit.py",
                    pre_tool_use("NotebookEdit",
                                 {"notebook_path": str(repo / "analysis.ipynb"),
                                  "new_source": "x"},
                                 cwd=str(repo)), cwd=repo)
    resolved = "analysis.ipynb" in proc.stderr and "no resolvable target path" not in proc.stderr
    record(proc.returncode == 2 and resolved,
           "validate-edit: NotebookEdit on main is BLOCKED for being on main (notebook_path resolved)",
           f"rc={proc.returncode} stderr={proc.stderr[:200]!r}")

    # A matched edit tool with no resolvable path at all must fail closed rather
    # than read "no path" as "nothing to check".
    proc = run_hook("validate-edit.py",
                    pre_tool_use("Edit", {"some_future_key": "/tmp/x"}, cwd=str(repo)), cwd=repo)
    record(proc.returncode == 2,
           "validate-edit: an edit tool with no resolvable path fails CLOSED",
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
    record(proc.returncode == 2,
           "validate-edit: git unavailable fails CLOSED (exit 2, not a hook error)",
           f"rc={proc.returncode} stderr={proc.stderr[:200]!r}")


# --------------------------------------------------------------------------
# post-edit-lint.py — feedback on the file just edited
# --------------------------------------------------------------------------
def check_post_edit_lint(tmp: Path) -> None:
    if shutil.which("ast-grep") is None:
        record(True, "post-edit-lint: SKIPPED (ast-grep not installed)")
        return

    repo = make_repo(tmp / "lint")
    shutil.copy(REPO_ROOT / "sgconfig.yml", repo / "sgconfig.yml")
    shutil.copytree(REPO_ROOT / "policies" / "ast-grep", repo / "policies" / "ast-grep")

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
# Registration: a hook that exists but is not wired up enforces nothing — and
# neither does one wired to the wrong event, or behind a matcher that misses the
# tools it guards. Substring-matching the filename anywhere in settings.json
# cannot tell those apart: moving validate-edit.py to a Stop hook still passes it.
# --------------------------------------------------------------------------
EXPECTED_WIRING = [
    # (hook file, event, tool names its matcher must cover)
    ("validate-bash.py", "PreToolUse", ["Bash"]),
    ("validate-edit.py", "PreToolUse", ["Edit", "Write", "MultiEdit", "NotebookEdit"]),
    ("post-edit-lint.py", "PostToolUse", ["Edit", "Write", "MultiEdit"]),
    ("prompt-validator.py", "UserPromptSubmit", []),
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
        resolved = "no resolvable target path" not in proc.stderr
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
        for sub in ("lint", "cross", "safe", "coverage"):
            (tmp / sub).mkdir()
        check_validate_bash()
        check_prompt_validator()
        check_validate_edit(tmp)
        check_post_edit_lint(tmp)
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
