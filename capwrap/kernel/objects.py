"""Kernel objects -- the things capabilities point at.

Objects live in one flat table owned by the kernel and are addressed by an
integer `oid` that **never leaves the kernel**.  Containers only ever see slot
numbers in their own capability table, so an agent cannot name an object it has
not been given, cannot enumerate objects it does not hold, and cannot forge a
reference by guessing.  That property is the whole reason for the indirection;
it is what makes the system analysable rather than merely locked down.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .rights import Rights

ObjectKind = Literal["container", "dataspace", "factory", "gate"]

_next_oid = itertools.count(1)


@dataclass
class KernelObject:
    """Base for everything a capability can refer to."""

    oid: int
    kind: ObjectKind
    label: str

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "label": self.label}


@dataclass
class ContainerObject(KernelObject):
    """A sandbox, running or not.

    The object outlives the process: killing a container leaves the object (and
    everyone's capabilities on it) in place, marked dead, so that revocation and
    audit stay meaningful and a restart can reuse the same identity.
    """

    kind: ObjectKind = field(default="container", init=False)
    name: str = ""
    parent: str | None = None
    state: str = "created"
    pid: int | None = None
    exit_code: int | None = None
    #: Mount summary, for the web UI.
    mounts: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "name": self.name,
            "parent": self.parent,
            "state": self.state,
            "pid": self.pid,
            "exit_code": self.exit_code,
        }

    @property
    def alive(self) -> bool:
        return self.state == "running"


@dataclass
class DataspaceObject(KernelObject):
    """A host path that may be shown or given to a container."""

    kind: ObjectKind = field(default="dataspace", init=False)
    path: Path = Path("/")
    ds_kind: Literal["dir", "file", "git_repo"] = "dir"

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "path": str(self.path), "ds_kind": self.ds_kind}


@dataclass
class FactoryObject(KernelObject):
    """Authority to create containers, and the budget for doing so.

    Quota is consumed on creation and *not* returned when a child dies: a
    factory's allowance bounds how many containers may ever be spawned through
    it, which is what stops a runaway agent from cycling containers forever.
    """

    kind: ObjectKind = field(default="factory", init=False)
    quota_containers: int = 0
    used_containers: int = 0
    #: What the *spawner* receives on each container it creates through this
    #: factory. Without it a parent can create a child and then have no way to
    #: reach it at all, which makes a supervisor agent impossible to express.
    child_rights: Rights = Rights.NONE

    @property
    def remaining(self) -> int:
        return max(0, self.quota_containers - self.used_containers)

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "quota_containers": self.quota_containers,
            "used_containers": self.used_containers,
            "remaining": self.remaining,
            "child_rights": self.child_rights.names(),
        }


@dataclass
class GateObject(KernelObject):
    """A bare message endpoint, not tied to a container's lifetime.

    Used for reply channels and for the operator's own inbox, so a container can
    be given the right to talk to *something* without being given a capability
    on the container behind it.
    """

    kind: ObjectKind = field(default="gate", init=False)
    owner: str = ""

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "owner": self.owner}


def new_oid() -> int:
    return next(_next_oid)


@dataclass
class CapRef:
    """One entry in a container's capability table.

    `node` links back into the mapping database, which is what makes recursive
    revocation possible: revoking a mapping walks its subtree and removes the
    `CapRef` each descendant is holding.
    """

    oid: int
    rights: Rights
    node: int
    #: Human-facing name, shown by `capctl caps`.  Purely cosmetic.
    label: str = ""

    def allows(self, needed: Rights) -> bool:
        return needed in self.rights
