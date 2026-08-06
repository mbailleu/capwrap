#!/usr/bin/env python3
"""capctl -- the agent's interface to the capability kernel.

Runs *inside* a sandbox.  Standard library only, single file, no imports from
the capwrap package: the guest tools directory is bind-mounted read-only into an
otherwise unrelated filesystem, so it cannot rely on capwrap being installed
there.

Everything an agent can do to the outside world it does through here, by way of
``/run/capwrap.sock``.  Slot numbers are local names in this container's own
capability table -- your slot 3 and another agent's slot 3 are unrelated, and
there is no way to refer to something you were not given.

    capctl caps                        what am I allowed to do?
    capctl send 4 "build is green"     message the holder of slot 4
    capctl recv --wait                 read my mailbox
    capctl ask "may I install curl?"   ask the human, and block for an answer
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from typing import Any

DEFAULT_SOCKET = os.environ.get("CAPWRAP_SOCKET", "/run/capwrap.sock")


class _SubParser(argparse.ArgumentParser):
    """Subparser that also understands the global flags.

    `add_subparsers(parser_class=...)` makes every subcommand inherit --json, so
    it works before or after the subcommand name.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("add_help", True)
        super().__init__(*args, **kwargs)
        self.add_argument("--json", action="store_true", help="raw JSON output")


class CapctlError(Exception):
    pass


def call(op: str, args: dict[str, Any] | None = None, timeout: float | None = 30.0) -> Any:
    """One request, one response, over the container's control socket."""
    path = DEFAULT_SOCKET
    if not os.path.exists(path):
        raise CapctlError(
            f"no capability socket at {path}. "
            "Either this is not a capwrap container, or the daemon is not running."
        )

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
    except OSError as exc:
        raise CapctlError(f"cannot reach the capwrap daemon: {exc}") from None

    try:
        sock.sendall(
            json.dumps({"id": 1, "op": op, "args": args or {}}).encode() + b"\n"
        )
        buffer = b""
        while b"\n" not in buffer:
            chunk = sock.recv(65536)
            if not chunk:
                raise CapctlError("the daemon closed the connection")
            buffer += chunk
    except socket.timeout:
        raise CapctlError(f"timed out waiting for the daemon ({timeout}s)") from None
    finally:
        sock.close()

    reply = json.loads(buffer.split(b"\n", 1)[0])
    if not reply.get("ok"):
        error = reply.get("error") or {}
        raise CapctlError(f"{error.get('code', 'error')}: {error.get('message', '?')}")
    return reply.get("result")


def resolve_slot(value: str) -> int:
    """Accept either a slot number or a capability label.

    `capctl send peer:beta "hi"` reads far better than making an agent look up a
    number first, and it is not a weakening of the model: the label is matched
    only against capabilities this container already holds, so it can still
    name nothing it was not given.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        pass

    caps = call("cap.list")
    exact = [c for c in caps if c["label"] == value]
    if len(exact) == 1:
        return int(exact[0]["slot"])
    if len(exact) > 1:
        raise CapctlError(
            f"{value!r} is ambiguous: slots {', '.join(str(c['slot']) for c in exact)}"
        )

    # `beta` should find `peer:beta`, which is what an agent will actually type.
    partial = [
        c for c in caps
        if c["label"].endswith(f":{value}") or c["label"].startswith(f"{value}:")
    ]
    if len(partial) == 1:
        return int(partial[0]["slot"])
    if len(partial) > 1:
        names = ", ".join(c["label"] for c in partial)
        raise CapctlError(f"{value!r} is ambiguous: matches {names}")

    known = ", ".join(c["label"] for c in caps) or "none"
    raise CapctlError(f"no capability called {value!r}; you hold: {known}")


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------


def emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2))
    elif isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value))


def print_caps(caps: list[dict]) -> None:
    if not caps:
        print("(no capabilities)")
        return
    width = max(len(c["label"]) for c in caps)
    print(f"{'SLOT':<5} {'KIND':<10} {'LABEL':<{width}}  RIGHTS")
    for cap in caps:
        detail = cap.get("detail") or {}
        extra = ""
        if cap["kind"] == "container":
            extra = f"  [{detail.get('state', '?')}]"
        elif cap["kind"] == "factory":
            extra = f"  [{detail.get('remaining', 0)} left]"
        print(
            f"{cap['slot']:<5} {cap['kind']:<10} {cap['label']:<{width}}  "
            f"{','.join(cap['rights'])}{extra}"
        )


def print_messages(messages: list[dict]) -> None:
    if not messages:
        print("(no messages)")
        return
    for m in messages:
        payload = m["payload"]
        body = payload if isinstance(payload, str) else json.dumps(payload)
        print(f"[{m['id']}] from {m['from']} ({m['kind']}): {body}")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_whoami(args):
    emit(call("whoami"), args.json)


def cmd_caps(args):
    caps = call("cap.list")
    if args.json:
        emit(caps, True)
    else:
        print_caps(caps)


def cmd_info(args):
    emit(call("cap.info", {"slot": resolve_slot(args.slot)}), True)


def cmd_send(args):
    payload: Any = args.message
    if args.json_payload:
        payload = json.loads(args.message)
    result = call("msg.send", {"slot": resolve_slot(args.slot), "payload": payload})
    if args.json:
        emit(result, True)
    else:
        print(f"delivered to {result['delivered_to']}")


def cmd_recv(args):
    timeout = None if args.wait and args.timeout is None else (args.timeout or 0)
    messages = call(
        "msg.recv", {"timeout": timeout, "limit": args.limit},
        timeout=None if timeout is None else max(timeout + 5, 30),
    )
    if args.json:
        emit(messages, True)
    else:
        print_messages(messages)


def cmd_grant(args):
    result = call("cap.delegate", {
        "target_slot": resolve_slot(args.target),
        "cap_slot": resolve_slot(args.cap),
        "rights": args.rights.split(",") if args.rights else None,
    })
    if args.json:
        emit(result, True)
    else:
        print(
            f"{result['recipient']} now holds it in slot {result['slot']} "
            f"with {','.join(result['rights'])}"
        )


def cmd_revoke(args):
    result = call("cap.revoke", {"slot": resolve_slot(args.slot), "include_self": args.self_too})
    if args.json:
        emit(result, True)
    else:
        holders = ", ".join(result["holders"]) or "nobody"
        print(f"revoked {result['revoked']} mapping(s); affected: {holders}")


def cmd_status(args):
    emit(call("ctr.status", {"slot": resolve_slot(args.slot)}), True)


def cmd_kill(args):
    emit(call("ctr.kill", {"slot": resolve_slot(args.slot), "signal": args.signal}), args.json)


def cmd_interrupt(args):
    emit(call("ctr.signal", {"slot": resolve_slot(args.slot), "signal": args.signal}), args.json)


def cmd_type(args):
    data = args.data if args.data.endswith("\n") else args.data + "\n"
    emit(call("ctr.input", {"slot": resolve_slot(args.slot), "data": data}), args.json)


def cmd_spawn(args):
    if args.config == "-":
        raw = json.loads(sys.stdin.read())
    else:
        with open(args.config) as fh:
            text = fh.read()
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            try:
                import tomllib
            except ImportError:  # pragma: no cover - python < 3.11 in the sandbox
                raise CapctlError("config must be JSON on this python") from None
            raw = tomllib.loads(text)
    if args.name:
        raw["name"] = args.name
    emit(call("ctr.spawn", {"factory_slot": resolve_slot(args.factory), "config": raw}), True)


def cmd_map(args):
    result = call("ds.map", {
        "target_slot": resolve_slot(args.target),
        "ds_slot": resolve_slot(args.dataspace),
        "dest": args.dest,
        "mode": args.mode,
    })
    if args.json:
        emit(result, True)
    else:
        print(f"{result['recipient']} can now read it at {result['path']}")


def cmd_ask(args):
    result = call(
        "ask",
        {
            "question": args.question,
            "context": json.loads(args.context) if args.context else {},
            "block": not args.no_wait,
            "timeout": args.timeout,
        },
        timeout=None if not args.no_wait else 30,
    )
    if args.json:
        emit(result, True)
    else:
        decision = result.get("decision", "pending")
        reason = result.get("reason") or ""
        print(f"{decision}{': ' + reason if reason else ''}")
    # Exit non-zero on denial, so `capctl ask ... && do-the-thing` works.
    if result.get("decision") not in ("allow", None):
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    # --json is accepted on both sides of the subcommand. `capctl caps --json`
    # is what anyone actually types, and argparse would otherwise only accept
    # `capctl --json caps` -- a pointless trap for a tool whose main users are
    # LLM agents reading --help.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="raw JSON output")

    parser = argparse.ArgumentParser(
        prog="capctl",
        parents=[common],
        description="Talk to the capwrap capability kernel from inside a container.",
        epilog="Slot numbers are local to this container; see `capctl caps`.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True, parser_class=_SubParser)

    sub.add_parser("whoami", help="which container am I?").set_defaults(func=cmd_whoami)
    sub.add_parser("caps", help="list my capabilities").set_defaults(func=cmd_caps)

    p = sub.add_parser("info", help="details of one capability")
    p.add_argument("slot")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("send", help="send a message through a capability")
    p.add_argument("slot")
    p.add_argument("message")
    p.add_argument("--json-payload", action="store_true",
                   help="parse the message as JSON before sending")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("recv", help="read my mailbox")
    p.add_argument("--wait", action="store_true", help="block until something arrives")
    p.add_argument("--timeout", type=float, default=None)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_recv)

    p = sub.add_parser("grant", help="delegate one of my capabilities to a peer")
    p.add_argument("target", help="slot of the container to give it to")
    p.add_argument("cap", help="slot of the capability to hand over")
    p.add_argument("--rights", help="comma-separated subset; defaults to all of mine")
    p.set_defaults(func=cmd_grant)

    p = sub.add_parser(
        "revoke",
        help="withdraw everything derived from one of my capabilities",
    )
    p.add_argument("slot")
    p.add_argument("--self-too", action="store_true",
                   help="also drop my own copy, not just what I delegated")
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("status", help="status of a container I hold a capability on")
    p.add_argument("slot")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("kill", help="terminate a container")
    p.add_argument("slot")
    p.add_argument("--signal", type=int, default=15)
    p.set_defaults(func=cmd_kill)

    p = sub.add_parser("interrupt", help="signal a container without killing it")
    p.add_argument("slot")
    p.add_argument("--signal", type=int, default=2)
    p.set_defaults(func=cmd_interrupt)

    p = sub.add_parser("type", help="type at another container's terminal")
    p.add_argument("slot")
    p.add_argument("data")
    p.set_defaults(func=cmd_type)

    p = sub.add_parser("spawn", help="create a container through a factory capability")
    p.add_argument("factory", help="slot of the factory capability")
    p.add_argument("config", help="path to a config (TOML or JSON), or '-' for stdin")
    p.add_argument("--name", help="override the child's name")
    p.set_defaults(func=cmd_spawn)

    p = sub.add_parser("map", help="give a peer access to a dataspace I hold")
    p.add_argument("target", help="slot of the receiving container")
    p.add_argument("dataspace", help="slot of the dataspace")
    p.add_argument("dest", help="name it should appear under in their /shared")
    p.add_argument("--mode", choices=["copy", "map"], default="copy",
                   help="copy duplicates the bytes; map aliases them")
    p.set_defaults(func=cmd_map)

    p = sub.add_parser("ask", help="ask the human operator, and wait for an answer")
    p.add_argument("question")
    p.add_argument("--context", help="JSON object of extra context")
    p.add_argument("--no-wait", action="store_true",
                   help="queue the question without blocking")
    p.add_argument("--timeout", type=float, default=None)
    p.set_defaults(func=cmd_ask)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except CapctlError as exc:
        print(f"capctl: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
