#!/usr/bin/env python3
"""Case table for hooks_registered.py — what counts as a hook that must be wired.

The linter's job is to notice a hook nobody registered. Its own failure mode is
the opposite one: answering about a set that no longer contains the file in
question, and reporting that everything is fine. Every case below fixes which
files are in scope, because that is the part a change can quietly shrink.

Hermetic: each case builds a fake `.claude/` tree and points the module's
directory and settings constants at it. No repository, no network.

Run by `make lint-hooks`. Exit 0 = every case holds.
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys
import tempfile
from contextlib import redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".claude/skills/harness.linters/scripts/code-health/hooks_registered.py"

spec = importlib.util.spec_from_file_location("hooks_registered", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

failures: list[str] = []


def run_case(name: str, files: dict[str, str | bytes], registered: list[str]) -> tuple[int, str]:
    """Build a hooks directory plus a settings file, then run the linter over it."""
    with tempfile.TemporaryDirectory() as tmp:
        claude = pathlib.Path(tmp) / ".claude"
        hooks = claude / "hooks"
        hooks.mkdir(parents=True)
        for filename, body in files.items():
            if isinstance(body, bytes):
                (hooks / filename).write_bytes(body)
            else:
                (hooks / filename).write_text(body, encoding="utf-8")
        settings = claude / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [
                                    {"command": f"python3 .claude/hooks/{n}"} for n in registered
                                ],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        mod.HOOKS_DIR = hooks
        mod.SETTINGS_SOURCES = [settings]
        out = io.StringIO()
        with redirect_stdout(out):
            rc = mod.main()
        return rc, out.getvalue()


def check(name: str, got: object, want: object) -> None:
    if got == want:
        print(f"  ok   {name}")
    else:
        failures.append(f"{name}: got {got!r}, want {want!r}")
        print(f"  FAIL {name}\n         got:  {got!r}\n         want: {want!r}")


GUARDED = 'import sys\n\nif __name__ == "__main__":\n    sys.exit(0)\n'
# The same hook after its entry point was renamed — still a hook, still wired,
# still able to no-op if nobody registers it.
RENAMED = "import sys\n\ndef entry():\n    sys.exit(0)\n\nentry()\n"
LIBRARY = "def read_payload():\n    return {}\n"

# 1. The ordinary case: an unregistered hook is an error naming the file.
rc, out = run_case("unregistered", {"validate-bash.py": GUARDED}, registered=[])
check("an unregistered hook -> error", rc, 1)
check("the error names the file", "validate-bash.py" in out, True)

# 2. Registered: silent success.
rc, out = run_case("registered", {"validate-bash.py": GUARDED}, registered=["validate-bash.py"])
check("a registered hook -> ok", rc, 0)

# 3. The regression this table exists for. Under the previous rule a file was a
#    hook only if it contained an entry-point guard, so renaming the entry point
#    removed the file from the check and the linter reported every hook wired.
rc, out = run_case("renamed entry point", {"validate-bash.py": RENAMED}, registered=[])
check("an unregistered hook whose entry point was renamed -> still an error", rc, 1)
check("and it is still named", "validate-bash.py" in out, True)

# 4. A genuine shared module is exempt, and its absence from settings is fine.
rc, out = run_case("library", {"hook_io.py": LIBRARY, "validate-bash.py": GUARDED},
                   registered=["validate-bash.py"])
check("a named library module is not required to be registered", rc, 0)

# 5. The opposite mistake: something exempted as a library that carries an entry
#    point. Not an error — a warning, because the name may be legitimate.
rc, out = run_case("library in disguise", {"hook_io.py": GUARDED, "validate-bash.py": GUARDED},
                   registered=["validate-bash.py"])
check("a library carrying an entry-point guard -> warned, not failed", rc, 0)
check("the warning names it", "hook_io.py" in out and "disguise" in out, True)

# 6. An unreadable file must not drop out of scope; deny by default means it is
#    judged by its name, and its name is not exempt.
rc, out = run_case("binary", {"validate-bash.py": b"\xff\xfe not decodable as utf-8\n"}, registered=[])
check("a file that cannot be read as text is still in scope", rc, 1)

if failures:
    print(f"\n[verify-hooks-registered] FAILED: {len(failures)} case(s):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\n[verify-hooks-registered] OK: all cases hold.")
