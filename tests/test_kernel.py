"""The capability model's load-bearing invariants.

These are the tests that matter most in the project: if monotonicity or
recursive revocation is wrong, the whole permission story is decorative.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from capwrap.config import load_config_data
from capwrap.errors import (
    InsufficientRights,
    NoSuchCapability,
    QuotaExceeded,
    RightsNotMonotonic,
)
from capwrap.kernel.kernel import ROOT, CapKernel
from capwrap.kernel.rights import Rights, parse_rights


def config(name: str, **caps) -> object:
    return load_config_data({"name": name, "caps": caps}, base_dir=Path("/tmp"))


@pytest.fixture
def kernel():
    return CapKernel()


def slot_labelled(kernel: CapKernel, actor: str, label: str) -> int:
    for info in kernel.cap_list(actor):
        if info.label == label:
            return info.slot
    raise AssertionError(f"{actor} holds no capability labelled {label!r}")


# ==========================================================================
# rights arithmetic
# ==========================================================================


def test_rights_containment_is_subset_not_intersection():
    held = Rights.SEND | Rights.INSPECT
    assert Rights.SEND in held
    assert (Rights.SEND | Rights.INSPECT) in held
    assert (Rights.SEND | Rights.KILL) not in held, "partial overlap must not pass"


def test_rights_round_trip_through_names():
    mask = parse_rights(["send", "inspect", "delegate"])
    assert parse_rights(mask.names()) == mask


# ==========================================================================
# no ambient authority
# ==========================================================================


def test_a_container_starts_with_only_self_and_the_operator(kernel):
    kernel.register_container(config("lonely"))
    labels = {c.label for c in kernel.cap_list("lonely")}
    assert labels == {"self", "operator"}


def test_you_cannot_act_on_a_container_you_hold_no_capability_for(kernel):
    kernel.register_container(config("a"))
    kernel.register_container(config("b"))

    # 'a' holds no capability on 'b', and has no way to name it. Probing slots
    # is the only option, and every slot either belongs to something else or is
    # empty.
    with pytest.raises(NoSuchCapability):
        kernel.msg_send("a", 99, "hello?")


def test_rights_are_checked_per_operation_not_per_object(kernel):
    kernel.register_container(config("a", peers=[{"container": "b", "rights": ["send"]}]))
    kernel.register_container(config("b"))
    kernel.link_peers(config("a", peers=[{"container": "b", "rights": ["send"]}]))

    slot = slot_labelled(kernel, "a", "peer:b")
    kernel.msg_send("a", slot, "hi")  # SEND: fine

    with pytest.raises(InsufficientRights, match="kill"):
        kernel.ctr_kill("a", slot)


def test_denials_are_audited(kernel):
    kernel.register_container(config("a", peers=[{"container": "b", "rights": ["send"]}]))
    kernel.register_container(config("b"))
    kernel.link_peers(config("a", peers=[{"container": "b", "rights": ["send"]}]))

    with pytest.raises(InsufficientRights):
        kernel.ctr_kill("a", slot_labelled(kernel, "a", "peer:b"))

    denied = kernel.audit.tail(denied_only=True)
    assert denied and denied[0]["op"] == "ctr.kill"
    assert denied[0]["actor"] == "a"


# ==========================================================================
# monotonicity
# ==========================================================================


def test_delegation_may_diminish_rights(kernel):
    peers = [{"container": "b", "rights": ["send", "inspect", "delegate"]}]
    kernel.register_container(config("a", peers=peers))
    kernel.register_container(config("b", peers=[{"container": "c", "rights": ["send"]}]))
    kernel.register_container(config("c"))
    kernel.link_peers(config("a", peers=peers))

    a_to_b = slot_labelled(kernel, "a", "peer:b")
    result = kernel.cap_delegate("a", a_to_b, a_to_b, ["send"])

    granted = [c for c in kernel.cap_list("b") if c.slot == result["slot"]][0]
    assert granted.rights == ["send"], "rights should have been narrowed"


def test_delegation_cannot_amplify_rights(kernel):
    """The single most important check in the system."""
    peers = [{"container": "b", "rights": ["send", "delegate"]}]
    kernel.register_container(config("a", peers=peers))
    kernel.register_container(config("b"))
    kernel.link_peers(config("a", peers=peers))

    a_to_b = slot_labelled(kernel, "a", "peer:b")
    with pytest.raises(RightsNotMonotonic, match="kill"):
        kernel.cap_delegate("a", a_to_b, a_to_b, ["send", "kill"])


def test_a_non_delegatable_capability_cannot_be_passed_on(kernel):
    """Holding a right is not the same as being allowed to share it."""
    peers = [{"container": "b", "rights": ["send"]}]  # no 'delegate'
    kernel.register_container(config("a", peers=peers))
    kernel.register_container(config("b"))
    kernel.link_peers(config("a", peers=peers))

    a_to_b = slot_labelled(kernel, "a", "peer:b")
    with pytest.raises(InsufficientRights, match="delegate"):
        kernel.cap_delegate("a", a_to_b, a_to_b, ["send"])


def test_authority_cannot_grow_along_a_delegation_chain(kernel):
    """A -> B -> C, with rights shrinking at each hop and never recovering."""
    kernel.register_container(config("a"))
    kernel.register_container(config("b"))
    kernel.register_container(config("c"))
    kernel.register_container(config("target"))

    target = kernel.find_container("target")
    full = Rights.SEND | Rights.INSPECT | Rights.KILL | Rights.DELEGATE
    a_slot = kernel._delegate_from_root(kernel.tasks["a"], target.oid, full, "target")

    a_to_b = kernel._delegate_from_root(
        kernel.tasks["a"], kernel.find_container("b").oid,
        Rights.SEND | Rights.DELEGATE, "peer:b",
    )
    handed = kernel.cap_delegate("a", a_to_b, a_slot, ["send", "inspect", "delegate"])
    b_slot = handed["slot"]

    b_to_c = kernel._delegate_from_root(
        kernel.tasks["b"], kernel.find_container("c").oid,
        Rights.SEND | Rights.DELEGATE, "peer:c",
    )
    # B never received KILL, so it cannot give KILL to C even though the
    # capability's root at the operator does carry it.
    with pytest.raises(RightsNotMonotonic):
        kernel.cap_delegate("b", b_to_c, b_slot, ["send", "kill"])


# ==========================================================================
# revocation
# ==========================================================================


def test_revocation_is_recursive_through_the_whole_chain(kernel):
    """Revoking at A must also strip C, which A never dealt with directly."""
    for name in ("a", "b", "c", "target"):
        kernel.register_container(config(name))

    target = kernel.find_container("target")
    shareable = Rights.SEND | Rights.INSPECT | Rights.DELEGATE
    a_slot = kernel._delegate_from_root(kernel.tasks["a"], target.oid, shareable, "target")

    a_to_b = kernel._delegate_from_root(
        kernel.tasks["a"], kernel.find_container("b").oid,
        Rights.SEND | Rights.DELEGATE, "peer:b")
    b_slot = kernel.cap_delegate("a", a_to_b, a_slot, ["send", "delegate"])["slot"]

    b_to_c = kernel._delegate_from_root(
        kernel.tasks["b"], kernel.find_container("c").oid,
        Rights.SEND | Rights.DELEGATE, "peer:c")
    c_slot = kernel.cap_delegate("b", b_to_c, b_slot, ["send"])["slot"]

    assert kernel.tasks["b"].slots.get(b_slot) is not None
    assert kernel.tasks["c"].slots.get(c_slot) is not None

    result = kernel.cap_revoke("a", a_slot)

    assert kernel.tasks["b"].slots.get(b_slot) is None, "B kept a revoked capability"
    assert kernel.tasks["c"].slots.get(c_slot) is None, "C kept a transitively revoked one"
    assert kernel.tasks["a"].slots.get(a_slot) is not None, "A should keep its own"
    assert set(result["holders"]) == {"b", "c"}


def test_revoke_including_self_drops_the_actors_own_capability(kernel):
    kernel.register_container(config("a"))
    kernel.register_container(config("target"))
    target = kernel.find_container("target")
    slot = kernel._delegate_from_root(kernel.tasks["a"], target.oid, Rights.SEND, "t")

    kernel.cap_revoke("a", slot, include_self=True)
    assert kernel.tasks["a"].slots.get(slot) is None


def test_a_revoked_slot_is_indistinguishable_from_an_empty_one(kernel):
    kernel.register_container(config("a"))
    kernel.register_container(config("target"))
    target = kernel.find_container("target")
    slot = kernel._delegate_from_root(kernel.tasks["a"], target.oid, Rights.SEND, "t")
    kernel.cap_revoke("a", slot, include_self=True)

    with pytest.raises(NoSuchCapability) as revoked:
        kernel.msg_send("a", slot, "x")
    with pytest.raises(NoSuchCapability) as never:
        kernel.msg_send("a", 4242, "x")
    assert type(revoked.value) is type(never.value)


def test_revoking_twice_is_harmless(kernel):
    kernel.register_container(config("a"))
    kernel.register_container(config("target"))
    target = kernel.find_container("target")
    slot = kernel._delegate_from_root(kernel.tasks["a"], target.oid, Rights.SEND, "t")

    first = kernel.cap_revoke("a", slot, include_self=True)
    assert first["revoked"] == 1
    with pytest.raises(NoSuchCapability):
        kernel.cap_revoke("a", slot)


def test_destroying_a_container_revokes_what_it_passed_on(kernel):
    for name in ("a", "b", "target"):
        kernel.register_container(config(name))
    target = kernel.find_container("target")
    a_slot = kernel._delegate_from_root(
        kernel.tasks["a"], target.oid, Rights.SEND | Rights.DELEGATE, "t")
    a_to_b = kernel._delegate_from_root(
        kernel.tasks["a"], kernel.find_container("b").oid,
        Rights.SEND | Rights.DELEGATE, "peer:b")
    b_slot = kernel.cap_delegate("a", a_to_b, a_slot, ["send"])["slot"]

    kernel.destroy_container("a")
    assert kernel.tasks["b"].slots.get(b_slot) is None, \
        "a destroyed container must not leave authority behind"


def test_the_operator_can_always_revoke_everything(kernel):
    kernel.register_container(config("a"))
    target = kernel.find_container("a")
    kernel.register_container(config("b"))
    kernel._delegate_from_root(kernel.tasks["b"], target.oid, Rights.SEND, "a")

    killed = kernel.mapdb.revoke_object(target.oid)
    kernel._apply_revocations(killed)
    assert not any(c.label == "a" for c in kernel.cap_list("b"))


def test_capability_provenance_is_traceable(kernel):
    """You can always answer 'how did this agent come to hold this?'."""
    kernel.register_container(config("a"))
    kernel.register_container(config("b"))
    target = kernel.find_container("b")
    slot = kernel._delegate_from_root(kernel.tasks["a"], target.oid, Rights.SEND, "b")

    chain = kernel.mapdb.ancestry(kernel.tasks["a"].slots[slot].node)
    assert [n.holder for n in chain] == ["a", ROOT]


# ==========================================================================
# factories
# ==========================================================================


def test_factory_quota_is_enforced(kernel):
    kernel.register_container(
        config("parent", factory={"rights": ["create"], "quota": {"containers": 1}})
    )
    slot = slot_labelled(kernel, "parent", "factory")

    spawned = []

    class Hooks(type(kernel.hooks)):
        def spawn_container(self, cfg, parent):
            spawned.append(cfg.name)
            return kernel.register_container(cfg, parent=parent)

    kernel.hooks = Hooks()
    kernel.ctr_spawn("parent", slot, config("child-1"))
    assert spawned == ["child-1"]

    with pytest.raises(QuotaExceeded, match="allowance"):
        kernel.ctr_spawn("parent", slot, config("child-2"))


def test_spawning_without_a_factory_capability_is_denied(kernel):
    kernel.register_container(config("plain"))
    with pytest.raises(NoSuchCapability):
        kernel.ctr_spawn("plain", 99, config("child"))


def test_a_child_cannot_be_given_authority_its_parent_lacks(kernel):
    """The factory hole: spawning must not be an authority-amplification path."""
    kernel.register_container(config("secret"))
    kernel.register_container(
        config("parent", factory={"rights": ["create"], "quota": {"containers": 2}})
    )

    # 'parent' holds no capability on 'secret', so it cannot hand one to a child.
    child = config("child", peers=[{"container": "secret", "rights": ["send"]}])
    with pytest.raises(InsufficientRights, match="holds none itself"):
        kernel.register_container(child, parent="parent")


def test_a_child_inherits_only_what_its_parent_delegates(kernel):
    kernel.register_container(config("peer"))
    parent_cfg = config(
        "parent",
        factory={"rights": ["create"], "quota": {"containers": 1}},
        peers=[{"container": "peer", "rights": ["send", "inspect", "delegate"]}],
    )
    kernel.register_container(parent_cfg)

    child = config("child", peers=[{"container": "peer", "rights": ["send"]}])
    kernel.register_container(child, parent="parent")

    granted = [c for c in kernel.cap_list("child") if c.label == "peer:peer"]
    assert granted and granted[0].rights == ["send"]

    # And it cannot ask for more than the parent has.
    greedy = config("greedy", peers=[{"container": "peer", "rights": ["send", "kill"]}])
    with pytest.raises(RightsNotMonotonic):
        kernel.register_container(greedy, parent="parent")


# ==========================================================================
# the operator channel
# ==========================================================================


def test_every_container_can_always_reach_the_operator(kernel):
    kernel.register_container(config("a"))
    asked = []
    kernel.hooks.deliver_message = lambda target, msg: asked.append((target, msg))

    kernel.ask("a", "may I write to /etc?")
    assert asked and asked[0][0] == "operator"
    assert asked[0][1]["payload"]["question"] == "may I write to /etc?"


def test_container_tree_reflects_parentage(kernel):
    kernel.register_container(
        config("root-agent", factory={"rights": ["create"], "quota": {"containers": 1}})
    )
    kernel.register_container(config("child"), parent="root-agent")

    tree = kernel.container_tree()
    top = [n for n in tree if n["name"] == "root-agent"][0]
    assert [c["name"] for c in top["children"]] == ["child"]
