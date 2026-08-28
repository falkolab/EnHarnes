#!/usr/bin/env python3
"""Check that worktree_boot.py roots new worktrees at the MAIN checkout.

Booting a task from inside another task's worktree used to nest the new checkout
under the current one (.claude/worktrees/task-a/.claude/worktrees/task-b),
because the script used `Path.cwd()` as the repo root.

Runs against a throwaway repo, so it never touches the real worktree list.

    python scripts/verify/verify_worktree_boot.py
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOT = REPO_ROOT / "scripts" / "harness" / "worktree_boot.py"


def load_boot():
    spec = importlib.util.spec_from_file_location("worktree_boot", BOOT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clean_env() -> dict[str, str]:
    """Environment with git's per-invocation variables stripped.

    `make lint` also runs from the pre-commit hook, which exports GIT_DIR /
    GIT_INDEX_FILE. Inherited by a subprocess they redirect every git command at
    the REAL repo regardless of cwd, so the throwaway fixture below would mutate
    this repository instead.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                   check=True, env=clean_env())


def main() -> int:
    boot = load_boot()
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        main_repo = tmp / "main"
        main_repo.mkdir()
        git(["init", "-b", "main"], main_repo)
        git(["config", "user.email", "verify@example.com"], main_repo)
        git(["config", "user.name", "verify"], main_repo)
        (main_repo / "README.md").write_text("probe\n")
        # Committed, not just written: a new git worktree receives only committed
        # ignore rules, and the bridge guard consults them from inside it.
        (main_repo / ".gitignore").write_text(".env\n.venv\n")
        git(["add", "."], main_repo)
        git(["commit", "-m", "init"], main_repo)

        # Local environment that a fresh worktree must inherit.
        (main_repo / ".env").write_text("SECRET=probe\n")
        (main_repo / ".venv" / "bin").mkdir(parents=True)
        (main_repo / ".venv" / "bin" / "pip").write_text("#!/bin/sh\nexit 0\n")
        (main_repo / ".venv" / "bin" / "pip").chmod(0o755)

        linked = main_repo / ".claude" / "worktrees" / "task-a"
        linked.parent.mkdir(parents=True)
        git(["worktree", "add", str(linked), "-b", "task/a"], main_repo)

        # 1. Resolved from inside a LINKED worktree, the root must still be main.
        resolved = boot.main_worktree_root(linked)
        if resolved.resolve() != main_repo.resolve():
            failures.append(
                f"main_worktree_root(linked) = {resolved} — expected the main checkout {main_repo}"
            )
            print(f"[FAIL] resolves the main checkout from a linked worktree — got {resolved}")
        else:
            print("[OK ] resolves the main checkout from inside a linked worktree")

        # 2. And from the main checkout itself.
        resolved_main = boot.main_worktree_root(main_repo)
        if resolved_main.resolve() != main_repo.resolve():
            failures.append(f"main_worktree_root(main) = {resolved_main}")
            print(f"[FAIL] resolves the main checkout from the main checkout — got {resolved_main}")
        else:
            print("[OK ] resolves the main checkout from the main checkout")

        # 3. The path a boot from inside task-a would use must not nest.
        would_create = resolved / ".claude" / "worktrees" / "task-b"
        if str(linked) in str(would_create):
            failures.append(f"new worktree would nest inside {linked}: {would_create}")
            print(f"[FAIL] new worktree path nests under the current worktree: {would_create}")
        else:
            print("[OK ] a task booted from another worktree lands beside it, not inside it")

        # 4. Environment bridging.
        target = main_repo / ".claude" / "worktrees" / "task-b"
        target.mkdir(parents=True)
        deps_safe = boot.bridge_environment(main_repo, target)
        env_link = target / ".env"
        if not env_link.is_symlink() or env_link.resolve() != (main_repo / ".env").resolve():
            failures.append(".env was not bridged into the new worktree")
            print("[FAIL] .env bridged into the worktree")
        else:
            print("[OK ] .env bridged into the worktree")

        # A per-worktree venv must EXIST and must not be a link to the main
        # checkout. Asserting only "not a symlink" passes when nothing was created
        # at all — it cannot tell success from total failure.
        target_venv = target / ".venv"
        if not target_venv.is_dir() or target_venv.is_symlink() or not deps_safe:
            failures.append(
                f".venv must be a real per-worktree dir: is_dir={target_venv.is_dir()} "
                f"is_symlink={target_venv.is_symlink()} deps_safe={deps_safe}"
            )
            print("[FAIL] .venv is a real per-worktree virtualenv, not a shared link")
        else:
            print("[OK ] .venv is a real per-worktree virtualenv, not a shared link")

        # And it must be the venv dependency installs actually use.
        pip = boot.venv_bin(target, "pip")
        if pip is None or not Path(pip).exists():
            failures.append("venv_bin did not find pip in the per-worktree virtualenv")
            print("[FAIL] finds pip inside the per-worktree virtualenv")
        else:
            print("[OK ] finds pip inside the per-worktree virtualenv")

        # 5. Bridging is idempotent and never clobbers a real file.
        real_env = main_repo / ".claude" / "worktrees" / "task-c"
        real_env.mkdir(parents=True)
        (real_env / ".env").write_text("LOCAL=override\n")
        boot.bridge_environment(main_repo, real_env)
        if (real_env / ".env").read_text() != "LOCAL=override\n":
            failures.append("bridge_environment overwrote an existing .env")
            print("[FAIL] leaves an existing .env untouched")
        else:
            print("[OK ] leaves an existing .env untouched")

        # 6. An inherited GIT_DIR must not hijack the resolution. `make lint` runs
        #    from the pre-commit hook, where git exports GIT_DIR/GIT_INDEX_FILE;
        #    before the fix, that pointed resolution at the real repo regardless
        #    of cwd — i.e. the script would create worktrees in the wrong place.
        hijack = dict(os.environ)
        hijack["GIT_DIR"] = str(REPO_ROOT / ".git")
        hijack["GIT_WORK_TREE"] = str(REPO_ROOT)
        saved = {k: os.environ.get(k) for k in ("GIT_DIR", "GIT_WORK_TREE")}
        os.environ.update({"GIT_DIR": hijack["GIT_DIR"], "GIT_WORK_TREE": hijack["GIT_WORK_TREE"]})
        try:
            under_hijack = boot.main_worktree_root(main_repo)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        if under_hijack.resolve() != main_repo.resolve():
            failures.append(
                f"an inherited GIT_DIR hijacked resolution: got {under_hijack}, expected {main_repo}"
            )
            print(f"[FAIL] ignores an inherited GIT_DIR — got {under_hijack}")
        else:
            print("[OK ] ignores an inherited GIT_DIR (pre-commit context)")

        # 7a. A name the repo does not ignore must NOT be bridged: the symlink's
        #     blob is this machine's absolute path, and `git add -A` would commit it.
        #     Exercised through `.env` — the only symlink still bridged, so this
        #     cannot pass vacuously the way a `.venv` assertion now would.
        unignored = main_repo / ".claude" / "worktrees" / "task-d"
        unignored.mkdir(parents=True, exist_ok=True)
        (main_repo / ".gitignore").write_text(".venv\n")  # deliberately omits .env
        boot.bridge_environment(main_repo, unignored)
        if (unignored / ".env").is_symlink():
            failures.append("bridged .env even though .gitignore does not cover it")
            print("[FAIL] refuses to bridge a name the repo does not ignore")
        else:
            print("[OK ] refuses to bridge a name the repo does not ignore")
        (main_repo / ".gitignore").write_text(".env\n.venv\n")

        # 7b. Unresolvable repo must STOP, not fall back to cwd. That fallback was
        #     the nesting bug, and it would fire exactly when git is misbehaving.
        outside = tmp / "not-a-repo"
        outside.mkdir(exist_ok=True)
        try:
            got = boot.main_worktree_root(outside)
            failures.append(f"main_worktree_root on a non-repo returned {got} instead of exiting")
            print(f"[FAIL] refuses to guess outside a repo — returned {got}")
        except SystemExit as exc:
            if exc.code == 0:
                failures.append("main_worktree_root exited 0 on a non-repo (should be non-zero)")
                print("[FAIL] refuses to guess outside a repo — exited 0")
            else:
                print("[OK ] refuses to guess outside a repo (exits non-zero, no cwd fallback)")

        # 8. End-to-end: run the script's CLI from inside a linked worktree.
        #    The helpers above can all be correct while main() still wires them
        #    up wrong — that is precisely the unit-vs-wire gap that let the dead
        #    hooks pass their own tests.
        proc = subprocess.run(
            [sys.executable, str(BOOT), "e2e-probe"],
            cwd=linked, capture_output=True, text=True, timeout=300, env=clean_env(),
        )
        expected = main_repo / ".claude" / "worktrees" / "e2e-probe"
        nested = linked / ".claude" / "worktrees" / "e2e-probe"
        if nested.exists():
            failures.append(f"CLI nested the new worktree inside the invoking one: {nested}")
            print(f"[FAIL] CLI roots the worktree at the main checkout — nested at {nested}")
        elif not expected.exists():
            failures.append(
                f"CLI did not create {expected} (rc={proc.returncode}); stderr={proc.stderr[-300:]!r}"
            )
            print(f"[FAIL] CLI roots the worktree at the main checkout — {expected} missing")
        else:
            print("[OK ] CLI booted from a linked worktree roots the new one at the main checkout")
            if (expected / ".env").is_symlink():
                print("[OK ] CLI bridged .env into the new worktree")
            else:
                failures.append("CLI did not bridge .env into the new worktree")
                print("[FAIL] CLI bridged .env into the new worktree")

        for worktree in (expected, linked):
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                           cwd=main_repo, capture_output=True, env=clean_env())

    print()
    if failures:
        print(f"[verify-worktree-boot] {len(failures)} FAILURE(S):")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("[verify-worktree-boot] OK: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
