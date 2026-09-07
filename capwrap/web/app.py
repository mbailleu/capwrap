"""The operator web interface.

One pane of glass over every container: the parent tree, every agent's live
terminal, one approval queue covering all of them, a message composer, and the
capability graph with a revoke button.

The daemon and the web server share a process and an event loop.  That is not
laziness -- it means an approval clicked in the browser resolves the very
`asyncio.Future` that a blocked agent is waiting on, with no polling, no second
store of truth, and no chance of the UI and the kernel disagreeing about what a
container is allowed to do.

Assets are vendored under `static/vendor`, so the interface works with no
network at all -- which matters, since the whole point is running agents that
themselves have no network.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import load_config_data
from ..daemon import OPERATOR, Daemon
from ..errors import CapabilityError, CapwrapError
from ..explain import ExplainError
from ..kernel.kernel import ROOT
from ..kernel.rights import parse_rights

STATIC = Path(__file__).resolve().parent / "static"


# --------------------------------------------------------------------------
# request bodies
# --------------------------------------------------------------------------


class SendBody(BaseModel):
    """One message, to one container or to several.

    `target` is kept alongside `targets` because it is the shape every existing
    caller uses; a broadcast is the same operation with more than one name in it.
    """

    message: str
    target: str | None = None
    targets: list[str] | None = None

    def recipients(self) -> list[str]:
        names = list(self.targets or [])
        if self.target:
            names.append(self.target)
        # Order-preserving dedupe: picking a container twice in the composer
        # must not post to it twice.
        return list(dict.fromkeys(names))


class TraceBody(BaseModel):
    enabled: bool


class AddBody(BaseModel):
    """A container to bring into a capwrap that is already running.

    The config arrives already parsed and with its paths resolved, because the
    CLI that sends it is the thing sitting in the directory the config's relative
    paths are written against.
    """

    config: dict
    start: bool = True


class ApprovalBody(BaseModel):
    decision: str
    reason: str = ""
    #: Only for a capability request: grant narrower rights than were asked for.
    rights: list[str] | None = None


class RevokeBody(BaseModel):
    container: str
    slot: int
    include_self: bool = True


class GrantBody(BaseModel):
    """The operator granting one container a capability on another."""

    holder: str
    target_container: str
    rights: list[str]
    label: str | None = None


class InputBody(BaseModel):
    data: str


class ResizeBody(BaseModel):
    cols: int
    rows: int


def create_app(daemon: Daemon) -> FastAPI:
    app = FastAPI(title="capwrap", docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.exception_handler(CapwrapError)
    async def _capwrap_error(_request, exc: CapwrapError):
        status = 403 if isinstance(exc, CapabilityError) else 400
        return JSONResponse({"error": str(exc)}, status_code=status)

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------

    @app.get("/api/overview")
    async def overview() -> dict:
        return daemon.overview()

    @app.get("/api/instance")
    async def instance() -> dict:
        """What this capwrap is called, for the page title and the header."""
        return {"name": daemon.instance_name}

    @app.get("/api/containers")
    async def containers() -> list[dict]:
        return [c.status() for c in daemon.containers.values()]

    @app.get("/api/containers/{name}")
    async def container(name: str) -> dict:
        c = daemon.containers.get(name)
        if c is None:
            raise HTTPException(404, f"no such container: {name}")
        return {
            **c.status(),
            "config": {
                "command": c.config.runtime.command,
                "cwd": c.config.runtime.cwd,
                "network": c.config.sandbox.network,
                "mounts": [
                    {"dest": m.dest, "mode": m.mode,
                     "src": str(m.src) if m.src else None,
                     "branch": m.branch}
                    for m in c.config.mounts
                ],
            },
            "caps": [cap.to_dict() for cap in daemon.kernel.cap_list(name)],
            "mailbox": [m.to_dict() for m in daemon.mailboxes.get(name).recent(50)],
            # The screen snapshot, not the byte log: the overview tiles need
            # what a TUI currently *shows*, which a replayed byte stream cannot
            # give you without a terminal emulator on the client side.
            "session": (
                {**c.session.status(), "screen": c.session.snapshot().to_dict()}
                if c.session else None
            ),
        }

    @app.get("/api/containers/{name}/screen")
    async def screen(name: str, rows: int = 12) -> dict:
        """The tail of a container's screen, for the all-agents overview.

        Separate from the container detail endpoint so refreshing a grid of
        tiles does not also pull every container's capability table and mailbox.
        """
        c = daemon.containers.get(name)
        if c is None:
            raise HTTPException(404, f"no such container: {name}")
        if c.session is None:
            return {"container": name, "running": False, "styled": [], "lines": []}
        snap = c.session.snapshot(tail=max(1, min(rows, 200)))
        return {"container": name, "running": c.running, **snap.to_dict()}

    @app.get("/api/screens")
    async def screens(rows: int = 12) -> dict:
        """Every running container's screen tail, in one response.

        The overview polls this. One request rather than one per container: on a
        Pi with several agents, a fan-out of requests every tick costs more in
        connection churn than the payload is worth, and they arrive interleaved.
        """
        limit = max(1, min(rows, 200))
        return {
            "screens": [
                {"container": c.name, "running": True,
                 **c.session.snapshot(tail=limit).to_dict()}
                for c in daemon.containers.values()
                if c.running and c.session is not None
            ],
        }

    @app.get("/api/boards")
    async def boards(limit: int = 100) -> dict:
        """Every board and its recent posts.

        The operator sees all of them regardless of who created one: the root
        capability is the ancestor of every mapping in the system, and a board
        that agents are coordinating on is exactly what a human overseeing them
        needs to be able to read.
        """
        return {
            "boards": [
                {
                    **board.describe(),
                    "holders": daemon.kernel.board_holders(board.oid),
                    "recent": board.posts[-max(1, limit):],
                }
                for board in daemon.kernel.boards()
            ],
        }

    @app.get("/api/caps/{name}")
    async def caps(name: str) -> list[dict]:
        if name not in daemon.kernel.tasks:
            raise HTTPException(404, f"no such task: {name}")
        return [c.to_dict() for c in daemon.kernel.cap_list(name)]

    @app.get("/api/capgraph")
    async def capgraph() -> dict:
        return daemon.kernel.cap_graph()

    @app.get("/api/audit")
    async def audit(limit: int = 100, actor: str | None = None,
                    denied: bool = False) -> list[dict]:
        return daemon.audit.tail(limit=limit, actor=actor, denied_only=denied)

    @app.get("/api/inbox")
    async def inbox(limit: int = 100) -> list[dict]:
        return [m.to_dict() for m in daemon.mailboxes.get(OPERATOR).recent(limit)]

    # ------------------------------------------------------------------
    # control
    # ------------------------------------------------------------------

    @app.post("/api/containers")
    async def add(body: AddBody) -> dict:
        """Register a new container, and start it unless told not to.

        The point of a running capwrap is that agents come and go: a review
        needs a reviewer, a build needs a tester, and stopping everything to
        restart with one more config file wastes whatever the others were in the
        middle of.

        It registers under the *operator*, not under any existing container, so
        this is an authority grant from the human rather than a spawn -- an agent
        wanting a child still goes through a factory capability and its quota.
        """
        config = load_config_data(dict(body.config), base_dir=Path.cwd(),
                                  origin="operator:add")
        if config.name in daemon.containers:
            raise HTTPException(409, f"a container named {config.name} already exists")

        container = daemon.register(config)
        # Both directions: the newcomer's peer references resolve, and any
        # existing container that named it before it existed gets its capability
        # filled in now rather than never.
        daemon.link_all_peers()
        if body.start:
            await daemon.start(config.name)
        return {**container.status(), "started": body.start}

    @app.post("/api/containers/{name}/start")
    async def start(name: str) -> dict:
        container = await daemon.start(name)
        return container.status()

    @app.post("/api/containers/{name}/stop")
    async def stop(name: str) -> dict:
        code = await daemon.stop(name)
        return {"container": name, "exit_code": code}

    @app.post("/api/containers/{name}/signal")
    async def signal(name: str, sig: int = 2) -> dict:
        daemon.signal_container(name, sig)
        return {"container": name, "signal": sig}

    @app.delete("/api/containers/{name}")
    async def destroy(
        name: str, remove_state: bool = False, force: bool = False
    ) -> dict:
        """Dismiss a container: forget it entirely, tree entry included.

        `remove_state=true` also deletes its host-side directory -- overlay
        writes, private copies, and the git worktree with whatever the agent
        committed. Off by default, because that work usually outlives the
        container that produced it.
        """
        return await daemon.destroy(name, remove_state=remove_state, force=force)

    @app.post("/api/containers/dismiss-finished")
    async def dismiss_finished(remove_state: bool = False) -> dict:
        """Clear away every container that has already exited."""
        dismissed = []
        for name in daemon.dismissable():
            with contextlib.suppress(CapwrapError):
                await daemon.destroy(name, remove_state=remove_state)
                dismissed.append(name)
        return {"dismissed": dismissed}

    @app.post("/api/containers/{name}/input")
    async def write_input(name: str, body: InputBody) -> dict:
        daemon.write_input(name, body.data)
        return {"wrote": len(body.data)}

    @app.post("/api/send")
    async def send(body: SendBody) -> dict:
        """Send a message as the operator, to one container or to several.

        The operator holds root capabilities on every container, so this is a
        normal `msg.send`/`msg.broadcast` through the kernel rather than a back
        door -- it is audited exactly like an agent's message would be.
        """
        names = body.recipients()
        if not names:
            raise HTTPException(400, "name at least one container to send to")

        slots: list[int] = []
        for name in names:
            target = daemon.kernel.find_container(name)
            if target is None:
                raise HTTPException(404, f"no such container: {name}")
            slot = daemon.kernel.root.find(target.oid)
            if slot is None:
                raise HTTPException(
                    500, f"the operator holds no capability on {name}"
                )
            slots.append(slot)

        if len(slots) == 1:
            return daemon.kernel.msg_send(ROOT, slots[0], body.message)
        return daemon.kernel.msg_broadcast(ROOT, slots, body.message)

    # ------------------------------------------------------------------
    # message tracing
    # ------------------------------------------------------------------

    @app.get("/api/trace")
    async def trace_state() -> dict:
        return daemon.trace_state()

    @app.post("/api/trace")
    async def set_trace(body: TraceBody) -> dict:
        """Turn the inter-container message trace on or off.

        Off by default and cleared when turned off: a trace keeps whole message
        payloads, which is the agents' working content rather than metadata.
        """
        return daemon.set_message_trace(body.enabled)

    @app.get("/api/messages")
    async def traced_messages(limit: int = 200) -> dict:
        return {
            **daemon.trace_state(),
            "messages": daemon.traced_messages(limit=limit),
        }

    @app.delete("/api/messages")
    async def clear_trace() -> dict:
        """Drop what has been recorded so far, leaving tracing on."""
        daemon.message_trace.clear()
        return daemon.trace_state()

    # ------------------------------------------------------------------
    # approvals
    # ------------------------------------------------------------------

    @app.get("/api/approvals")
    async def approvals() -> list[dict]:
        return daemon.pending_approvals()

    @app.post("/api/approvals/{approval_id}")
    async def resolve(approval_id: int, body: ApprovalBody) -> dict:
        if body.decision not in ("allow", "deny"):
            raise HTTPException(400, "decision must be 'allow' or 'deny'")
        if not daemon.resolve_approval(
            approval_id, body.decision, body.reason, body.rights
        ):
            raise HTTPException(404, "no such pending approval")
        return {"id": approval_id, "decision": body.decision}

    @app.post("/api/approvals/{approval_id}/explain")
    async def explain(approval_id: int) -> dict:
        """What would this request actually do?

        Advisory, and labelled as such wherever it is shown: it is one model's
        reading of another model's request. It decides nothing -- the operator
        still clicks the button.
        """
        pending = daemon.approvals.get(approval_id)
        if pending is None or pending.future.done():
            raise HTTPException(404, "no such pending approval")

        container = daemon.containers.get(pending.container)
        detail = None
        if container is not None:
            detail = {"config": {
                "agent": container.config.runtime.agent,
                "model": container.config.runtime.model,
                "command": container.config.runtime.command,
                "cwd": container.config.runtime.cwd,
                "network": container.config.sandbox.network,
                "mounts": [
                    {"dest": m.dest, "mode": m.mode} for m in container.config.mounts
                ],
            }}

        try:
            result = await daemon.explainer.explain(pending.to_dict(), detail)
        except ExplainError as exc:
            raise HTTPException(503, str(exc)) from None
        daemon.audit.record(
            OPERATOR, "approval.explain", allowed=True,
            target=pending.container, detail={"model": result["model"]},
        )
        return result

    # ------------------------------------------------------------------
    # capability administration
    # ------------------------------------------------------------------

    @app.post("/api/caps/revoke")
    async def revoke(body: RevokeBody) -> dict:
        """Revoke a capability, and everything derived from it.

        Available for any container's slot because the operator's root
        capability is the ancestor of every mapping in the system.
        """
        return daemon.kernel.cap_revoke(
            body.container, body.slot, include_self=body.include_self
        )

    @app.post("/api/caps/grant")
    async def grant(body: GrantBody) -> dict:
        """Hand a container a new capability on another container, at runtime."""
        return daemon.kernel.operator_grant(
            body.holder, "container", body.target_container,
            parse_rights(body.rights), label=body.label,
        )

    # ------------------------------------------------------------------
    # live streams
    # ------------------------------------------------------------------

    @app.websocket("/ws/events")
    async def ws_events(socket: WebSocket) -> None:
        """Everything happening across the whole system, as one stream."""
        await socket.accept()
        queue = daemon.subscribe_events()
        try:
            await socket.send_json({"event": "overview", **daemon.overview()})
            while True:
                event = await queue.get()
                await socket.send_json(event)
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            daemon.unsubscribe_events(queue)

    @app.websocket("/ws/terminal/{name}")
    async def ws_terminal(socket: WebSocket, name: str) -> None:
        """A container's terminal, both directions.

        Raw PTY bytes out, keystrokes in.  The scrollback is replayed first so a
        browser that connects late still sees the session rather than a blank
        screen waiting for the next redraw.
        """
        await socket.accept()
        container = daemon.containers.get(name)
        if container is None or container.session is None:
            await socket.send_json({"type": "error", "message": f"{name} is not running"})
            await socket.close()
            return

        session = container.session
        loop = asyncio.get_running_loop()
        outbound: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)

        def on_output(data: bytes) -> None:
            # Called from the reader callback; hop back onto the loop safely.
            with contextlib.suppress(asyncio.QueueFull):
                outbound.put_nowait(data)

        unsubscribe = session.subscribe(on_output)

        async def pump() -> None:
            while True:
                data = await outbound.get()
                await socket.send_bytes(data)

        pump_task = loop.create_task(pump())
        try:
            # Replay everything retained, in the order the terminal originally
            # received it. That is what fills the browser's scrollback: sending
            # only the current frame -- which is all a full-screen program's
            # repaint can give you -- left the operator able to see what an agent
            # is doing now and nothing of what it did to get there, and threw the
            # session away again every time they clicked another container.
            if history := session.scrollback():
                if session.truncated:
                    await socket.send_bytes(
                        b"\x1b[90m[capwrap: earlier output has aged out of the "
                        b"buffer; raise CAPWRAP_SCROLLBACK_BYTES to keep more]"
                        b"\x1b[0m\r\n"
                    )
                await socket.send_bytes(history)

            # Then re-assert the program's modes and, for a full-screen one, its
            # current frame. The replay above may have been trimmed part-way
            # through a redraw, so this is what guarantees the live screen is
            # right regardless of where the ring happened to start.
            if preamble := session.mode_preamble():
                await socket.send_bytes(preamble)
            if session.alternate_screen:
                await socket.send_bytes(session.repaint())

            while True:
                message = await socket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if (text := message.get("text")) is not None:
                    await _handle_terminal_message(session, text)
                elif (data := message.get("bytes")) is not None:
                    session.write(data)
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            unsubscribe()
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task

    async def _handle_terminal_message(session: Any, text: str) -> None:
        """Text frames are control JSON; keystrokes arrive as binary."""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            session.write(text)
            return
        kind = payload.get("type")
        if kind == "input":
            session.write(payload.get("data", ""))
        elif kind == "resize":
            session.resize(int(payload.get("cols", 80)), int(payload.get("rows", 24)))

    # ------------------------------------------------------------------
    # static assets
    # ------------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app
