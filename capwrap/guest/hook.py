#!/usr/bin/env python3
"""PreToolUse hook: route an agent's permission prompts to the operator's inbox.

Registered inside a sandbox as a Claude Code `PreToolUse` hook.  Claude runs it
before every tool call and waits for its verdict, so this is where a prompt that
would otherwise appear in the agent's own terminal gets diverted to the capwrap
web UI instead.

That is the point of the whole approval feature: with five agents running you do
not want five terminals each blocking on their own y/n prompt.  They all queue
in one list, and answering there unblocks the agent that asked.

Protocol, from the installed `claude`'s own documentation:

    stdin   {"hook_event_name": "PreToolUse", "tool_name": ..., "tool_input": {...},
             "session_id": ..., "cwd": ...}
    stdout  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                    "permissionDecision": "allow"|"deny"|"ask",
                                    "permissionDecisionReason": "..."}}

Failure is deliberately *not* silent-allow: if the daemon cannot be reached we
return "ask", which falls back to Claude's own prompt.  A hook that defaulted to
"allow" when broken would quietly disable every permission check in the system.
"""

from __future__ import annotations

import fnmatch
import json
import os
import socket
import sys
from typing import Any

SOCKET = os.environ.get("CAPWRAP_SOCKET", "/run/capwrap.sock")
POLICY = os.environ.get("CAPWRAP_POLICY", "/opt/capwrap/policy.json")

#: Long, because the whole design is that a human answers this. Claude's own
#: prompt has no timeout either.
ASK_TIMEOUT = float(os.environ.get("CAPWRAP_ASK_TIMEOUT", "3600"))


def respond(decision: str, reason: str = "") -> None:
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
    sys.stdout.flush()
    sys.exit(0)


def load_policy() -> dict:
    """Auto-decisions the operator configured up front.

    Reduces the queue to things that actually need a human: nobody wants to
    approve every single `Read`.
    """
    try:
        with open(POLICY) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def matches(rules: list[str], tool: str, command: str) -> bool:
    """A rule matches a bare tool name, or `Tool(pattern)` against its argument.

    ``Bash(git *)`` is the shape people actually want: allow git, keep asking
    about everything else the same tool could do.
    """
    for rule in rules:
        if rule == tool or rule == "*":
            return True
        if "(" in rule and rule.endswith(")"):
            name, _, pattern = rule.partition("(")
            if name == tool and fnmatch.fnmatch(command, pattern[:-1]):
                return True
    return False


def describe(tool: str, tool_input: dict) -> str:
    """A one-line summary, because that is what the operator reads first.

    The fallback shows argument *values*, not just their names: a queue entry
    reading "Skill: skill" tells you nothing, while "Skill: skill=capwrap" tells
    you what is about to happen.
    """
    if tool == "Bash":
        return str(tool_input.get("command", "")).strip()
    for key in ("file_path", "path", "url", "pattern", "notebook_path",
                "command", "name", "query", "prompt"):
        if key in tool_input:
            return _short(tool_input[key])

    parts = [f"{k}={_short(v)}" for k, v in sorted(tool_input.items())]
    return ", ".join(parts[:3]) or "(no arguments)"


def _short(value: object, limit: int = 120) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def ask_operator(question: str, context: dict) -> dict:
    """Block on the capwrap daemon until the operator answers."""
    if not os.path.exists(SOCKET):
        raise OSError(f"no capability socket at {SOCKET}")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(ASK_TIMEOUT)
    try:
        sock.connect(SOCKET)
        sock.sendall(json.dumps({
            "id": 1,
            "op": "ask",
            "args": {
                "question": question,
                "context": context,
                "block": True,
                "timeout": ASK_TIMEOUT,
            },
        }).encode() + b"\n")

        buffer = b""
        while b"\n" not in buffer:
            chunk = sock.recv(65536)
            if not chunk:
                raise OSError("the daemon closed the connection")
            buffer += chunk
    finally:
        sock.close()

    reply = json.loads(buffer.split(b"\n", 1)[0])
    if not reply.get("ok"):
        raise OSError((reply.get("error") or {}).get("message", "request failed"))
    return reply.get("result") or {}


def main() -> None:
    try:
        event: dict[str, Any] = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        respond("ask", "capwrap: could not parse the hook payload")

    tool = event.get("tool_name", "?")
    tool_input = event.get("tool_input") or {}
    summary = describe(tool, tool_input)

    policy = load_policy()
    if matches(policy.get("deny", []), tool, summary):
        respond("deny", f"capwrap policy denies {tool}")
    if matches(policy.get("allow", []), tool, summary):
        respond("allow", f"capwrap policy allows {tool}")

    container = os.environ.get("CAPWRAP_CONTAINER", "?")
    question = f"{tool}: {summary}" if summary else f"run {tool}"

    try:
        result = ask_operator(question, {
            "tool": tool,
            "input": tool_input,
            "container": container,
            "cwd": event.get("cwd"),
            "session": event.get("session_id"),
        })
    except (OSError, json.JSONDecodeError, socket.timeout) as exc:
        # Fall back to Claude's own prompt rather than deciding for the operator.
        respond("ask", f"capwrap unreachable ({exc}); falling back to the local prompt")

    decision = result.get("decision")
    reason = result.get("reason") or ""
    if decision == "allow":
        respond("allow", reason or "approved in the capwrap console")
    if decision == "deny":
        respond("deny", reason or "denied in the capwrap console")
    respond("ask", reason or "no answer from the operator")


if __name__ == "__main__":
    main()
