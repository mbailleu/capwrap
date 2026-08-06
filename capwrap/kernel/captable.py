"""Per-task capability tables.

A `Task` is one principal in the system: a container, or the operator (the
`root` task, which the web UI acts as).  Its capability table maps small
integers -- *slots* -- to `CapRef`s.

Slots are local names.  Agent A's slot 3 and agent B's slot 3 have nothing to do
with each other, and neither agent can say anything about an object it does not
hold a slot for.  This is the difference between a capability system and an
access-control list: authority travels with the reference, so there is no
ambient namespace to be enumerated or guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import CapTableFull, InsufficientRights, NoSuchCapability
from .objects import CapRef
from .rights import Rights

#: Slot 0 is never handed out, so a zero slot is always an error rather than a
#: valid-but-surprising reference to whatever happened to be allocated first.
FIRST_SLOT = 1
DEFAULT_CAPACITY = 4096


@dataclass
class Task:
    """A principal and everything it is allowed to do."""

    name: str
    slots: dict[int, CapRef] = field(default_factory=dict)
    capacity: int = DEFAULT_CAPACITY
    #: True for the operator's task, which the kernel trusts implicitly.
    is_root: bool = False

    # -- slot management -------------------------------------------------

    def _free_slot(self) -> int:
        """Lowest unused slot, so tables stay compact and readable."""
        for candidate in range(FIRST_SLOT, self.capacity + FIRST_SLOT):
            if candidate not in self.slots:
                return candidate
        raise CapTableFull(f"{self.name}: capability table is full")

    def insert(self, ref: CapRef, slot: int | None = None) -> int:
        """Place `ref` in `slot`, or in the lowest free slot."""
        if slot is None:
            slot = self._free_slot()
        elif slot in self.slots:
            raise CapTableFull(f"{self.name}: slot {slot} is already occupied")
        self.slots[slot] = ref
        return slot

    def remove(self, slot: int) -> CapRef | None:
        return self.slots.pop(slot, None)

    def remove_node(self, node: int) -> list[int]:
        """Drop every slot backed by mapping-database node `node`.

        Used by revocation, which works in terms of mappings rather than slots.
        """
        doomed = [s for s, ref in self.slots.items() if ref.node == node]
        for slot in doomed:
            del self.slots[slot]
        return doomed

    # -- lookup ----------------------------------------------------------

    def get(self, slot: int) -> CapRef:
        """The capability in `slot`, or `NoSuchCapability`.

        A revoked capability is indistinguishable from one that was never
        granted -- deliberately, so that revocation leaks nothing about what
        used to be there.
        """
        try:
            return self.slots[slot]
        except KeyError:
            raise NoSuchCapability(f"{self.name}: no capability in slot {slot}") from None

    def require(self, slot: int, needed: Rights) -> CapRef:
        """Look up `slot` and check it carries `needed`.

        Every privileged operation in the kernel funnels through here.
        """
        ref = self.get(slot)
        if not ref.allows(needed):
            missing = Rights(needed.value & ~ref.rights.value)
            raise InsufficientRights(
                f"{self.name}: slot {slot} lacks {missing} (holds {ref.rights})"
            )
        return ref

    def find(self, oid: int) -> int | None:
        """First slot referring to `oid`, if the task holds one.

        Only used to avoid handing a task a second slot for something it already
        has; it is never exposed to agents, which have no way to ask "do I hold
        a capability on object N?" since they cannot name N.
        """
        for slot, ref in self.slots.items():
            if ref.oid == oid:
                return slot
        return None

    def rights_on(self, oid: int) -> Rights:
        """Union of rights this task holds on `oid` across all its slots."""
        mask = Rights.NONE
        for ref in self.slots.values():
            if ref.oid == oid:
                mask |= ref.rights
        return mask

    def __len__(self) -> int:
        return len(self.slots)
