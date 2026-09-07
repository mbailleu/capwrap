"""The daemon: the kernel's hands.

`CapKernel` decides what is permitted; `Daemon` is what actually happens as a
result.  It owns the sandboxes, the PTYs, the mailboxes and the per-container
sockets, and it implements the kernel's `Hooks` protocol.

The socket layout is the security-relevant part.  Each container gets its own
`AF_UNIX` socket, bound on the host at ``<state>/containers/<name>/agent.sock``
and bind-mounted into the sandbox at ``/run/capwrap.sock``.  When a connection
arrives, the daemon already knows which container it came from, because it knows
which socket accepted it.  Nothing in the request establishes identity, so there
is nothing an agent can lie about.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Any

from .config import ContainerConfig, load_config_data
from .errors import CapabilityError, CapwrapError, SandboxError
from .explain import Explainer
from .guest import ed25519
from .ipc.mailbox import MailboxRegistry, write_inbox_file
from .ipc.protocol import AGENT_OPS, MAX_REQUEST_BYTES, ProtocolError, Request, Response
from .kernel.audit import AuditLog
from .kernel.kernel import ROOT, CapKernel
from .kernel.objects import ContainerObject
from .kernel import signing
from .kernel.policy import contains as policy_contains
from .kernel.rights import VALID_RIGHTS, Rights, parse_rights
from .net.proxy import NetProxy
from .paths import ContainerPaths, db_path, force_rmtree, state_root
from .runtime import bwrap as bwrap_mod
from .runtime import fsprep, mapper as mapper_mod
from .runtime import probe
from .runtime.supervisor import PtySession

OPERATOR = "operator"

#: How many inter-container messages the debug trace keeps.  Bounded because a
#: trace holds whole payloads, which are the agents' working content.
MESSAGE_TRACE_LIMIT = 2000

#: What an agent gets if it requests a capability without naming rights.
DEFAULT_REQUEST_RIGHTS = {
    "container": Rights.SEND | Rights.INSPECT,
    "dataspace": Rights.READ,
    "factory": Rights.CREATE,
    "net_rule": Rights.CONNECT,
}


class PendingApproval:
    """A question from an agent, waiting on the operator.

    The agent's request stays blocked on the future until someone answers in the
    web UI.  That is what makes the approval flow work for a hook that has to
    return a decision synchronously.
    """

    _next_id = 1

    def __init__(self, container: str, question: str, context: dict) -> None:
        self.id = PendingApproval._next_id
        PendingApproval._next_id += 1
        self.container = container
        self.question = question
        self.context = context
        self.created_at = time.time()
        self.future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        #: Set once an outcome has been recorded, so answering and abandoning
        #: cannot both stamp the inbox entry.
        self.closed = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "container": self.container,
            "question": self.question,
            "context": self.context,
            "created_at": self.created_at,
            "resolved": self.future.done(),
        }


class _RequestTooLong(Exception):
    """A peer sent more than MAX_REQUEST_BYTES without a newline."""


class _Framed:
    """Newline framing over a StreamReader, plus a raceable end-of-stream.

    `StreamReader.readuntil` cannot be raced against an in-flight request: the
    losing read has already taken bytes off the stream and nothing hands them
    back.  That race is exactly what is needed here, because an agent that dies
    while blocked on an approval must not leave its question sitting in the
    operator's queue.  So the buffer lives in this object, where whatever
    arrives *during* a request is simply kept for the request after it.
    """

    def __init__(self, reader: asyncio.StreamReader, limit: int) -> None:
        self._reader = reader
        self._limit = limit
        self._buffer = bytearray()
        self.eof = False

    async def readline(self) -> bytes | None:
        """The next request line, or None once the peer is finished."""
        while True:
            cut = self._buffer.find(b"\n")
            if cut >= 0:
                line = bytes(self._buffer[: cut + 1])
                del self._buffer[: cut + 1]
                return line
            if len(self._buffer) > self._limit:
                raise _RequestTooLong
            if self.eof or not await self._fill():
                return None

    async def wait_closed(self) -> None:
        """Resolve when the peer goes away; keep anything it sends first."""
        while not self.eof:
            if len(self._buffer) > self._limit:
                # Stop reading rather than buffer without bound. The next
                # `readline` refuses the request; until then this simply never
                # reports a close, which is the safe direction to be wrong in.
                await asyncio.Event().wait()
            await self._fill()

    async def _fill(self) -> bool:
        try:
            chunk = await self._reader.read(65536)
        except (ConnectionResetError, asyncio.IncompleteReadError, OSError):
            chunk = b""
        if not chunk:
            self.eof = True
            return False
        self._buffer += chunk
        return True


class Container:
    """A registered container and, when running, its sandbox."""

    def __init__(self, config: ContainerConfig, obj: ContainerObject) -> None:
        self.config = config
        self.obj = obj
        self.paths = ContainerPaths(config.name)
        self.session: PtySession | None = None
        self.prepared: fsprep.PreparedFs | None = None
        self.server: asyncio.AbstractServer | None = None
        #: Only for a container with network rules; None means no network.
        self.proxy: NetProxy | None = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def running(self) -> bool:
        return self.session is not None and self.session.running

    # -- the bits a Mapper needs (see runtime/mapper.py) ------------------

    @property
    def shared_dir(self) -> Path:
        return self.paths.shared

    @property
    def pid(self) -> int | None:
        return self.session.pid if self.running else None

    def status(self) -> dict:
        return {
            **self.obj.describe(),
            "running": self.running,
            "mounts": self.obj.mounts,
            "session": self.session.status() if self.session else None,
        }


class Daemon:
    """Owns every container, and implements the kernel's effects."""

    def __init__(
        self,
        audit_path: Path | None = None,
        trace_messages: bool = False,
        instance_name: str = "",
    ) -> None:
        #: What this whole capwrap is *for* -- "FastPath HashTable", say. Purely
        #: a label, but a load-bearing one: several of these run at once on
        #: different ports, and without it every browser tab is called "capwrap"
        #: and you cannot tell which team of agents you are looking at.
        self.instance_name = instance_name.strip()
        self.state = state_root()
        self.state.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLog(audit_path if audit_path is not None else db_path())
        self.kernel = CapKernel(audit=self.audit, hooks=self)
        self.mailboxes = MailboxRegistry()
        self.containers: dict[str, Container] = {}
        self.approvals: dict[int, PendingApproval] = {}
        #: Opt-in, and off by default: a trace holds whole message payloads,
        #: which are the agents' working content, not metadata. The audit log
        #: records that a message was sent; this records what was in it.
        self.trace_messages = bool(trace_messages)
        self.message_trace: deque[dict] = deque(maxlen=MESSAGE_TRACE_LIMIT)
        #: Explanations of pending requests, produced on demand.
        self.explainer = Explainer()
        #: Operator-inbox entries for questions, so a resolved one can be marked.
        #: The inbox is history and survives a reload; without this an answered
        #: question comes back looking like an open one.
        self._question_messages: dict[int, Any] = {}
        self._events: list[asyncio.Queue] = []
        self._overlay_backend: str | None = None
        self._bwrap: str | None = None
        #: Chosen on first use, by trying it -- see runtime/mapper.select.
        self._mapper = None
        self.mapper_detail = ""

    # ==================================================================
    # host capability discovery
    # ==================================================================

    def _ensure_host_ready(self) -> None:
        if self._bwrap is not None:
            return
        report = probe.run_all()
        check = report.get("bwrap can create namespaces")
        if check is None or not check.ok or not report.bwrap:
            raise SandboxError(
                f"cannot sandbox on this host: {check.detail if check else 'no bwrap'}"
                + (f" -- {check.hint}" if check and check.hint else "")
            )
        # The one the probe actually got a namespace out of, which is not
        # necessarily the first on PATH.
        self._bwrap = report.bwrap
        self._overlay_backend = report.overlay_backend

    # ==================================================================
    # container lifecycle
    # ==================================================================

    def register(self, config: ContainerConfig, parent: str = ROOT) -> Container:
        """Register a container with the kernel without starting it."""
        config.validate_sources()
        obj = self.kernel.register_container(config, parent=parent)
        container = Container(config, obj)
        self._mint_signing_key(container)
        self.containers[config.name] = container
        self.mailboxes.get(config.name)
        self._emit("container.registered", {"container": config.name})
        return container

    def _mint_signing_key(self, container: Container) -> None:
        """Give a container a signing identity, and keep only its public half.

        The seed is written into the container's own private directory and bound
        into its sandbox alone; the public key goes on the kernel object, where
        anyone reading a board can find it. The daemon does not keep the seed in
        memory, which is not a strong claim -- it wrote the file and could read
        it back -- but it does mean the ordinary path never has it.

        A key is minted per registration rather than per start, so a container
        that is stopped and started again keeps the identity its earlier posts
        were signed with.
        """
        paths = container.paths
        paths.root.mkdir(parents=True, exist_ok=True)
        if paths.signing_key.exists():
            seed = paths.signing_key.read_bytes()
        else:
            seed = ed25519.generate_seed()
            paths.signing_key.write_bytes(seed)
        os.chmod(paths.signing_key, 0o600)
        container.obj.public_key = ed25519.public_key(seed).hex()

    def link_all_peers(self) -> None:
        """Resolve peer capabilities that referred to containers registered later."""
        for container in self.containers.values():
            self.kernel.link_peers(container.config)

    async def start(self, name: str) -> Container:
        """Prepare the filesystem, bind the control socket, launch the sandbox."""
        container = self._get(name)
        if container.running:
            return container

        self._ensure_host_ready()
        assert self._bwrap is not None

        needs_overlay = any(m.mode == "overlay" for m in container.config.mounts)
        backend = self._overlay_backend or "kernel"
        if needs_overlay and self._overlay_backend is None:
            raise SandboxError("no overlay backend available; run `capwrap doctor`")

        container.prepared = fsprep.prepare(
            container.config, container.paths, overlay_backend=backend
        )
        container.obj.mounts = fsprep.describe(container.prepared)

        # Bind the container's sockets *before* building the argv, because
        # `build_argv` only mounts them if the files already exist.
        container.server = await self._serve_container(container)
        await self._serve_proxy(container)

        argv = bwrap_mod.build_argv(
            container.config,
            container.prepared,
            container.paths,
            bwrap=self._bwrap,
            guest_tools=Path(__file__).resolve().parent / "guest",
        )

        # The container's environment, not the daemon's: handed to bwrap as its
        # own environ so that tokens never appear in argv.
        session = PtySession(
            name=name, argv=argv,
            env=bwrap_mod.build_env(container.config),
        )
        session.start()
        container.session = session
        container.obj.state = "running"
        container.obj.pid = session.pid
        container.obj.exit_code = None

        self.audit.record(ROOT, "container.start", allowed=True, target=name,
                          detail={"pid": session.pid})
        self._emit("container.started", {"container": name, "pid": session.pid})

        asyncio.ensure_future(self._watch_exit(container))
        return container

    async def _watch_exit(self, container: Container) -> None:
        assert container.session is not None
        code = await container.session.wait()
        container.obj.state = "exited"
        container.obj.exit_code = code
        container.obj.pid = None
        self.abandon_approvals(container.name, "the container exited")
        self.audit.record(ROOT, "container.exit", allowed=True, target=container.name,
                          detail={"exit_code": code})
        self._emit("container.exited", {"container": container.name, "exit_code": code})

    async def stop(self, name: str, grace: float = 5.0) -> int | None:
        container = self._get(name)
        if container.session is None:
            return None
        code = await container.session.terminate(grace=grace)
        return code

    async def destroy(
        self, name: str, remove_state: bool = False, force: bool = False
    ) -> dict:
        """Dismiss a container: stop it, revoke its authority, forget it.

        A running container is refused unless `force`, so a mis-click in the tree
        cannot kill an agent that is in the middle of something.
        """
        container = self.containers.get(name)
        if container is None:
            raise CapwrapError(f"no such container: {name}")
        if container.running and not force:
            raise CapwrapError(
                f"{name} is still running; stop it first, or dismiss with force"
            )
        await self.stop(name)

        if container.server is not None:
            container.server.close()
            # Bounded. `wait_closed` also waits for in-flight handler tasks, and
            # an agent that opened the control socket and never closed it would
            # otherwise park the daemon here for good -- which on the way out of
            # `capwrap up` means a process that will not exit.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(container.server.wait_closed(), timeout=2.0)
        if container.proxy is not None:
            await container.proxy.stop()
            container.proxy = None
        if container.prepared is not None:
            container.prepared.cleanup()

        self.abandon_approvals(name, "the container was dismissed")
        result = self.kernel.forget_container(name)
        self.mailboxes.drop(name)
        del self.containers[name]

        if remove_state:
            force_rmtree(container.paths.root)
        self._emit("container.destroyed", {"container": name})
        return {**result, "state_removed": remove_state}

    def dismissable(self) -> list[str]:
        """Containers that have finished and could be cleared away."""
        return sorted(
            name for name, c in self.containers.items() if not c.running
        )

    def _get(self, name: str) -> Container:
        container = self.containers.get(name)
        if container is None:
            raise CapwrapError(f"no such container: {name}")
        return container

    # ==================================================================
    # per-container socket server
    # ==================================================================

    async def _serve_container(self, container: Container) -> asyncio.AbstractServer:
        """Bind this container's control socket.

        The socket path is what identifies the caller, so the handler closes over
        the container name rather than reading it from any request.
        """
        socket_path = container.paths.socket
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            socket_path.unlink()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            await self._handle_connection(container.name, reader, writer)

        server = await asyncio.start_unix_server(handle, path=str(socket_path))
        # Only the owner may connect. The sandbox runs as the same uid, so this
        # keeps other local users out without getting in the agent's way.
        os.chmod(socket_path, 0o600)
        return server

    async def _serve_proxy(self, container: Container) -> None:
        """Bind this container's network proxy, if it has any network rules.

        A container with none gets no socket at all -- not an empty allowlist.
        The difference matters: there is then nothing in its filesystem that even
        gestures at an outside world.
        """
        if not container.config.proxied_network:
            return
        proxy = NetProxy(
            container.name,
            decide=lambda name, host, port: self.kernel.net_allows(name, host, port),
            on_event=lambda record: self._emit("net.request", record),
        )
        await proxy.start(container.paths.proxy_socket)
        container.proxy = proxy

    async def _handle_connection(
        self, actor: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        framed = _Framed(reader, MAX_REQUEST_BYTES)
        try:
            while True:
                try:
                    line = await framed.readline()
                except _RequestTooLong:
                    await self._reply(writer, Response(
                        id=0, ok=False, code="protocol_error",
                        message="request exceeds the maximum size",
                    ))
                    return
                if line is None:
                    return
                if not line.strip():
                    continue
                if len(line) > MAX_REQUEST_BYTES:
                    await self._reply(writer, Response(
                        id=0, ok=False, code="protocol_error",
                        message="request exceeds the maximum size",
                    ))
                    return

                response = await self._serve(actor, line, framed)
                if response is None:
                    return          # the caller hung up mid-request
                await self._reply(writer, response)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _serve(
        self, actor: str, line: bytes, framed: _Framed
    ) -> Response | None:
        """Handle one request, giving it up if the caller disappears.

        The requests that take real time are the blocking ones -- `ask` and
        `cap.request` -- and those are precisely the ones that leave something
        behind.  A hook whose agent is killed mid-prompt would otherwise leave
        its question in the operator's queue with nothing on the far end:
        answering it does nothing, and it is still sitting there when the
        browser next reconnects, which is what makes stale approvals look like
        a UI bug rather than a daemon one.
        """
        dispatch = asyncio.ensure_future(self._dispatch(actor, line))
        hung_up = asyncio.ensure_future(framed.wait_closed())
        try:
            await asyncio.wait(
                {dispatch, hung_up}, return_when=asyncio.FIRST_COMPLETED
            )
            if dispatch.done():
                # `_dispatch` turns every failure into a Response, so the only
                # way it ends without one is cancellation.
                return None if dispatch.cancelled() else dispatch.result()
            dispatch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await dispatch
            return None
        finally:
            hung_up.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hung_up

    async def _reply(self, writer: asyncio.StreamWriter, response: Response) -> None:
        writer.write(response.encode())
        with contextlib.suppress(Exception):
            await writer.drain()

    async def _dispatch(self, actor: str, line: bytes) -> Response:
        try:
            request = Request.parse(line)
        except ProtocolError as exc:
            return Response.failure(0, exc)

        if request.op not in AGENT_OPS:
            return Response.failure(
                request.id, ProtocolError(f"unknown operation {request.op!r}")
            )

        try:
            result = await self._invoke(actor, request)
            return Response.success(request.id, result)
        except CapabilityError as exc:
            return Response.failure(request.id, exc)
        except CapwrapError as exc:
            return Response.failure(request.id, exc)
        except Exception as exc:  # noqa: BLE001
            self.audit.record(
                actor, request.op, allowed=False, detail=f"internal error: {exc!r}"
            )
            return Response.failure(request.id, exc)

    async def _invoke(self, actor: str, request: Request) -> Any:
        """Route one agent request to the kernel."""
        op, args = request.op, request.args
        k = self.kernel

        if op == "whoami":
            obj = k.find_container(actor)
            return {
                "container": actor,
                "caps": len(k.tasks[actor]),
                "public_key": obj.public_key if obj else "",
                "fingerprint": signing.fingerprint(obj.public_key) if obj else "",
            }
        if op == "cap.list":
            return [c.to_dict() for c in k.cap_list(actor)]
        if op == "cap.info":
            return k.cap_info(actor, int(args["slot"])).to_dict()
        if op == "cap.delegate":
            return k.cap_delegate(
                actor, int(args["target_slot"]), int(args["cap_slot"]),
                args.get("rights"),
            )
        if op == "cap.revoke":
            return k.cap_revoke(
                actor, int(args["slot"]), bool(args.get("include_self", False))
            )
        if op == "msg.send":
            return k.msg_send(
                actor, int(args["slot"]), args.get("payload"),
                signature=str(args.get("signature") or ""),
            )
        if op == "msg.broadcast":
            slots = args.get("slots")
            if not isinstance(slots, list) or not slots:
                raise ProtocolError("'slots' must be a non-empty list")
            return k.msg_broadcast(
                actor, [int(s) for s in slots], args.get("payload"),
                signature=str(args.get("signature") or ""),
            )
        if op == "board.create":
            return k.board_create(
                actor, int(args["factory_slot"]), str(args.get("topic", ""))
            )
        if op == "board.post":
            return k.board_post(
                actor, int(args["slot"]), args.get("payload"),
                signature=str(args.get("signature") or ""),
            )
        if op == "board.read":
            return k.board_read(
                actor, int(args["slot"]),
                since=int(args.get("since", 0)), limit=int(args.get("limit", 50)),
            )
        if op == "msg.recv":
            box = self.mailboxes.get(actor)
            timeout = args.get("timeout", 0)
            messages = await box.receive(
                timeout=None if timeout is None else float(timeout),
                limit=int(args.get("limit", 10)),
            )
            return [m.to_dict() for m in messages]
        if op == "ctr.status":
            return k.ctr_status(actor, int(args["slot"]))
        if op == "ctr.kill":
            return k.ctr_kill(actor, int(args["slot"]), int(args.get("signal", 15)))
        if op == "ctr.signal":
            return k.ctr_signal(actor, int(args["slot"]), int(args.get("signal", 2)))
        if op == "ctr.output":
            return k.ctr_output(actor, int(args["slot"]), int(args.get("rows", 24)))
        if op == "ctr.input":
            return k.ctr_input(actor, int(args["slot"]), str(args["data"]))
        if op == "ctr.spawn":
            config = self._config_from_agent(args.get("config") or {}, actor)
            await self._check_permission_escalation(actor, config)
            return k.ctr_spawn(actor, int(args["factory_slot"]), config)
        if op == "ds.map":
            return k.ds_map(
                actor, int(args["target_slot"]), int(args["ds_slot"]),
                str(args["dest"]), str(args.get("mode", "copy")),
            )
        if op == "cap.request":
            return await self.request_capability(
                actor,
                kind=str(args.get("kind", "container")),
                target=str(args.get("target", "")),
                rights=args.get("rights") or [],
                quota=int(args.get("quota", 1)),
                reason=str(args.get("reason", "")),
                timeout=args.get("timeout"),
            )
        if op == "ask":
            return await self.ask_operator(
                actor, str(args["question"]), args.get("context") or {},
                blocking=bool(args.get("block", True)),
                timeout=args.get("timeout"),
            )
        raise ProtocolError(f"unhandled operation {op!r}")

    def _config_from_agent(self, raw: dict, actor: str) -> ContainerConfig:
        """Validate a config an agent submitted, and pin down what it may set.

        A spawning agent controls its child's *shape*, but the child's authority
        still comes from the kernel's delegation check.  What is forced here is
        the things that would otherwise let a child escape the parent's own
        confinement.
        """
        if not isinstance(raw, dict):
            raise ProtocolError("'config' must be an object")
        config = load_config_data(dict(raw), base_dir=Path.cwd(), origin=f"{actor}:spawn")

        parent = self.containers.get(actor)
        if parent is not None and not parent.config.sandbox.network:
            # A container without network must not be able to spawn one with it.
            config.sandbox.network = False
        config.validate_sources()
        return config

    async def _check_permission_escalation(
        self, actor: str, config: ContainerConfig
    ) -> None:
        """Stop a container handing its child broader native permissions than it has.

        Spawning is governed by a factory capability, but that says nothing about
        the *tool permissions* inside the child. Without this check, a container
        confined to `Read` could spawn a child with `Bash(*)` and act through it,
        so the capability model would be sound while the thing it is protecting
        walked out the back.

        The child's policy is compared against the parent's envelope. Narrowing
        -- fewer allows, more denies, allow downgraded to ask -- is provably safe
        and passes silently. Only a genuine widening reaches the operator, and
        even that can be pre-authorised by giving the parent an explicit
        `permission_envelope` wider than its own permissions.
        """
        parent = self.containers.get(actor)
        if parent is None:
            return  # operator-launched: nothing to escalate from

        envelope = parent.config.runtime.envelope_policy()
        child_policy = config.runtime.permissions.to_policy()

        # A child that requests nothing inherits the parent's rules, which by
        # definition cannot escalate.
        if child_policy.is_empty:
            config.runtime.permissions = parent.config.runtime.permissions
            return

        diff = policy_contains(envelope, child_policy)
        if diff.ok:
            self.audit.record(
                actor, "policy.check", allowed=True, target=config.name,
                detail="child policy is within the parent's envelope",
            )
            return

        reasons = diff.reasons()
        self.audit.record(
            actor, "policy.escalation", allowed=False, target=config.name,
            detail={"reasons": reasons},
        )

        decision = await self.ask_operator(
            actor,
            f"{actor} wants to spawn {config.name} with wider permissions than its own",
            {
                "kind": "permission_escalation",
                "child": config.name,
                "reasons": reasons,
                "child_policy": child_policy.to_settings(),
                "parent_envelope": envelope.to_settings(),
            },
        )
        if decision.get("decision") != "allow":
            raise CapabilityError(
                f"refusing to spawn {config.name}: it "
                + "; ".join(reasons)
                + ". The operator declined the escalation."
            )
        self.audit.record(
            OPERATOR, "policy.escalation", allowed=True, target=config.name,
            detail={"approved_for": actor},
        )

    # ==================================================================
    # kernel hooks -- the effects the kernel authorises
    # ==================================================================

    def deliver_message(self, target: str, message: dict) -> None:
        box = self.mailboxes.get(target)
        posted = box.post(message)

        notify = "none"
        container = self.containers.get(target)
        if container is not None:
            notify = container.config.runtime.notify
            if notify == "file":
                with contextlib.suppress(OSError):
                    write_inbox_file(container.paths.shared, posted)
            elif notify == "pty" and container.session is not None:
                with contextlib.suppress(Exception):
                    container.session.write(
                        f"\r\n[capwrap] message from {posted.sender}: "
                        f"{posted.payload}\r\n"
                    )
        self._trace_message(target, posted, notify)
        self._emit("message", {"to": target, "message": posted.to_dict()})

    # ------------------------------------------------------------------
    # message tracing -- opt-in, for working out why agents are not talking
    # ------------------------------------------------------------------

    def _trace_message(self, target: str, posted: Any, notify: str) -> None:
        """Record one delivery, if tracing is on.

        The audit log already says that a message was sent and through which
        slot. What it deliberately does not keep is the payload, and that is
        the one thing you need when two agents are talking past each other. So
        this is a separate, opt-in buffer rather than more audit detail.
        """
        if not self.trace_messages:
            return
        record = {
            "id": posted.id,
            "ts": posted.ts,
            "from": posted.sender,
            "to": target,
            "kind": posted.kind,
            "via_slot": posted.via_slot,
            "notify": notify,
            "payload": posted.payload,
            "signature": posted.signature,
            "public_key": posted.public_key,
            "signed": posted.signed,
        }
        self.message_trace.append(record)
        self._emit("message.trace", {"record": record})

    def trace_state(self) -> dict:
        return {
            "enabled": self.trace_messages,
            "recorded": len(self.message_trace),
            "capacity": self.message_trace.maxlen,
        }

    def set_message_trace(self, enabled: bool) -> dict:
        """Turn the trace on or off at runtime.

        Turning it off discards what was collected: leaving payloads in memory
        after the operator has said they no longer want them recorded would
        make "off" mean something weaker than it says.
        """
        enabled = bool(enabled)
        if enabled != self.trace_messages:
            self.audit.record(
                OPERATOR, "trace.messages", allowed=True,
                detail={"enabled": enabled},
            )
        self.trace_messages = enabled
        if not enabled:
            self.message_trace.clear()
        self._emit("trace.changed", self.trace_state())
        return self.trace_state()

    def traced_messages(self, limit: int = 200) -> list[dict]:
        return list(self.message_trace)[-max(1, limit):]

    def board_posted(self, topic: str, entry: dict) -> None:
        """A board gained a post.

        Emitted, not delivered: a board is read by whoever holds it rather than
        pushed at anyone, so there is no mailbox to write to. The console is the
        one reader that wants telling.
        """
        self._emit("board.posted", {"board": topic, "post": entry})

    def kill_container(self, name: str, signal: int) -> None:
        container = self.containers.get(name)
        if container is None or container.session is None:
            return
        asyncio.ensure_future(container.session.terminate())

    def signal_container(self, name: str, signal: int) -> None:
        container = self.containers.get(name)
        if container is not None and container.session is not None:
            container.session.signal(signal)

    def write_input(self, name: str, data: str) -> None:
        container = self.containers.get(name)
        if container is None or container.session is None:
            raise CapwrapError(f"{name} is not running")
        container.session.write(data)

    def read_output(self, name: str, rows: int) -> dict:
        """The target's current screen, as the capability kernel authorised.

        The pyte screen rather than the raw byte log: a caller wants to know what
        the agent is *showing* -- which option is highlighted, what the prompt
        says -- and replaying bytes cannot answer that without a terminal
        emulator on the other end.
        """
        container = self.containers.get(name)
        if container is None or container.session is None:
            return {"container": name, "running": False, "lines": [], "cursor": None}
        snapshot = container.session.snapshot(tail=max(1, min(rows, 200)))
        return {
            "container": name,
            "running": container.running,
            **snapshot.to_dict(),
        }

    def spawn_container(self, config: ContainerConfig, parent: str) -> ContainerObject:
        container = self.register(config, parent=parent)
        asyncio.ensure_future(self.start(config.name))
        return container.obj

    @property
    def mapper(self):
        """The mapping backend, selected on first use."""
        if self._mapper is None:
            self._mapper, self.mapper_detail = mapper_mod.select(
                os.environ.get("CAPWRAP_MAPPING_BACKEND", "auto")
            )
            self.audit.record(
                ROOT, "mapper.select", allowed=True,
                target=self._mapper.name, detail=self.mapper_detail,
            )
        return self._mapper

    def materialise(self, target: str, source: Path, dest_name: str, mode: str) -> str:
        """Put a dataspace where the receiving container can reach it.

        Which mechanism that is depends on the backend: a bind mount into the
        live container when the daemon is privileged enough, a copy or symlink
        into its /shared otherwise. `ds_map` has already decided that the
        mapping is permitted; this only carries it out.
        """
        container = self.containers.get(target)
        if container is None:
            raise CapwrapError(f"no such container: {target}")
        return self.mapper.materialise(container, source, dest_name, mode)

    def unmaterialise(self, token: str) -> None:
        """Undo a `materialise`, when its mapping is revoked."""
        parts = token.split(":")
        name = parts[1] if len(parts) >= 3 else (parts[0] if parts else "")
        container = self.containers.get(name)
        if container is None:
            return
        with contextlib.suppress(Exception):
            self.mapper.unmaterialise(container, token)

    # ==================================================================
    # operator interaction
    # ==================================================================

    async def ask_operator(
        self,
        container: str,
        question: str,
        context: dict,
        blocking: bool = True,
        timeout: float | None = None,
    ) -> dict:
        """Put a question in front of the human and, if asked, wait for it.

        Every container can reach this -- it is granted at creation and is not
        revocable by another agent -- because an agent that cannot ask for
        permission will simply guess instead.
        """
        pending = PendingApproval(container, question, context)
        self.approvals[pending.id] = pending
        self.kernel.audit.record(
            container, "ask", allowed=True, target=OPERATOR, detail=question[:200]
        )
        message = self.mailboxes.get(OPERATOR).post({
            "from": container, "kind": "question",
            "payload": {"id": pending.id, "question": question, "context": context},
        })
        self._question_messages[pending.id] = message
        # Posted straight to the mailbox rather than through `deliver_message`,
        # since the operator is not a container -- so the event that a browser
        # listens for has to be emitted here too. Without it a question only
        # showed up in the inbox after a reload, which made the inbox look like
        # it had missed it.
        self._emit("message", {"to": OPERATOR, "message": message.to_dict()})
        self._emit("approval.requested", pending.to_dict())

        if not blocking:
            return {"id": pending.id, "pending": True}

        try:
            return await asyncio.wait_for(pending.future, timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            self._abandon(pending, "timeout", "no answer from the operator")
            return {"id": pending.id, "decision": "timeout", "reason": "no answer"}
        except asyncio.CancelledError:
            # The asker is gone: its process died, its container exited, or the
            # control connection dropped. Nobody is left to receive an answer,
            # so the question must not stay in the queue looking answerable.
            self._abandon(
                pending, "abandoned", f"{container} is no longer waiting for an answer"
            )
            raise
        finally:
            self.approvals.pop(pending.id, None)

    async def request_capability(
        self,
        actor: str,
        kind: str,
        target: str,
        rights: list[str],
        quota: int = 1,
        reason: str = "",
        timeout: float | None = None,
    ) -> dict:
        """Ask the operator for a capability, and receive it if they agree.

        `capctl ask` returns a *string*: approving it tells the agent "yes" but
        performs nothing, so an agent that asked for a factory and was told
        "allow" still had an unchanged capability table. This closes that loop --
        the approval itself does the delegation, atomically, and the agent gets
        back the slot number it can immediately use.

        Naming a container here is not a hole in "no ambient authority". An agent
        still cannot *act* on anything it has no slot for; it is asking a human
        to grant one, and the human is the one who resolves the name and decides.
        A request is not a reference.

        The operator may grant narrower rights than were asked for; whatever they
        return is what gets delegated.
        """
        if kind not in DEFAULT_REQUEST_RIGHTS:
            raise CapabilityError(
                f"cannot request a capability of kind {kind!r}; "
                f"expected {', '.join(sorted(DEFAULT_REQUEST_RIGHTS))}"
            )
        # Defaults have to match the kind: `send` means nothing on a factory,
        # and the kernel would reject the grant after the operator had already
        # clicked approve.
        requested = parse_rights(rights) if rights else DEFAULT_REQUEST_RIGHTS[kind]

        if kind == "net_rule" and "=" not in target:
            raise CapabilityError(
                "a net_rule request names the rule and its pattern, as "
                "'name=<host:port regex>' -- for example "
                "\"pypi=(pypi\\.org|files\\.pythonhosted\\.org):443\""
            )

        self.audit.record(
            actor, "cap.request", allowed=True, target=target,
            rights=str(requested), detail={"kind": kind, "reason": reason},
        )

        decision = await self.ask_operator(
            actor,
            f"{actor} requests a {kind} capability"
            + (f" on {target}" if target else "")
            + (f": {reason}" if reason else ""),
            {
                "kind": "capability_request",
                "request": {
                    "kind": kind,
                    "target": target,
                    "rights": requested.names(),
                    "quota": quota,
                    "reason": reason,
                    # So the approval card offers the rights that apply to this
                    # kind, rather than a container-shaped list every time.
                    "valid_rights": VALID_RIGHTS[kind].names(),
                },
            },
            timeout=timeout,
        )

        if decision.get("decision") != "allow":
            self.audit.record(
                actor, "cap.request", allowed=False, target=target,
                detail=decision.get("reason") or "declined",
            )
            return {
                "granted": False,
                "decision": decision.get("decision", "denied"),
                "reason": decision.get("reason", ""),
            }

        # The operator can hand back a narrower set than was asked for.
        granted = parse_rights(decision["rights"]) if decision.get("rights") else requested
        result = self.kernel.operator_grant(
            actor, kind, target, granted, quota=quota
        )
        self._emit("cap.granted", {"container": actor, **result})
        return {"granted": True, "decision": "allow", **result}

    def resolve_approval(
        self,
        approval_id: int,
        decision: str,
        reason: str = "",
        rights: list[str] | None = None,
    ) -> bool:
        """Answer a pending question from the web UI.

        `rights` is only meaningful for a capability request, where it lets the
        operator grant less than was asked for.
        """
        pending = self.approvals.get(approval_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(
            {"decision": decision, "reason": reason, "rights": rights}
        )
        self._close_question(pending, decision, reason)
        self.kernel.audit.record(
            OPERATOR, "approval.resolve", allowed=(decision == "allow"),
            target=pending.container, detail={"decision": decision, "reason": reason},
        )
        self._emit("approval.resolved", {"id": approval_id, "decision": decision})
        return True

    def pending_approvals(self) -> list[dict]:
        return [
            p.to_dict() for p in self.approvals.values() if not p.future.done()
        ]

    def _close_question(
        self, pending: PendingApproval, decision: str, reason: str
    ) -> None:
        """Stamp an outcome onto the operator-inbox copy of a question.

        The inbox is history and is replayed verbatim on reload, so a question
        with nothing recorded against it comes back looking like one still
        waiting on you.
        """
        if pending.closed:
            return
        pending.closed = True
        self.explainer.forget(pending.id)
        message = self._question_messages.pop(pending.id, None)
        if message is not None and isinstance(message.payload, dict):
            message.payload["decision"] = decision
            message.payload["reason"] = reason

    def _abandon(self, pending: PendingApproval, decision: str, reason: str) -> None:
        """Retire a question that is not going to be answered.

        The waiter, if there still is one, is given the outcome rather than
        having its connection dropped: a container being dismissed out from
        under a `capctl ask` should see "abandoned" and exit on it, not report
        that the daemon hung up on it. Where the waiter has already gone -- the
        connection-lost case -- the future is cancelled by then and this only
        records what happened.

        Idempotent, because the two paths overlap: resolving the future to clear
        a dead container's queue is itself what wakes the coroutine that then
        finds its own question already closed.
        """
        if pending.closed:
            self.approvals.pop(pending.id, None)
            return
        self._close_question(pending, decision, reason)
        self.approvals.pop(pending.id, None)
        if not pending.future.done():
            pending.future.set_result({"decision": decision, "reason": reason})
        self.audit.record(
            ROOT, "approval.abandon", allowed=False, target=pending.container,
            detail={"decision": decision, "reason": reason},
        )
        self._emit("approval.resolved", {"id": pending.id, "decision": decision})

    def abandon_approvals(self, container: str, reason: str) -> int:
        """Clear every question a container still has open.

        Called when it exits or is dismissed. Without this its questions
        outlive it: `pending_approvals` still reports them, so they come back
        on every page load and every reconnect, and clicking Allow resolves a
        future that nothing is waiting on.
        """
        stale = [
            p for p in list(self.approvals.values())
            if p.container == container and not p.closed
        ]
        for pending in stale:
            self._abandon(pending, "abandoned", reason)
        return len(stale)

    # ==================================================================
    # events, for the web UI
    # ==================================================================

    def subscribe_events(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._events.append(queue)
        return queue

    def unsubscribe_events(self, queue: asyncio.Queue) -> None:
        with contextlib.suppress(ValueError):
            self._events.remove(queue)

    def _emit(self, kind: str, data: dict) -> None:
        event = {"event": kind, "ts": time.time(), **data}
        for queue in list(self._events):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A browser that has stopped reading must not stall the daemon.
                pass

    # ==================================================================
    # operator-side views
    # ==================================================================

    def overview(self) -> dict:
        return {
            "instance": self.instance_name,
            "containers": [c.status() for c in self.containers.values()],
            "tree": self.kernel.container_tree(),
            "approvals": self.pending_approvals(),
            "operator_inbox": [
                m.to_dict() for m in self.mailboxes.get(OPERATOR).recent(50)
            ],
        }

    async def shutdown(self) -> None:
        """Tear everything down, on the way out of `capwrap up`.

        `force`, because `destroy` otherwise refuses a running container. That
        guard exists to stop a mis-click in the tree ending an agent mid-task,
        and it has no business surviving into shutdown: without it Ctrl-C left
        every sandbox running and the process hanging on its own cleanup.
        """
        for name in list(self.containers):
            with contextlib.suppress(Exception):
                await self.destroy(name, force=True)
        self.audit.close()
