"""Access rights, as a diminishable bitmask.

Modelled on L4Re/Fiasco.OC: a capability is an object reference *plus* a set of
rights, and the only operation permitted on rights during delegation is removal.
`Rights.__contains__` is therefore the single most important predicate in the
system -- every delegation goes through it (see `mapdb.MappingDB.map`).

Rights are namespaced per object kind but share one bitfield.  That means a right
which is meaningless for an object kind simply never gets checked for it; the
kernel validates at grant time (`validate_for`) so nonsense like `KILL` on a
dataspace is rejected where it is written rather than silently ignored.
"""

from __future__ import annotations

from enum import Flag, auto


class Rights(Flag):
    """What the holder of a capability may do with it."""

    NONE = 0

    # --- valid on every object kind -------------------------------------
    #: See that the object exists and read its status (never its contents).
    INSPECT = auto()
    #: Pass this capability on to someone else.  Without it a capability is
    #: strictly personal -- the holder may use it but cannot spread it.
    DELEGATE = auto()

    # --- Container ------------------------------------------------------
    #: Post a message to the container's mailbox.
    SEND = auto()
    #: Terminate the container.
    KILL = auto()
    #: Send it a signal short of termination (the "interrupt" in the brief).
    SIGNAL = auto()
    #: Write bytes into the container's PTY, i.e. type at its agent.
    WRITE_INPUT = auto()
    #: Read the container's output stream.
    READ_OUTPUT = auto()

    # --- Dataspace ------------------------------------------------------
    READ = auto()
    WRITE = auto()
    #: Install this dataspace into another container's filesystem.
    MAP = auto()
    #: Copy its contents into another container (weaker than MAP: no aliasing).
    COPY = auto()
    #: Create branches in a git_repo dataspace.
    BRANCH = auto()

    # --- Factory --------------------------------------------------------
    #: Create new containers, subject to the factory's quota.
    CREATE = auto()

    def __contains__(self, other: "Rights") -> bool:
        """True when `self` carries every right in `other`.

        This is the monotonicity test: a delegation is legal exactly when the
        requested rights are contained in the delegator's own rights.
        """
        return (self.value & other.value) == other.value

    def names(self) -> list[str]:
        """Lowercase right names, sorted, for display and the wire protocol."""
        return sorted(r.name.lower() for r in Rights if r.value and r in self)

    def __str__(self) -> str:  # pragma: no cover - display only
        return "|".join(self.names()) or "none"


#: Rights that make sense for each object kind.  Used to reject configs that ask
#: for a right the object could never honour.
VALID_RIGHTS: dict[str, Rights] = {
    "container": (
        Rights.INSPECT
        | Rights.DELEGATE
        | Rights.SEND
        | Rights.KILL
        | Rights.SIGNAL
        | Rights.WRITE_INPUT
        | Rights.READ_OUTPUT
    ),
    "dataspace": (
        Rights.INSPECT
        | Rights.DELEGATE
        | Rights.READ
        | Rights.WRITE
        | Rights.MAP
        | Rights.COPY
        | Rights.BRANCH
    ),
    "factory": Rights.INSPECT | Rights.DELEGATE | Rights.CREATE,
    "gate": Rights.INSPECT | Rights.DELEGATE | Rights.SEND,
}

_BY_NAME = {r.name.lower(): r for r in Rights if r.value}


def parse_rights(values: "str | list[str] | Rights | None") -> Rights:
    """Build a `Rights` mask from config/wire representations.

    Accepts a `Rights` unchanged, a comma-separated string (``"send,inspect"``),
    or a list of names.  Raises `ValueError` naming the offender on an unknown
    right, so config errors point at the typo.
    """
    if values is None:
        return Rights.NONE
    if isinstance(values, Rights):
        return values
    if isinstance(values, str):
        values = [v for v in values.replace("|", ",").split(",") if v.strip()]

    mask = Rights.NONE
    for name in values:
        key = name.strip().lower()
        if key in ("none", ""):
            continue
        if key == "all":
            for r in Rights:
                if r.value:
                    mask |= r
            continue
        try:
            mask |= _BY_NAME[key]
        except KeyError:
            raise ValueError(
                f"unknown right {name!r}; known rights: {', '.join(sorted(_BY_NAME))}"
            ) from None
    return mask


def validate_for(kind: str, rights: Rights) -> Rights:
    """Reject rights that the given object kind cannot honour."""
    allowed = VALID_RIGHTS.get(kind)
    if allowed is None:
        raise ValueError(f"unknown object kind {kind!r}")
    if rights not in allowed:
        bogus = Rights(rights.value & ~allowed.value)
        raise ValueError(f"rights {bogus} are not meaningful for a {kind}")
    return rights
