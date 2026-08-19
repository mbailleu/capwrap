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

from ..daemon import OPERATOR, Daemon
from ..errors import CapabilityError, CapwrapError
from ..kernel.kernel import ROOT
from ..kernel.rights import parse_rights

STATIC = Path(__file__).resolve().parent / "static"


# --------------------------------------------------------------------------
# request bodies
# --------------------------------------------------------------------------


class SendBody(BaseModel):
    target: str
    message: str


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
        """Send a message as the operator.

        The operator holds root capabilities on every container, so this is a
        normal `msg.send` through the kernel rather than a back door -- it is
        audited exactly like an agent's message would be.
        """
        target = daemon.kernel.find_container(body.target)
        if target is None:
            raise HTTPException(404, f"no such container: {body.target}")
        slot = daemon.kernel.root.find(target.oid)
        if slot is None:
            raise HTTPException(500, "the operator holds no capability on that container")
        return daemon.kernel.msg_send(ROOT, slot, body.message)

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
            # Put the terminal back into the modes the program is using before
            # sending anything, or xterm renders alt-screen output in the normal
            # buffer. For a full-screen program, repaint the current screen
            # rather than replaying redraws it has long since superseded.
            if preamble := session.mode_preamble():
                await socket.send_bytes(preamble)
            if session.alternate_screen:
                await socket.send_bytes(session.repaint())
            elif scrollback := session.scrollback():
                await socket.send_bytes(scrollback)

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
