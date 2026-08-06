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
from pathlib import Path
from typing import Any

from .config import ContainerConfig, load_config_data
from .errors import CapabilityError, CapwrapError, SandboxError
from .ipc.mailbox import MailboxRegistry, write_inbox_file
from .ipc.protocol import AGENT_OPS, MAX_REQUEST_BYTES, ProtocolError, Request, Response
from .kernel.audit import AuditLog
from .kernel.kernel import ROOT, CapKernel
from .kernel.objects import ContainerObject
from .kernel.policy import contains as policy_contains
from .kernel.rights import VALID_RIGHTS, Rights, parse_rights
from .paths import ContainerPaths, db_path, force_rmtree, state_root
from .runtime import bwrap as bwrap_mod
from .runtime import fsprep, probe
from .runtime.supervisor import PtySession

OPERATOR = "operator"

#: What an agent gets if it requests a capability without naming rights.
DEFAULT_REQUEST_RIGHTS = {
    "container": Rights.SEND | Rights.INSPECT,
    "dataspace": Rights.READ,
    "factory": Rights.CREATE,
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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "container": self.container,
            "question": self.question,
            "context": self.context,
            "created_at": self.created_at,
            "resolved": self.future.done(),
        }


class Container:
    """A registered container and, when running, its sandbox."""

    def __init__(self, config: ContainerConfig, obj: ContainerObject) -> None:
        self.config = config
        self.obj = obj
        self.paths = ContainerPaths(config.name)
        self.session: PtySession | None = None
        self.prepared: fsprep.PreparedFs | None = None
        self.server: asyncio.AbstractServer | None = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def running(self) -> bool:
        return self.session is not None and self.session.running

    def status(self) -> dict:
        return {
            **self.obj.describe(),
            "running": self.running,
            "mounts": self.obj.mounts,
            "session": self.session.status() if self.session else None,
        }


class Daemon:
    """Owns every container, and implements the kernel's effects."""

    def __init__(self, audit_path: Path | None = None) -> None:
        self.state = state_root()
        self.state.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLog(audit_path if audit_path is not None else db_path())
        self.kernel = CapKernel(audit=self.audit, hooks=self)
        self.mailboxes = MailboxRegistry()
        self.containers: dict[str, Container] = {}
        self.approvals: dict[int, PendingApproval] = {}
        self._events: list[asyncio.Queue] = []
        self._overlay_backend: str | None = None
        self._bwrap: str | None = None

    # ==================================================================
    # host capability discovery
    # ==================================================================

    def _ensure_host_ready(self) -> None:
        if self._bwrap is not None:
            return
        report = probe.run_all()
        check = report.get("bwrap can create namespaces")
        if check is None or not check.ok:
            raise SandboxError(
                f"cannot sandbox on this host: {check.detail if check else 'no bwrap'}"
                + (f" -- {check.hint}" if check and check.hint else "")
            )
        self._bwrap = probe.find_bwrap()
        self._overlay_backend = report.overlay_backend

    # ==================================================================
    # container lifecycle
    # ==================================================================

    def register(self, config: ContainerConfig, parent: str = ROOT) -> Container:
        """Register a container with the kernel without starting it."""
        config.validate_sources()
        obj = self.kernel.register_container(config, parent=parent)
        container = Container(config, obj)
        self.containers[config.name] = container
        self.mailboxes.get(config.name)
        self._emit("container.registered", {"container": config.name})
        return container

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

        # Bind the container's socket *before* building the argv, because
        # `build_argv` only mounts it if the file already exists.
        container.server = await self._serve_container(container)

        argv = bwrap_mod.build_argv(
            container.config,
            container.prepared,
            container.paths,
            bwrap=self._bwrap,
            guest_tools=Path(__file__).resolve().parent / "guest",
        )

        session = PtySession(name=name, argv=argv, env=os.environ.copy())
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
        self.audit.record(ROOT, "container.exit", allowed=True, target=container.name,
                          detail={"exit_code": code})
        self._emit("container.exited", {"container": container.name, "exit_code": code})

    async def stop(self, name: str, grace: float = 5.0) -> int | None:
        container = self._get(name)
        if container.session is None:
            return None
        code = await container.session.terminate(grace=grace)
        return code

    async def destroy(self, name: str, remove_state: bool = False) -> None:
        """Stop a container and revoke everything it held or passed on."""
        container = self.containers.get(name)
        if container is None:
            return
        await self.stop(name)

        if container.server is not None:
            container.server.close()
            with contextlib.suppress(Exception):
                await container.server.wait_closed()
        if container.prepared is not None:
            container.prepared.cleanup()

        self.kernel.destroy_container(name)
        self.mailboxes.drop(name)
        del self.containers[name]

        if remove_state:
            force_rmtree(container.paths.root)
        self._emit("container.destroyed", {"container": name})

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

    async def _handle_connection(
        self, actor: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.LimitOverrunError:
                    await self._reply(writer, Response(
                        id=0, ok=False, code="protocol_error",
                        message="request exceeds the maximum size",
                    ))
                    return
                except (asyncio.IncompleteReadError, ConnectionResetError):
                    return
                if not line.strip():
                    continue
                if len(line) > MAX_REQUEST_BYTES:
                    await self._reply(writer, Response(
                        id=0, ok=False, code="protocol_error",
                        message="request exceeds the maximum size",
                    ))
                    return

                response = await self._dispatch(actor, line)
                await self._reply(writer, response)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

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
            return {"container": actor, "caps": len(k.tasks[actor])}
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
            return k.msg_send(actor, int(args["slot"]), args.get("payload"))
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
        """Stop a container handing its child broader Claude permissions than it has.

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
        self._emit("message", {"to": target, "message": posted.to_dict()})

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

    def spawn_container(self, config: ContainerConfig, parent: str) -> ContainerObject:
        container = self.register(config, parent=parent)
        asyncio.ensure_future(self.start(config.name))
        return container.obj

    def materialise(self, target: str, source: Path, dest_name: str, mode: str) -> str:
        """Place a dataspace into a container's /shared.

        This is the "map now, live" path: `/shared` is a plain host directory
        already bind-mounted into the running sandbox, so anything written here
        appears immediately without touching the container's mount namespace.
        Real bind mounts into a running container (via nsenter into its user and
        mount namespaces) would slot in behind this same interface.
        """
        container = self.containers.get(target)
        if container is None:
            raise CapwrapError(f"no such container: {target}")

        safe_name = dest_name.strip("/").replace("..", "_").replace("/", "_")
        if not safe_name:
            raise CapwrapError("a destination name is required")
        destination = container.paths.shared / safe_name

        if destination.exists() or destination.is_symlink():
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            else:
                destination.unlink()

        if mode == "copy":
            if source.is_dir():
                shutil.copytree(source, destination, symlinks=True)
            else:
                shutil.copy2(source, destination)
        else:
            # A symlink aliases the two containers to the same bytes, which is
            # what distinguishes MAP from COPY.
            destination.symlink_to(source)

        return f"{target}:{safe_name}"

    def unmaterialise(self, token: str) -> None:
        """Undo a `materialise`, when its mapping is revoked."""
        target, _, name = token.partition(":")
        container = self.containers.get(target)
        if container is None or not name:
            return
        path = container.paths.shared / name
        if path.is_symlink() or path.is_file():
            with contextlib.suppress(OSError):
                path.unlink()
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)

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
        self.mailboxes.get(OPERATOR).post({
            "from": container, "kind": "question",
            "payload": {"id": pending.id, "question": question, "context": context},
        })
        self._emit("approval.requested", pending.to_dict())

        if not blocking:
            return {"id": pending.id, "pending": True}

        try:
            return await asyncio.wait_for(pending.future, timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return {"id": pending.id, "decision": "timeout", "reason": "no answer"}
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
            "containers": [c.status() for c in self.containers.values()],
            "tree": self.kernel.container_tree(),
            "approvals": self.pending_approvals(),
            "operator_inbox": [
                m.to_dict() for m in self.mailboxes.get(OPERATOR).recent(50)
            ],
        }

    async def shutdown(self) -> None:
        for name in list(self.containers):
            with contextlib.suppress(Exception):
                await self.destroy(name)
        self.audit.close()
