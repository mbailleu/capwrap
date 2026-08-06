"""The capability kernel: every privileged operation in capwrap goes through here.

Design rules, in order of importance:

1. **No ambient authority.**  Every operation names its subject by a slot in the
   caller's own capability table.  There is no "by name" variant, anywhere.  An
   agent cannot act on a container it was not given a capability for, because it
   has no way to refer to one.

2. **Authority only ever shrinks.**  Delegation goes through the mapping
   database, which refuses to hand on rights the delegator does not hold.  This
   applies to spawning too: a container created through a factory gets its
   initial capabilities *derived from the spawner's*, so an agent cannot mint a
   child with more authority than it has itself.  Skipping this would make
   `factory` a hole big enough to drive the whole system through.

3. **The kernel performs no I/O.**  Sending a message, killing a process and
   copying a file are effects; the kernel decides whether they are permitted and
   then calls a hook.  That keeps policy testable without a sandbox, a daemon or
   a filesystem, and keeps the audit log honest, since every decision passes one
   choke point.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from ..config import ContainerConfig
from ..errors import (
    CapabilityError,
    InsufficientRights,
    NoSuchCapability,
    QuotaExceeded,
)
from .audit import AuditLog
from .captable import Task
from .mapdb import MapNode, MappingDB
from .objects import (
    CapRef,
    ContainerObject,
    DataspaceObject,
    FactoryObject,
    GateObject,
    KernelObject,
    new_oid,
)
from .rights import VALID_RIGHTS, Rights, parse_rights, validate_for

#: The operator's task.  Holds a root capability on every object, which is what
#: makes "revoke anything from the web UI" always possible.
ROOT = "root"

#: Rights a container holds on itself.  Enough to introspect and to exit.
#:
#: DELEGATE is included on purpose: handing out authority *over yourself* is
#: never amplification, whoever you hand it to.  It is also load-bearing -- when
#: an agent spawns a child, the child's "talk back to your parent" capability is
#: derived from the parent's capability on itself, and without DELEGATE that
#: derivation is illegal and every spawn fails.
SELF_RIGHTS = (
    Rights.INSPECT
    | Rights.SEND
    | Rights.READ_OUTPUT
    | Rights.KILL
    | Rights.SIGNAL
    | Rights.DELEGATE
)

class Hooks(Protocol):
    """Effects the kernel authorises but does not perform."""

    def deliver_message(self, target: str, message: dict) -> None: ...
    def kill_container(self, name: str, signal: int) -> None: ...
    def signal_container(self, name: str, signal: int) -> None: ...
    def write_input(self, name: str, data: str) -> None: ...
    def spawn_container(
        self, config: ContainerConfig, parent: str
    ) -> "ContainerObject": ...
    def materialise(
        self, target: str, source: Path, dest_name: str, mode: str
    ) -> str: ...
    def unmaterialise(self, token: str) -> None: ...


class NullHooks:
    """No-op hooks, so the kernel can be exercised on its own in tests."""

    def deliver_message(self, target: str, message: dict) -> None:
        pass

    def kill_container(self, name: str, signal: int) -> None:
        pass

    def signal_container(self, name: str, signal: int) -> None:
        pass

    def write_input(self, name: str, data: str) -> None:
        pass

    def spawn_container(self, config: ContainerConfig, parent: str):
        raise CapabilityError("spawning is not available in this context")

    def materialise(self, target: str, source: Path, dest_name: str, mode: str) -> str:
        return f"{target}:{dest_name}"

    def unmaterialise(self, token: str) -> None:
        pass


@dataclass
class CapInfo:
    """What an agent is told about one of its own slots.

    Note what is *absent*: the object id.  Agents see a kind, a label and their
    rights, which is everything they need to use the capability and nothing they
    could use to name an object they were not given.
    """

    slot: int
    kind: str
    label: str
    rights: list[str]
    detail: dict[str, Any]

    def to_dict(self) -> dict:
        return {
            "slot": self.slot,
            "kind": self.kind,
            "label": self.label,
            "rights": self.rights,
            "detail": self.detail,
        }


def _unique_label(task: Task, base: str) -> str:
    """`peer:x`, then `peer:x#2`, ... -- labels are how agents address a slot,
    so a second capability on the same object must not collide with the first.
    """
    taken = {ref.label for ref in task.slots.values()}
    if base not in taken:
        return base
    n = 2
    while f"{base}#{n}" in taken:
        n += 1
    return f"{base}#{n}"


class CapKernel:
    """Objects, tasks, mappings and the operations over them."""

    def __init__(self, audit: AuditLog | None = None, hooks: Hooks | None = None) -> None:
        self.objects: dict[int, KernelObject] = {}
        self.tasks: dict[str, Task] = {}
        self.mapdb = MappingDB()
        self.audit = audit or AuditLog()
        self.hooks: Hooks = hooks or NullHooks()  # type: ignore[assignment]

        self.root = Task(name=ROOT, is_root=True)
        self.tasks[ROOT] = self.root

        #: The operator's inbox.  Every container is given a capability on this,
        #: so an agent can always reach the human even with no other authority.
        self.operator_gate = self._new_gate("operator", owner=ROOT)

    # ==================================================================
    # object creation (kernel-internal; no agent reaches these directly)
    # ==================================================================

    def _register(self, obj: KernelObject) -> KernelObject:
        self.objects[obj.oid] = obj
        return obj

    def _mint_root_cap(self, obj: KernelObject, rights: Rights | None = None) -> int:
        """Give the operator a root capability on a newly created object."""
        mask = rights if rights is not None else VALID_RIGHTS[obj.kind]
        slot = self.root._free_slot()
        node = self.mapdb.insert_root(obj.oid, ROOT, slot, mask)
        self.root.insert(CapRef(obj.oid, mask, node.id, obj.label), slot)
        return slot

    def _new_gate(self, label: str, owner: str) -> GateObject:
        gate = GateObject(oid=new_oid(), label=label, owner=owner)
        self._register(gate)
        self._mint_root_cap(gate)
        return gate

    def create_dataspace(
        self, path: Path, kind: str = "dir", label: str | None = None
    ) -> DataspaceObject:
        path = Path(path)
        existing = self.find_dataspace(path)
        if existing is not None:
            return existing
        ds = DataspaceObject(
            oid=new_oid(), label=label or str(path), path=path, ds_kind=kind  # type: ignore[arg-type]
        )
        self._register(ds)
        self._mint_root_cap(ds)
        return ds

    def create_factory(self, label: str, quota_containers: int) -> FactoryObject:
        factory = FactoryObject(
            oid=new_oid(), label=label, quota_containers=quota_containers
        )
        self._register(factory)
        self._mint_root_cap(factory)
        return factory

    def find_dataspace(self, path: Path) -> DataspaceObject | None:
        for obj in self.objects.values():
            if isinstance(obj, DataspaceObject) and obj.path == Path(path):
                return obj
        return None

    def find_container(self, name: str) -> ContainerObject | None:
        for obj in self.objects.values():
            if isinstance(obj, ContainerObject) and obj.name == name:
                return obj
        return None

    # ==================================================================
    # container registration
    # ==================================================================

    def register_container(
        self, config: ContainerConfig, parent: str = ROOT, mounts: list[str] | None = None
    ) -> ContainerObject:
        """Create a container object plus its task and initial capability table.

        `parent` is the *granter*: every initial capability the config asks for
        must be derived from one the parent already holds.  For an
        operator-launched container that is the root task, which holds
        everything; for a container spawned by an agent it is that agent, which
        is how factories are prevented from amplifying authority.
        """
        if config.name in self.tasks:
            raise CapabilityError(f"a container named {config.name!r} already exists")

        granter = self.tasks.get(parent)
        if granter is None:
            raise CapabilityError(f"unknown parent task {parent!r}")

        obj = ContainerObject(
            oid=new_oid(), label=config.name, name=config.name,
            parent=None if parent == ROOT else parent,
            mounts=mounts or [],
        )
        self._register(obj)
        self._mint_root_cap(obj)

        task = Task(name=config.name)
        self.tasks[config.name] = task
        self._grant_initial_caps(task, config, granter, obj)

        self.audit.record(
            parent, "container.register", allowed=True, target=config.name,
            detail={"caps": len(task)},
        )
        return obj

    def _grant_initial_caps(
        self,
        task: Task,
        config: ContainerConfig,
        granter: Task,
        obj: ContainerObject,
    ) -> None:
        """Populate a new container's capability table.

        Every grant is a delegation from `granter`, so `MappingDB.map` enforces
        that the new container's authority is bounded by its creator's.
        """
        caps = config.caps

        # A capability on itself, so an agent can introspect and exit.  Derived
        # from the root cap because it is authority over the new container, not
        # over anything the granter owns.
        self._delegate_from_root(task, obj.oid, SELF_RIGHTS, label="self")

        # The operator's inbox: always present, so `capctl ask` works even for a
        # container with no other capabilities at all.
        self._delegate_from_root(
            task, self.operator_gate.oid, Rights.SEND | Rights.INSPECT, label="operator"
        )

        # The parent container.
        if granter.name != ROOT:
            parent_obj = self.find_container(granter.name)
            if parent_obj is not None and caps.parent_mask:
                self._delegate_from(
                    granter, task, parent_obj.oid, caps.parent_mask, label="parent"
                )

        if caps.factory is not None:
            factory = self.create_factory(
                f"{config.name}-factory", caps.factory.quota.containers
            )
            self._delegate_from_root(
                task, factory.oid, caps.factory.mask, label="factory"
            )

        for peer in caps.peers:
            peer_obj = self.find_container(peer.container)
            if peer_obj is None:
                # Forward references are normal: dev-a names dev-b before dev-b
                # exists.  Recorded and skipped rather than fatal; `link_peers`
                # fills these in once both sides are registered.
                self.audit.record(
                    config.name, "cap.grant.deferred", allowed=True,
                    target=peer.container, rights=str(peer.mask),
                    detail="peer not registered yet",
                )
                continue
            self._delegate_from(
                granter, task, peer_obj.oid, peer.mask, label=f"peer:{peer.container}"
            )

        for ds_spec in caps.dataspaces:
            ds = self.create_dataspace(ds_spec.path, ds_spec.kind, ds_spec.label)
            self._delegate_from(
                granter, task, ds.oid, ds_spec.mask, label=ds_spec.label or str(ds.path)
            )

    def _root_node_for(self, oid: int) -> MapNode:
        slot = self.root.find(oid)
        if slot is None:
            obj = self.objects[oid]
            slot = self._mint_root_cap(obj)
        return self.mapdb.get(self.root.slots[slot].node)

    def _delegate_from_root(
        self, task: Task, oid: int, rights: Rights, label: str = ""
    ) -> int:
        parent_node = self._root_node_for(oid)
        slot = task._free_slot()
        node = self.mapdb.map(parent_node.id, task.name, slot, rights)
        task.insert(CapRef(oid, rights, node.id, label or self.objects[oid].label), slot)
        return slot

    def _delegate_from(
        self, granter: Task, task: Task, oid: int, rights: Rights, label: str = ""
    ) -> int:
        """Delegate `oid` from `granter` to `task`, bounded by what granter holds."""
        if granter.is_root:
            return self._delegate_from_root(task, oid, rights, label)

        slot = granter.find(oid)
        if slot is None:
            raise InsufficientRights(
                f"{granter.name} cannot grant a capability on "
                f"{self.objects[oid].label!r}: it holds none itself"
            )
        ref = granter.slots[slot]
        if Rights.DELEGATE not in ref.rights:
            raise InsufficientRights(
                f"{granter.name}'s capability on {ref.label!r} is not delegatable"
            )
        # map() raises RightsNotMonotonic if `rights` exceeds the granter's.
        new_slot = task._free_slot()
        node = self.mapdb.map(ref.node, task.name, new_slot, rights)
        task.insert(CapRef(oid, rights, node.id, label or ref.label), new_slot)
        return new_slot

    def link_peers(self, config: ContainerConfig) -> None:
        """Resolve peer capabilities that named a container registered later.

        Called after a batch of containers is registered, so configs can refer to
        each other in any order.
        """
        task = self.tasks.get(config.name)
        if task is None:
            return
        for peer in config.caps.peers:
            peer_obj = self.find_container(peer.container)
            if peer_obj is None or task.find(peer_obj.oid) is not None:
                continue
            self._delegate_from_root(
                task, peer_obj.oid, peer.mask, label=f"peer:{peer.container}"
            )

    def destroy_container(self, name: str) -> None:
        """Drop a container's task and revoke everything it held or passed on."""
        killed = self.mapdb.revoke_holder(name)
        self._apply_revocations(killed)
        self.tasks.pop(name, None)
        obj = self.find_container(name)
        if obj is not None:
            obj.state = "destroyed"
        self.audit.record(
            ROOT, "container.destroy", allowed=True, target=name,
            detail={"mappings_revoked": len(killed)},
        )

    # ==================================================================
    # the syscall surface -- everything below is reachable by an agent
    # ==================================================================

    def _task(self, actor: str) -> Task:
        task = self.tasks.get(actor)
        if task is None:
            raise NoSuchCapability(f"unknown task {actor!r}")
        return task

    def _checked(
        self, actor: str, op: str, slot: int, needed: Rights
    ) -> tuple[Task, CapRef]:
        """Look up a slot, verify rights, and audit the outcome either way."""
        task = self._task(actor)
        try:
            ref = task.require(slot, needed)
        except CapabilityError as exc:
            self.audit.record(
                actor, op, allowed=False, slot=slot,
                rights=str(needed), detail=str(exc),
            )
            raise
        self.audit.record(
            actor, op, allowed=True, slot=slot, rights=str(needed),
            target=ref.label,
        )
        return task, ref

    # -- introspection ---------------------------------------------------

    def cap_list(self, actor: str) -> list[CapInfo]:
        task = self._task(actor)
        out: list[CapInfo] = []
        for slot in sorted(task.slots):
            ref = task.slots[slot]
            obj = self.objects.get(ref.oid)
            if obj is None:
                continue
            out.append(
                CapInfo(
                    slot=slot, kind=obj.kind, label=ref.label or obj.label,
                    rights=ref.rights.names(), detail=obj.describe(),
                )
            )
        return out

    def cap_info(self, actor: str, slot: int) -> CapInfo:
        _task, ref = self._checked(actor, "cap.info", slot, Rights.INSPECT)
        obj = self.objects[ref.oid]
        return CapInfo(
            slot=slot, kind=obj.kind, label=ref.label or obj.label,
            rights=ref.rights.names(), detail=obj.describe(),
        )

    # -- messaging -------------------------------------------------------

    def msg_send(self, actor: str, slot: int, payload: Any) -> dict:
        """Post a message through a capability that carries SEND."""
        _task, ref = self._checked(actor, "msg.send", slot, Rights.SEND)
        obj = self.objects[ref.oid]

        message = {"from": actor, "payload": payload, "via_slot": slot}
        if isinstance(obj, ContainerObject):
            self.hooks.deliver_message(obj.name, message)
            return {"delivered_to": obj.name}
        if isinstance(obj, GateObject):
            self.hooks.deliver_message(obj.label, message)
            return {"delivered_to": obj.label}
        raise InsufficientRights(f"slot {slot} does not name a message endpoint")

    # -- delegation ------------------------------------------------------

    def cap_delegate(
        self, actor: str, target_slot: int, cap_slot: int, rights: str | list[str] | None
    ) -> dict:
        """Give the holder of `target_slot` a capability from `cap_slot`.

        Requires SEND on the target (you must be allowed to talk to it at all)
        and DELEGATE on the capability being passed (it must be shareable).  The
        requested rights are then bounded by what the actor holds, by the mapping
        database.
        """
        task, target_ref = self._checked(
            actor, "cap.delegate", target_slot, Rights.SEND
        )
        cap_ref = task.require(cap_slot, Rights.DELEGATE)

        target_obj = self.objects[target_ref.oid]
        if not isinstance(target_obj, ContainerObject):
            raise InsufficientRights("capabilities can only be delegated to a container")
        recipient = self.tasks.get(target_obj.name)
        if recipient is None:
            raise NoSuchCapability(f"{target_obj.name} has no capability table")

        requested = parse_rights(rights) if rights else cap_ref.rights
        try:
            new_slot = recipient._free_slot()
            node = self.mapdb.map(cap_ref.node, recipient.name, new_slot, requested)
        except CapabilityError as exc:
            self.audit.record(
                actor, "cap.delegate", allowed=False, target=target_obj.name,
                slot=cap_slot, rights=str(requested), detail=str(exc),
            )
            raise
        recipient.insert(
            CapRef(cap_ref.oid, requested, node.id, cap_ref.label), new_slot
        )

        self.audit.record(
            actor, "cap.delegate", allowed=True, target=target_obj.name,
            slot=cap_slot, rights=str(requested),
            detail={"recipient_slot": new_slot, "label": cap_ref.label},
        )
        self.hooks.deliver_message(
            target_obj.name,
            {
                "from": actor,
                "kind": "capability",
                "payload": {
                    "slot": new_slot, "label": cap_ref.label,
                    "rights": requested.names(),
                },
            },
        )
        return {"recipient": target_obj.name, "slot": new_slot,
                "rights": requested.names()}

    def cap_revoke(self, actor: str, slot: int, include_self: bool = False) -> dict:
        """Withdraw everything derived from a capability the actor holds.

        Default `include_self=False` matches L4's unmap: take back what you gave
        away, keep your own.  Pass True to drop your own capability as well.

        Revocation is recursive, so this also removes capabilities the recipient
        passed on to third parties the actor may never have heard of.
        """
        task = self._task(actor)
        ref = task.get(slot)
        killed = self.mapdb.revoke(ref.node, include_self=include_self)
        self._apply_revocations(killed)

        self.audit.record(
            actor, "cap.revoke", allowed=True, slot=slot, target=ref.label,
            detail={"revoked": len(killed), "include_self": include_self},
        )
        return {
            "revoked": len(killed),
            "holders": sorted({n.holder for n in killed}),
        }

    def _apply_revocations(self, killed: list[MapNode]) -> None:
        """Drop capability-table slots and undo side effects for dead mappings."""
        for node in killed:
            holder = self.tasks.get(node.holder)
            if holder is not None:
                holder.remove_node(node.id)
            if node.on_revoke:
                try:
                    self.hooks.unmaterialise(node.on_revoke)
                except Exception:  # noqa: BLE001 - teardown must not break revocation
                    pass

    # -- container control -----------------------------------------------

    def ctr_kill(self, actor: str, slot: int, signal: int = 15) -> dict:
        _task, ref = self._checked(actor, "ctr.kill", slot, Rights.KILL)
        obj = self.objects[ref.oid]
        if not isinstance(obj, ContainerObject):
            raise InsufficientRights(f"slot {slot} does not name a container")
        self.hooks.kill_container(obj.name, signal)
        return {"killed": obj.name, "signal": signal}

    def ctr_signal(self, actor: str, slot: int, signal: int = 2) -> dict:
        """Interrupt without terminating -- SIGINT by default."""
        _task, ref = self._checked(actor, "ctr.signal", slot, Rights.SIGNAL)
        obj = self.objects[ref.oid]
        if not isinstance(obj, ContainerObject):
            raise InsufficientRights(f"slot {slot} does not name a container")
        self.hooks.signal_container(obj.name, signal)
        return {"signalled": obj.name, "signal": signal}

    def ctr_input(self, actor: str, slot: int, data: str) -> dict:
        _task, ref = self._checked(actor, "ctr.input", slot, Rights.WRITE_INPUT)
        obj = self.objects[ref.oid]
        if not isinstance(obj, ContainerObject):
            raise InsufficientRights(f"slot {slot} does not name a container")
        self.hooks.write_input(obj.name, data)
        return {"wrote": len(data), "to": obj.name}

    def ctr_status(self, actor: str, slot: int) -> dict:
        _task, ref = self._checked(actor, "ctr.status", slot, Rights.INSPECT)
        return self.objects[ref.oid].describe()

    def ctr_spawn(
        self, actor: str, factory_slot: int, config: ContainerConfig
    ) -> dict:
        """Create a container through a factory capability.

        The new container's initial capabilities are delegated from `actor`, so
        it cannot start life with authority the spawner lacks.
        """
        task, ref = self._checked(actor, "ctr.spawn", factory_slot, Rights.CREATE)
        factory = self.objects[ref.oid]
        if not isinstance(factory, FactoryObject):
            raise InsufficientRights(f"slot {factory_slot} does not name a factory")
        if factory.remaining <= 0:
            self.audit.record(
                actor, "ctr.spawn", allowed=False, slot=factory_slot,
                detail=f"quota exhausted ({factory.used_containers}/"
                       f"{factory.quota_containers})",
            )
            raise QuotaExceeded(
                f"factory {factory.label!r} has used all "
                f"{factory.quota_containers} of its container allowance"
            )

        factory.used_containers += 1
        obj = self.hooks.spawn_container(config, actor)
        return {"spawned": obj.name, "remaining_quota": factory.remaining}

    # -- dataspaces ------------------------------------------------------

    def ds_map(
        self,
        actor: str,
        target_slot: int,
        ds_slot: int,
        dest_name: str,
        mode: str = "copy",
    ) -> dict:
        """Place a dataspace into another container's /shared directory.

        `mode="copy"` needs COPY on the dataspace; anything that keeps the
        containers aliased to the same bytes needs MAP, which is the stronger
        right.  Both additionally need SEND on the target: you may not push data
        at a container you are not allowed to talk to.
        """
        needed = Rights.MAP if mode != "copy" else Rights.COPY
        task, target_ref = self._checked(actor, "ds.map", target_slot, Rights.SEND)
        ds_ref = task.require(ds_slot, needed | Rights.READ)

        ds = self.objects[ds_ref.oid]
        target = self.objects[target_ref.oid]
        if not isinstance(ds, DataspaceObject):
            raise InsufficientRights(f"slot {ds_slot} does not name a dataspace")
        if not isinstance(target, ContainerObject):
            raise InsufficientRights(f"slot {target_slot} does not name a container")

        recipient = self.tasks.get(target.name)
        if recipient is None:
            raise NoSuchCapability(f"{target.name} has no capability table")

        token = self.hooks.materialise(target.name, ds.path, dest_name, mode)

        # The recipient also gets a capability on the dataspace, so it can pass
        # it on (if given DELEGATE) and so revoking the mapping removes both the
        # files and the authority in one step.
        granted = Rights.READ | (Rights.WRITE if Rights.WRITE in ds_ref.rights else Rights.NONE)
        new_slot = recipient._free_slot()
        node = self.mapdb.map(
            ds_ref.node, recipient.name, new_slot, granted, on_revoke=token
        )
        recipient.insert(CapRef(ds.oid, granted, node.id, dest_name), new_slot)

        self.audit.record(
            actor, "ds.map", allowed=True, target=target.name, slot=ds_slot,
            rights=str(granted), detail={"dest": dest_name, "mode": mode},
        )
        self.hooks.deliver_message(
            target.name,
            {
                "from": actor, "kind": "dataspace",
                "payload": {
                    "slot": new_slot, "path": f"/shared/{dest_name}",
                    "mode": mode, "rights": granted.names(),
                },
            },
        )
        return {"recipient": target.name, "slot": new_slot,
                "path": f"/shared/{dest_name}"}

    # -- operator --------------------------------------------------------

    def ask(self, actor: str, question: str, context: dict | None = None) -> dict:
        """Route a question to the operator's inbox.

        Deliberately not gated on a capability the agent could lose: reaching
        the human is granted to every container at creation and is the one
        channel that must never be revocable by another agent.
        """
        message = {
            "from": actor, "kind": "question",
            "payload": {"question": question, "context": context or {}},
        }
        self.audit.record(actor, "ask", allowed=True, target="operator",
                          detail=question[:200])
        self.hooks.deliver_message(self.operator_gate.label, message)
        return {"asked": True}

    def operator_grant(
        self,
        holder_name: str,
        kind: str,
        target: str,
        rights: Rights,
        quota: int = 0,
        label: str | None = None,
    ) -> dict:
        """Mint a capability into `holder_name`'s table, on the operator's say-so.

        The one place authority enters the system from outside. Reached from the
        web UI's grant button and from an approved `cap.request`; never from an
        agent directly, because it delegates from the root task rather than from
        the caller.
        """
        task = self.tasks.get(holder_name)
        if task is None:
            raise NoSuchCapability(f"unknown container {holder_name!r}")

        if kind == "container":
            obj = self.find_container(target)
            if obj is None:
                raise NoSuchCapability(f"no such container: {target}")
            default_label = f"peer:{target}"
        elif kind == "dataspace":
            obj = self.create_dataspace(Path(target))
            default_label = target
        elif kind == "factory":
            obj = self.create_factory(f"{holder_name}-factory", quota)
            default_label = "factory"
        else:
            raise CapabilityError(f"cannot grant a capability of kind {kind!r}")

        rights = validate_for(obj.kind, rights)
        slot = self._delegate_from_root(
            task, obj.oid, rights, label=_unique_label(task, label or default_label)
        )
        self.audit.record(
            ROOT, "cap.operator_grant", allowed=True, target=holder_name,
            slot=slot, rights=str(rights), detail={"kind": kind, "object": target},
        )
        return {
            "holder": holder_name,
            "slot": slot,
            "kind": kind,
            "label": task.slots[slot].label,
            "rights": rights.names(),
        }

    # -- reporting -------------------------------------------------------

    def container_tree(self) -> list[dict]:
        """Parent/child forest of all containers, for the web UI."""
        containers = [
            obj for obj in self.objects.values() if isinstance(obj, ContainerObject)
        ]
        by_parent: dict[str | None, list[ContainerObject]] = {}
        for obj in containers:
            by_parent.setdefault(obj.parent, []).append(obj)

        def build(parent: str | None) -> list[dict]:
            return [
                {
                    **obj.describe(),
                    "mounts": obj.mounts,
                    "caps": len(self.tasks.get(obj.name, Task(obj.name))),
                    "children": build(obj.name),
                }
                for obj in sorted(by_parent.get(parent, []), key=lambda o: o.name)
            ]

        return build(None)

    def cap_graph(self) -> dict:
        """Every live mapping, for the operator's capability inspector."""
        return {
            "objects": {
                str(oid): obj.describe() for oid, obj in self.objects.items()
            },
            "mappings": [
                n.summary() for n in self.mapdb.all_nodes() if not n.revoked
            ],
        }
