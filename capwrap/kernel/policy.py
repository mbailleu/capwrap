"""Permission policies as a lattice, so delegation can be checked.

The lattice is capwrap's normalized vocabulary for tool permissions; per-agent
encoders translate it into each agent's native shape (`to_settings` for Claude
Code, `to_opencode` for opencode).

A config can set an agent's permissions (`allow` / `ask` / `deny`).
That is useful and also the obvious escalation hole: a container that spawns a
child could hand the child a policy far more permissive than its own, and the
capability system would never notice, because from the kernel's point of view a
spawn is just a spawn.

The fix reuses the idea the rest of the system already runs on. Rights can only
be *diminished* when delegated; permission policies get the same treatment. We
define "no more permissive than" as a partial order over policies and require
`child ⊑ parent's envelope`. A child that only narrows -- drops allows, adds
denies, downgrades allow to ask -- passes automatically and needs no human.
Anything that genuinely widens authority is what reaches the operator.

That answers "can we make this less human-invasive?": most real changes are
narrowing, and narrowing is provably safe, so the operator only sees the cases
that are actually escalations. And even those can be pre-authorised by giving a
container an `envelope` wider than its own policy -- the operator says once
"children may go this far" instead of clicking every time.

**Conservative by construction.** `covers()` returns False whenever it cannot
prove containment. An unprovable case becomes an approval prompt, never a silent
allow. Being wrong in that direction costs a click; the other direction costs
the whole guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

#: Claude's permission modes, ordered from most restrictive to most permissive.
#: A child may not select a mode above its parent's.
MODE_ORDER = ["plan", "default", "acceptEdits", "bypassPermissions"]


def mode_rank(mode: str | None) -> int:
    if mode is None:
        return MODE_ORDER.index("default")
    try:
        return MODE_ORDER.index(mode)
    except ValueError:
        # Unknown mode: treat as maximally permissive so it cannot sneak past.
        return len(MODE_ORDER)


def _opencode_pattern(pattern: str | None) -> str | None:
    """A rule's pattern in opencode's glob dialect, or None for a bare rule.

    Claude writes prefixes as `git log:*` ("this command plus any args");
    opencode matches plain globs, where the colon is a literal and the rule
    would never fire.  Rewriting the trailing ":*" to "*" keeps one policy
    fragment alive in both dialects.
    """
    if pattern is None:
        return None
    if pattern.endswith(":*"):
        return pattern[:-2] + "*"
    return pattern


@dataclass(frozen=True)
class Rule:
    """One permission rule: a tool, optionally narrowed by a pattern.

    ``Bash(git status:*)``, ``Read(docs/**)``, or a bare ``Read``.
    """

    tool: str
    pattern: str | None = None

    @classmethod
    def parse(cls, text: str) -> "Rule":
        text = text.strip()
        if text.endswith(")") and "(" in text:
            tool, _, rest = text.partition("(")
            return cls(tool.strip(), rest[:-1].strip())
        return cls(text, None)

    def __str__(self) -> str:
        return self.tool if self.pattern is None else f"{self.tool}({self.pattern})"

    # -- containment ---------------------------------------------------

    def covers(self, other: "Rule") -> bool:
        """True when everything `other` permits, `self` already permits.

        Returns False whenever containment cannot be *proved*, so novel or
        complex patterns fall through to the operator rather than being waved
        past.
        """
        if self.tool != other.tool:
            return False
        if self.pattern is None:
            # A bare tool rule permits every use of that tool.
            return True
        if other.pattern is None:
            # `Bash(git *)` does not cover bare `Bash`.
            return False

        mine = _normalise(self.pattern)
        theirs = _normalise(other.pattern)
        if mine == theirs:
            return True

        my_prefix = _prefix_of(mine)
        if my_prefix is None:
            # Not a simple "literal text then *" pattern; refuse to reason.
            return False
        if my_prefix == "":
            return True  # bare `*` covers anything for this tool

        their_prefix = _prefix_of(theirs)
        if their_prefix is None:
            # `self` is a prefix rule but `other` is complex; only safe if the
            # complex pattern cannot escape the prefix.
            return theirs.startswith(my_prefix)
        return their_prefix.startswith(my_prefix)


def _normalise(pattern: str) -> str:
    """Fold the equivalent spellings Claude accepts into one form.

    ``git status:*`` and ``git status *`` and ``git status*`` all mean "git
    status with any arguments".
    """
    text = pattern.strip()
    if text.endswith(":*"):
        text = text[:-2] + "*"
    # `foo *` and `foo*` differ only in whether a space is required before the
    # arguments; for containment they describe the same prefix.
    if text.endswith(" *"):
        text = text[:-2] + "*"
    return text


def _prefix_of(pattern: str) -> str | None:
    """Literal prefix of a `text*` pattern, or None if it is more complex."""
    if "*" not in pattern and "?" not in pattern and "[" not in pattern:
        return pattern  # a literal is its own prefix
    if pattern.count("*") == 1 and pattern.endswith("*") and "?" not in pattern:
        return pattern[:-1]
    return None


def _covered_by(rule: Rule, rules: Iterable[Rule]) -> bool:
    return any(candidate.covers(rule) for candidate in rules)


@dataclass
class Policy:
    """A Claude permission policy, plus capwrap's own auto-decisions."""

    allow: tuple[Rule, ...] = ()
    ask: tuple[Rule, ...] = ()
    deny: tuple[Rule, ...] = ()
    default_mode: str | None = None

    @classmethod
    def from_lists(
        cls,
        allow: Iterable[str] = (),
        ask: Iterable[str] = (),
        deny: Iterable[str] = (),
        default_mode: str | None = None,
    ) -> "Policy":
        return cls(
            allow=tuple(Rule.parse(r) for r in allow),
            ask=tuple(Rule.parse(r) for r in ask),
            deny=tuple(Rule.parse(r) for r in deny),
            default_mode=default_mode,
        )

    def to_settings(self) -> dict:
        """The `permissions` block of a Claude Code settings.json."""
        block: dict = {}
        if self.allow:
            block["allow"] = [str(r) for r in self.allow]
        if self.ask:
            block["ask"] = [str(r) for r in self.ask]
        if self.deny:
            block["deny"] = [str(r) for r in self.deny]
        if self.default_mode:
            block["defaultMode"] = self.default_mode
        return block

    def to_opencode(self) -> dict:
        """The `permission` block of an opencode config.

        opencode's permission object maps a tool name to either an effect
        ("allow"/"ask"/"deny") or, for a patterned rule, a map of pattern to
        effect.  Rules are emitted allow first, then ask, then deny last within
        each tool, because opencode resolves overlapping patterns last-match-wins
        and capwrap's semantics treat deny as absolute: a deny must always win
        over an allow for the same tool.

        Patterns are normalized to opencode's glob dialect: a trailing ":*"
        (Claude's prefix convention) is rewritten to "*", so `Bash(git log:*)`
        arrives as `git log*` and actually matches.  Tool names are lowercased
        to opencode's convention.

        `default_mode` has no opencode equivalent, so it is dropped here.
        """
        block: dict = {}
        for effect, rules in (
            ("allow", self.allow),
            ("ask", self.ask),
            ("deny", self.deny),
        ):
            for rule in rules:
                tool = rule.tool.lower()
                pattern = _opencode_pattern(rule.pattern)
                if pattern is None:
                    if isinstance(block.get(tool), dict):
                        # A bare rule alongside patterned ones for the same
                        # tool: promote it to the catch-all key so nothing is
                        # lost.
                        block[tool]["*"] = effect
                    else:
                        block[tool] = effect
                else:
                    existing = block.get(tool)
                    if not isinstance(existing, dict):
                        # A bare rule for this tool was emitted earlier; fold it
                        # into the object under the catch-all key so the
                        # patterned rule does not clobber it.
                        block[tool] = {"*": existing} if existing is not None else {}
                    block[tool][pattern] = effect
        return block

    @property
    def is_empty(self) -> bool:
        return not (self.allow or self.ask or self.deny or self.default_mode)


@dataclass
class PolicyDiff:
    """Why one policy is more permissive than another."""

    widened_allow: list[Rule] = field(default_factory=list)
    widened_ask: list[Rule] = field(default_factory=list)
    dropped_deny: list[Rule] = field(default_factory=list)
    undenied: list[Rule] = field(default_factory=list)
    mode_escalation: tuple[str, str] | None = None

    @property
    def ok(self) -> bool:
        return not (
            self.widened_allow
            or self.widened_ask
            or self.dropped_deny
            or self.undenied
            or self.mode_escalation
        )

    def reasons(self) -> list[str]:
        out: list[str] = []
        for rule in self.widened_allow:
            out.append(f"allows {rule}, which the parent does not allow")
        for rule in self.widened_ask:
            out.append(f"may ask for {rule}, which the parent cannot permit at all")
        for rule in self.dropped_deny:
            out.append(f"drops the parent's deny rule {rule}")
        for rule in self.undenied:
            out.append(f"permits {rule}, which the parent explicitly denies")
        if self.mode_escalation:
            child, parent = self.mode_escalation
            out.append(f"requests permission mode {child!r}, above the parent's {parent!r}")
        return out

    def __str__(self) -> str:  # pragma: no cover - display only
        return "; ".join(self.reasons()) or "no escalation"


def contains(parent: Policy, child: Policy) -> PolicyDiff:
    """Check `child ⊑ parent`: is the child no more permissive?

    Four ways a child can widen authority, all caught here:

    1. allowing something the parent does not allow;
    2. asking for something the parent has no way to permit;
    3. dropping a deny rule the parent carries;
    4. allowing or asking for something the parent explicitly denies.

    Narrowing -- fewer allows, more denies, allow downgraded to ask -- produces
    an empty diff and needs no approval.
    """
    diff = PolicyDiff()

    for rule in child.allow:
        if not _covered_by(rule, parent.allow):
            diff.widened_allow.append(rule)
        if _covered_by(rule, parent.deny):
            diff.undenied.append(rule)

    # `ask` is weaker than `allow`: the human still decides. So a child may ask
    # for anything the parent could allow or ask for -- but not for something
    # the parent denies outright.
    permitted = parent.allow + parent.ask
    for rule in child.ask:
        if not _covered_by(rule, permitted):
            diff.widened_ask.append(rule)
        if _covered_by(rule, parent.deny):
            diff.undenied.append(rule)

    for rule in parent.deny:
        if not _covered_by(rule, child.deny):
            diff.dropped_deny.append(rule)

    if mode_rank(child.default_mode) > mode_rank(parent.default_mode):
        diff.mode_escalation = (
            child.default_mode or "default", parent.default_mode or "default",
        )

    return diff
