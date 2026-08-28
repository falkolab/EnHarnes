#!/usr/bin/env python3
"""PreToolUse hook: blocks dangerous bash commands.

Reads tool input from stdin, returns JSON decision to stdout.
Blocks: rm -rf /, force pushes to main, dropping databases, etc.
"""

import json
import re
import sys


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
_PROTECTED = r"\b(?:main|master)\b"

BLOCKED_PATTERNS = [
    (r"rm\s+-rf\s+/(?!\S)", "Refusing to rm -rf /"),
    # flag form, either order (flag before or after the branch)
    (rf"{_PUSH}[^\n]*{_FORCE_FLAG}[^\n]*{_PROTECTED}", "Force push to main/master is blocked"),
    (rf"{_PUSH}[^\n]*{_PROTECTED}[^\n]*{_FORCE_FLAG}", "Force push to main/master is blocked"),
    # leading-'+' refspec form (a force push that never says "force")
    (rf"{_PUSH}[^\n]*\+\S*{_PROTECTED}", "Force push to main/master is blocked"),
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


def _emit(decision: str, reason: str) -> None:
    """Emit a PreToolUse permission decision in Claude Code's current schema."""
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        },
    }, sys.stdout)


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return

    # Claude Code passes snake_case keys (tool_name / tool_input).
    tool_name = data.get("tool_name", "")
    if tool_name != "Bash":
        return

    command = data.get("tool_input", {}).get("command", "")
    if not command:
        return

    # Shell joins backslash-newline continuations into one logical line; collapse
    # them so a wrapped command can't slip a force flag past a per-line matcher.
    command = re.sub(r"\\\r?\n", " ", command)

    for pattern, reason in BLOCKED_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            _emit("deny", reason)
            return

    for pattern, reason in ASK_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            _emit("ask", reason)
            return


if __name__ == "__main__":
    main()
