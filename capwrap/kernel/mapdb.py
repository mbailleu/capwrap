"""The mapping database: who gave what to whom, and how to take it back.

Straight out of L4.  Every capability a task holds is a node in a forest.  Roots
are capabilities the kernel minted directly (held by the operator); every other
node was derived from its parent by delegation.

Two invariants do all the work:

**Monotonicity.**  A child's rights are a subset of its parent's.  Authority can
only ever shrink as it spreads, so the rights any task can possibly obtain are
bounded by the rights on the path back to a root.  You can read a container's
maximum possible authority off the tree without simulating the system.

**Recursive revocation.**  Revoking a node revokes its entire subtree.  When you
take a capability back from agent A, everything A passed on dies with it --
including things A gave to agents you have never heard of.  Without this, a
capability system leaks authority permanently on the first delegation, and
"revoke" becomes a lie.

The tree is the reason delegation is safe to allow at all.  Agents can hand each
other authority freely, because the operator holds the root and one `revoke`
unwinds the whole thing.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from ..errors import NoSuchCapability, RightsNotMonotonic
from .rights import Rights


@dataclass
class MapNode:
    """One delegation of one object to one holder."""

    id: int
    oid: int
    rights: Rights
    holder: str
    slot: int
    parent: int | None = None
    children: set[int] = field(default_factory=set)
    #: Set instead of deleting, so an audit trail can still resolve old ids.
    revoked: bool = False
    #: Optional cleanup to run when this mapping dies -- used by dataspace maps
    #: that materialised a file into a container's /shared.
    on_revoke: str | None = None

    def summary(self) -> dict:
        return {
            "id": self.id,
            "oid": self.oid,
            "holder": self.holder,
            "slot": self.slot,
            "rights": self.rights.names(),
            "parent": self.parent,
            "children": sorted(self.children),
            "revoked": self.revoked,
        }


class MappingDB:
    """The delegation forest."""

    def __init__(self) -> None:
        self._nodes: dict[int, MapNode] = {}
        self._ids = itertools.count(1)

    # -- construction ----------------------------------------------------

    def insert_root(
        self, oid: int, holder: str, slot: int, rights: Rights,
        on_revoke: str | None = None,
    ) -> MapNode:
        """Mint a fresh root capability.

        Only the kernel calls this, and only for the operator's task or for a
        container's initial table as declared in its config.  Nothing an agent
        can invoke reaches it -- which is what keeps the forest's roots under
        the operator's control.
        """
        node = MapNode(
            id=next(self._ids), oid=oid, rights=rights,
            holder=holder, slot=slot, on_revoke=on_revoke,
        )
        self._nodes[node.id] = node
        return node

    def map(
        self, parent_id: int, holder: str, slot: int, rights: Rights,
        on_revoke: str | None = None,
    ) -> MapNode:
        """Derive a child mapping from `parent_id`.

        Raises `RightsNotMonotonic` if `rights` is not a subset of the parent's
        -- the single check that keeps authority from being amplified in
        transit.
        """
        parent = self.get(parent_id)
        if rights not in parent.rights:
            excess = Rights(rights.value & ~parent.rights.value)
            raise RightsNotMonotonic(
                f"cannot delegate {excess}: the delegator only holds {parent.rights}"
            )
        node = MapNode(
            id=next(self._ids), oid=parent.oid, rights=rights,
            holder=holder, slot=slot, parent=parent.id, on_revoke=on_revoke,
        )
        self._nodes[node.id] = node
        parent.children.add(node.id)
        return node

    # -- lookup ----------------------------------------------------------

    def get(self, node_id: int) -> MapNode:
        node = self._nodes.get(node_id)
        if node is None or node.revoked:
            raise NoSuchCapability(f"mapping {node_id} does not exist")
        return node

    def subtree(self, node_id: int) -> list[MapNode]:
        """`node_id` and every mapping derived from it, parents before children."""
        root = self._nodes.get(node_id)
        if root is None:
            return []
        out: list[MapNode] = []
        stack = [root]
        while stack:
            node = stack.pop()
            out.append(node)
            stack.extend(
                self._nodes[c] for c in node.children if c in self._nodes
            )
        return out

    def nodes_for_holder(self, holder: str) -> list[MapNode]:
        return [
            n for n in self._nodes.values()
            if n.holder == holder and not n.revoked
        ]

    def nodes_for_object(self, oid: int) -> list[MapNode]:
        return [n for n in self._nodes.values() if n.oid == oid and not n.revoked]

    # -- revocation ------------------------------------------------------

    def revoke(self, node_id: int, include_self: bool = True) -> list[MapNode]:
        """Revoke a mapping and everything derived from it.

        Returns the nodes that were live and are now dead, so the caller can
        drop the corresponding capability-table slots and run any `on_revoke`
        side effects.  Already-revoked nodes are not returned twice, which makes
        repeated revocation harmless.
        """
        doomed = self.subtree(node_id)
        if not include_self:
            doomed = [n for n in doomed if n.id != node_id]

        killed: list[MapNode] = []
        for node in doomed:
            if node.revoked:
                continue
            node.revoked = True
            killed.append(node)
            if node.parent is not None and (parent := self._nodes.get(node.parent)):
                parent.children.discard(node.id)
        return killed

    def revoke_holder(self, holder: str) -> list[MapNode]:
        """Revoke everything `holder` was given. Used when a container is destroyed.

        Note this also kills anything the holder had passed on, which is the
        point: a destroyed container should not leave authority behind it.
        """
        killed: list[MapNode] = []
        for node in list(self.nodes_for_holder(holder)):
            killed.extend(self.revoke(node.id))
        return killed

    def revoke_object(self, oid: int) -> list[MapNode]:
        """Revoke every mapping of an object, everywhere. The big red button."""
        killed: list[MapNode] = []
        for node in list(self.nodes_for_object(oid)):
            killed.extend(self.revoke(node.id))
        return killed

    # -- introspection ---------------------------------------------------

    def ancestry(self, node_id: int) -> list[MapNode]:
        """Chain from `node_id` back to its root: the provenance of a capability.

        Answers "how did this agent come to hold this?", which is the question
        you actually want when reviewing an audit log.
        """
        chain: list[MapNode] = []
        current = self._nodes.get(node_id)
        while current is not None:
            chain.append(current)
            current = (
                self._nodes.get(current.parent) if current.parent is not None else None
            )
        return chain

    def all_nodes(self) -> list[MapNode]:
        return list(self._nodes.values())

    def __len__(self) -> int:
        return sum(1 for n in self._nodes.values() if not n.revoked)
