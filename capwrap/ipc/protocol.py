"""The agent-facing wire protocol: newline-delimited JSON over AF_UNIX.

Deliberately boring.  The interesting property is not in the encoding but in how
a request is attributed: the daemon binds one socket per container and remembers
which container each socket belongs to, so the *identity of the caller is a
property of the connection*, never of anything the caller sends.  There is no
token in a request, so there is nothing for an agent to steal, forge or replay.

Errors carry a stable `code` (from `capwrap.errors`) so guest tooling can branch
on the reason without parsing prose.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..errors import CapabilityError, CapwrapError

#: Ops an agent may invoke. Anything else is rejected before it reaches the
#: kernel, so a typo cannot accidentally reach an internal method.
AGENT_OPS = frozenset({
    "whoami",
    "cap.list",
    "cap.info",
    "cap.delegate",
    "cap.revoke",
    "cap.request",
    "msg.send",
    "msg.recv",
    "ctr.status",
    "ctr.kill",
    "ctr.signal",
    "ctr.input",
    "ctr.spawn",
    "ds.map",
    "ask",
})


@dataclass
class Request:
    op: str
    args: dict[str, Any] = field(default_factory=dict)
    id: int = 0

    @classmethod
    def parse(cls, line: bytes | str) -> "Request":
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"malformed JSON: {exc}") from None
        if not isinstance(raw, dict):
            raise ProtocolError("a request must be a JSON object")
        op = raw.get("op")
        if not isinstance(op, str):
            raise ProtocolError("request is missing an 'op'")
        args = raw.get("args") or {}
        if not isinstance(args, dict):
            raise ProtocolError("'args' must be an object")
        return cls(op=op, args=args, id=int(raw.get("id") or 0))

    def encode(self) -> bytes:
        return json.dumps({"id": self.id, "op": self.op, "args": self.args}).encode() + b"\n"


@dataclass
class Response:
    id: int
    ok: bool
    result: Any = None
    code: str | None = None
    message: str | None = None

    @classmethod
    def success(cls, req_id: int, result: Any) -> "Response":
        return cls(id=req_id, ok=True, result=result)

    @classmethod
    def failure(cls, req_id: int, exc: Exception) -> "Response":
        """Turn an exception into a wire error.

        Only `CapabilityError` messages reach an agent verbatim -- those are
        answers to a question it asked.  Everything else is flattened to a
        generic message so that host paths, stack shapes and daemon internals
        do not leak into a sandbox.
        """
        if isinstance(exc, CapabilityError):
            return cls(id=req_id, ok=False, code=exc.code, message=str(exc))
        if isinstance(exc, ProtocolError):
            return cls(id=req_id, ok=False, code="protocol_error", message=str(exc))
        if isinstance(exc, CapwrapError):
            return cls(id=req_id, ok=False, code="capwrap_error", message=str(exc))
        return cls(id=req_id, ok=False, code="internal_error",
                   message="the daemon failed to handle this request")

    def encode(self) -> bytes:
        payload: dict[str, Any] = {"id": self.id, "ok": self.ok}
        if self.ok:
            payload["result"] = self.result
        else:
            payload["error"] = {"code": self.code, "message": self.message}
        return json.dumps(payload).encode() + b"\n"

    @classmethod
    def parse(cls, line: bytes | str) -> "Response":
        raw = json.loads(line)
        if raw.get("ok"):
            return cls(id=raw.get("id", 0), ok=True, result=raw.get("result"))
        err = raw.get("error") or {}
        return cls(
            id=raw.get("id", 0), ok=False,
            code=err.get("code"), message=err.get("message"),
        )


class ProtocolError(CapwrapError):
    """The request was not well-formed."""


#: Requests larger than this are refused rather than buffered, so a container
#: cannot exhaust the daemon's memory by never sending a newline.
MAX_REQUEST_BYTES = 1 << 20
