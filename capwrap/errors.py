"""Exception hierarchy.

The split matters for the IPC layer: `CapabilityError` and its subclasses are the
ones an agent is allowed to see (they are answers to a request it made), while
everything else is a host-side fault that gets logged and reported as a generic
failure so sandbox internals do not leak into a container.
"""

from __future__ import annotations


class CapwrapError(Exception):
    """Base for everything raised by capwrap."""


class ConfigError(CapwrapError):
    """A container config file is malformed or refers to something that isn't there."""


class ProbeError(CapwrapError):
    """The host is missing something capwrap needs (bwrap, overlay support, ...)."""


class SandboxError(CapwrapError):
    """Constructing or launching a sandbox failed."""


class CapabilityError(CapwrapError):
    """Base for denials that are safe to report back to an agent."""

    #: Short stable string sent over IPC, so guests can branch on it.
    code = "cap_error"


class NoSuchCapability(CapabilityError):
    """The slot is empty, or names an object that has since been revoked."""

    code = "no_such_cap"


class InsufficientRights(CapabilityError):
    """The capability exists but does not carry the right this operation needs."""

    code = "insufficient_rights"


class RightsNotMonotonic(CapabilityError):
    """A delegation asked for rights the delegator does not itself hold.

    Rights may only ever be diminished when a capability is passed on; this is the
    invariant that makes the whole system analysable.
    """

    code = "rights_not_monotonic"


class QuotaExceeded(CapabilityError):
    """A factory capability has run out of its allowance."""

    code = "quota_exceeded"


class CapTableFull(CapabilityError):
    """No free slots left in a container's capability table."""

    code = "cap_table_full"
