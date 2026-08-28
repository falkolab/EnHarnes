#!/usr/bin/env python3
"""PreToolUse hook: blocks dangerous bash commands.

Reads tool input from stdin, returns JSON decision to stdout.
Blocks: rm -rf /, force pushes to main, dropping databases, etc.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hook_io  # noqa: E402  (path shim above must run first)


# --- Force-push-to-main guard ---
# Scoped to main/master ONLY — feature-branch force-push (needed after a rebase under the
# fast-forward-only merge policy) stays allowed. Rewriting published main is an owner-run
# exception (see AGENTS.md), never an automated agent step.
#
# Catches, beyond a plain `--force`:
#   * short `-f` and combined short flags (`-fq`, `-qf`)
#   * `--force-with-lease` / `--force-if-includes`
#   * the `+refspec` force form (`git push origin +main`, `+HEAD:main`, `+refs/heads/main`)
#   * global options between `git` and `push` (`git -c k=v push …`, `git -C path push …`)
#   * force flag / target split across lines — continuations (`\<newline>`, CRLF, trailing
#     whitespace) AND bare newlines inside `$(…)`/quotes: the patterns match with re.DOTALL
#     so `.*` spans line breaks. This fails CLOSED — a multi-line command that both
#     force-pushes and names main/master (even in unrelated segments) is denied. That is the
#     safe direction for this guard; split such calls if you hit it.
# One shell word for an option VALUE, allowing embedded quoted spans —
# `user.name="Coding Agent"`, `"/path with space"`. A bare \S+ here was a
# bypass: any quoted value containing a space broke the whole _GIT_PUSH match,
# and with it every force-push pattern built on it.
_WORD = r"(?:\"[^\"]*\"|'[^']*'|[^\s\"'])+"
_GIT_PUSH = rf"git(?:\s+(?:-c\s+{_WORD}|-C\s+{_WORD}|--[\w-]+(?:={_WORD})?|-\w))*\s+push\b"
# Lookbehind (?<![\w-]) anchors the flag to a real token start, so it does not match the
# "-force" fragment inside an unrelated long flag such as --follow-tags.
_FORCE = r"(?<![\w-])(?:--force(?:-with-lease|-if-includes)?|-[a-zA-Z]*f[a-zA-Z]*)\b"
# main/master only as a whole ref token: preceded by a separator (space, /, :, +) and NOT
# glued to more branch-name characters (- or / or word chars). So `feature/main-fix`,
# `feature/domain-main`, `release/master-cutover` are NOT treated as the main/master ref,
# while `main`, `+main`, `HEAD:main`, `refs/heads/main` are.
_MAIN = r"(?<=[\s/:+])(?:main|master)(?![\w/-])"
_FORCE_MAIN = "Force push to main/master is blocked (owner-run exception only — see AGENTS.md)"

BLOCKED_PATTERNS = [
    (r"rm\s+-rf\s+/(?!\S)", "Refusing to rm -rf /"),
    (rf"{_GIT_PUSH}.*{_FORCE}.*{_MAIN}", _FORCE_MAIN),       # force flag, then main/master
    (rf"{_GIT_PUSH}.*{_MAIN}.*{_FORCE}", _FORCE_MAIN),       # main/master, then force flag
    (rf"{_GIT_PUSH}.*\+\S*{_MAIN}", _FORCE_MAIN),            # +refspec force form
    (r"git\s+reset\s+--hard\s+origin/(main|master)", "Hard reset to origin main/master is blocked"),
    (r"DROP\s+(DATABASE|TABLE)", "DROP DATABASE/TABLE is blocked"),
    (r"truncate\s+table", "TRUNCATE TABLE is blocked"),
    (r":(){ :\|:& };:", "Fork bomb detected"),
    (r"mkfs\.", "Filesystem format command is blocked"),
    (r"> /dev/sd[a-z]", "Direct device write is blocked"),
]

ASK_PATTERNS = [
    (r"git\s+push", "About to push to remote — confirm?"),
    (r"rm\s+-rf", "Recursive delete — confirm target?"),
    (r"git\s+reset\s+--hard", "Hard reset will discard changes — confirm?"),
    (r"pip\s+install(?!.*-r\s+requirements)", "Installing package outside requirements.txt — confirm?"),
]

# .* must span newlines so a force flag / target split across lines cannot slip through.
_FLAGS = re.IGNORECASE | re.DOTALL


def decide(command: str):
    """Return (decision, reason) for a command, or None if nothing matches."""
    for pattern, reason in BLOCKED_PATTERNS:
        if re.search(pattern, command, _FLAGS):
            return "deny", reason
    for pattern, reason in ASK_PATTERNS:
        if re.search(pattern, command, _FLAGS):
            return "ask", reason
    return None


def main():
    # Fails closed: an unparsable payload blocks rather than waving the command
    # through. This guard is the last line in front of destructive commands.
    event = hook_io.require_event("validate-bash")

    if event.tool_name != "Bash":
        return

    command = event.command
    if not command:
        return

    result = decide(command)
    if result is not None:
        decision, reason = result
        hook_io.permission(decision, reason)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # a guard must not fail OPEN via exit 1 on an odd payload shape
        hook_io.block(f"validate-bash: internal error — blocking to fail closed: {exc!r}")
