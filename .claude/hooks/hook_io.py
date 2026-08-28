#!/usr/bin/env python3
"""Shared stdin/stdout contract for Claude Code hooks.

Every hook here speaks the same wire protocol, so it is defined once instead of
being re-derived — and independently re-broken — per hook.

Read out of the shipped Claude Code binary — transcribed at v2.1.211, re-verified
unchanged at v2.1.220 (2026-08-03) — whose payload builders construct:

    {... hook_event_name: "PreToolUse",  tool_name, tool_input, tool_use_id}
    {... hook_event_name: "PostToolUse", tool_name, tool_input, tool_response,
                                         tool_use_id, duration_ms}
    {... hook_event_name: "UserPromptSubmit", prompt}

and whose decision reader dispatches on `hookSpecificOutput.permissionDecision`.
Re-verify there, in the binary, after a Claude Code upgrade, and record the
version checked. Prose summaries of this contract have carried wrong keys
(`user_prompt`, `tool_output`), either of which silently disables a hook built on
it. Note that `user_prompt` does occur in the binary — as a telemetry event field,
not a payload key; the UserPromptSubmit builder emits `prompt`.

Two rules, both of which fail silently when broken:

* Keys are **snake_case**. A hook reading `toolName`/`toolInput`/`userPrompt`
  parses nothing, returns early, and no-ops — it looks installed and enforces
  nothing.
* A permission decision must nest under `hookSpecificOutput`. A top-level
  `{"permissionDecision": ...}` is not read on that path.

`read_event()` also accepts the camelCase spellings, so a future rename degrades
to "still works" rather than "guard silently disappears". `require_event()` is
the fail-closed variant for guards that must never skip.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# Git's per-invocation variables. They outrank `cwd`: inherited by a subprocess,
# they point every git command at the repo that exported them, whatever directory
# the command was pointed at. A hook that shells out to git while one of these is
# set is judging the wrong repository — which, for a guard, means an
# ambient variable can disarm it. Stripped by `git_env()` below.
#
# Deliberately a fixed list rather than the whole `GIT_*` namespace: variables
# like GIT_SSH_COMMAND, GIT_ASKPASS and GIT_CONFIG_* carry the credentials and
# remote config a fetch needs, and dropping those turns an auth failure into a
# silent one.
_GIT_LOCATION_VARS = (
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX",
)


def git_env() -> dict[str, str]:
    """Environment for shelling out to git, with repo-location overrides removed."""
    return {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}

# Accepted spellings per logical field, most-correct first.
_ALIASES: dict[str, tuple[str, ...]] = {
    "hook_event_name": ("hook_event_name", "hookEventName"),
    "tool_name": ("tool_name", "toolName"),
    "tool_input": ("tool_input", "toolInput"),
    "tool_response": ("tool_response", "toolResponse", "tool_output"),
    "prompt": ("prompt", "user_prompt", "userPrompt"),
    "cwd": ("cwd",),
    # NotebookEdit names its target `notebook_path`, not `file_path` (verified in the
    # binary: 22 uses vs 5, with a `"notebook_path" in …` branch). A guard reading only
    # `file_path` therefore sees an empty path for every notebook edit and waves it
    # through while the matcher claims coverage.
    "target_path": ("file_path", "notebook_path"),
}


class HookEvent:
    """One hook invocation, with the wire spellings normalized away."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw

    def _get(self, field: str, default: Any = None) -> Any:
        for key in _ALIASES.get(field, (field,)):
            if key in self.raw:
                return self.raw[key]
        return default

    @property
    def event_name(self) -> str:
        return self._get("hook_event_name", "") or ""

    @property
    def tool_name(self) -> str:
        return self._get("tool_name", "") or ""

    @property
    def tool_input(self) -> dict[str, Any]:
        value = self._get("tool_input", {})
        return value if isinstance(value, dict) else {}

    @property
    def prompt(self) -> str:
        return self._get("prompt", "") or ""

    @property
    def file_path(self) -> str:
        """Target path for the file-editing tools.

        Covers both spellings, because they are not interchangeable per tool:
        Edit/Write use `file_path`, NotebookEdit uses `notebook_path`. Reading one
        of them means the other tool's calls arrive with an empty path — which a
        guard is liable to read as "nothing to check".
        """
        for key in _ALIASES["target_path"]:
            value = self.tool_input.get(key)
            if value:
                return value
        return ""

    @property
    def command(self) -> str:
        """Command line for the Bash tool."""
        return self.tool_input.get("command", "") or ""


def read_event() -> HookEvent | None:
    """Parse the hook payload from stdin. Returns None if stdin is not JSON."""
    try:
        raw = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return HookEvent(raw)


def require_event(hook_name: str) -> HookEvent:
    """Parse the payload, or fail CLOSED for guards that must not silently skip.

    An unparsable payload means the guard cannot judge the action. For a security
    guard that is a block, not a shrug: exit 2 stops the tool call and shows the
    reason to the model.
    """
    event = read_event()
    if event is None:
        block(f"{hook_name}: could not parse the hook payload — blocking to fail closed.")
    return event


def permission(decision: str, reason: str) -> None:
    """Emit a PreToolUse permission decision ("allow" | "deny" | "ask") and exit 0."""
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    sys.exit(0)


def block(reason: str) -> None:
    """Block via the universally-supported path: exit 2 with the reason on stderr.

    Works for PreToolUse (stops the call) and for PostToolUse (the tool already
    ran, so this is feedback to the model rather than a block).
    """
    print(reason, file=sys.stderr)
    sys.exit(2)
