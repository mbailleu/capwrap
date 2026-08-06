"""Permission-policy containment.

Setting Claude's permissions from a config is convenient and is also an
escalation hole: a container could hand a child broader tool permissions than it
holds itself, and the capability system would not notice, because a spawn is
just a spawn.

The rule is the same one the rest of the kernel runs on -- authority may only be
diminished. These tests pin down which changes count as narrowing (silent) and
which count as widening (operator approval).
"""

from __future__ import annotations

import pytest

from capwrap.kernel.policy import Policy, Rule, contains, mode_rank

P = Policy.from_lists


def ok(parent: Policy, child: Policy) -> bool:
    return contains(parent, child).ok


# ==========================================================================
# rule subsumption
# ==========================================================================


@pytest.mark.parametrize("broad, narrow", [
    ("Bash", "Bash(git status)"),          # a bare tool covers any use of it
    ("Bash(*)", "Bash(anything at all)"),
    ("Bash(git *)", "Bash(git status)"),
    ("Bash(git *)", "Bash(git log --oneline)"),
    ("Bash(git *)", "Bash(git *)"),
    ("Bash(git:*)", "Bash(git status)"),   # `:*` and ` *` mean the same thing
    ("Bash(git *)", "Bash(git:*)"),
    ("Read(docs/**)", "Read(docs/**)"),
])
def test_broader_rule_covers_narrower(broad, narrow):
    assert Rule.parse(broad).covers(Rule.parse(narrow))


@pytest.mark.parametrize("a, b", [
    ("Bash(git status)", "Bash"),          # specific does not cover the bare tool
    ("Bash(git log *)", "Bash(git *)"),    # narrower does not cover broader
    ("Bash(git *)", "Bash(gh *)"),
    ("Read(docs/**)", "Write(docs/**)"),   # different tools never cover
    ("Bash(git *)", "Bash(sudo git status)"),
])
def test_rule_does_not_cover(a, b):
    assert not Rule.parse(a).covers(Rule.parse(b))


def test_unprovable_patterns_are_treated_as_not_covering():
    """Conservative by design: unclear means "ask", never "allow".

    Getting this backwards would turn every pattern the matcher does not
    understand into a silent escalation.
    """
    assert not Rule.parse("Bash(git * --force)").covers(Rule.parse("Bash(git push --force)"))
    assert not Rule.parse("Read(**/secret*)").covers(Rule.parse("Read(a/secret1)"))


# ==========================================================================
# narrowing is silent
# ==========================================================================


@pytest.fixture
def parent() -> Policy:
    return P(allow=["Read", "Bash(git *)"], ask=["Write"], deny=["Bash(sudo *)"])


def test_an_exact_copy_is_allowed(parent):
    assert ok(parent, P(allow=["Read", "Bash(git *)"], ask=["Write"],
                        deny=["Bash(sudo *)"]))


def test_dropping_an_allow_is_narrowing(parent):
    assert ok(parent, P(allow=["Read"], ask=["Write"], deny=["Bash(sudo *)"]))


def test_adding_a_deny_is_narrowing(parent):
    assert ok(parent, P(allow=["Read", "Bash(git *)"], ask=["Write"],
                        deny=["Bash(sudo *)", "WebFetch"]))


def test_downgrading_allow_to_ask_is_narrowing(parent):
    """The human still decides, so moving a rule into `ask` cannot escalate."""
    assert ok(parent, P(allow=["Bash(git *)"], ask=["Write", "Read"],
                        deny=["Bash(sudo *)"]))


def test_making_a_rule_more_specific_is_narrowing(parent):
    assert ok(parent, P(allow=["Read", "Bash(git log *)"], ask=["Write"],
                        deny=["Bash(sudo *)"]))


def test_an_empty_policy_is_narrowing(parent):
    assert ok(parent, P(deny=["Bash(sudo *)"]))


# ==========================================================================
# widening needs approval
# ==========================================================================


def test_allowing_a_new_tool_is_escalation(parent):
    diff = contains(parent, P(allow=["Read", "Bash(git *)", "Write"],
                              ask=["Write"], deny=["Bash(sudo *)"]))
    assert not diff.ok
    assert any("Write" in r for r in diff.reasons())


def test_broadening_a_glob_is_escalation(parent):
    diff = contains(parent, P(allow=["Read", "Bash(*)"], ask=["Write"],
                              deny=["Bash(sudo *)"]))
    assert not diff.ok
    assert diff.widened_allow


def test_replacing_a_glob_with_the_bare_tool_is_escalation(parent):
    """`Bash` is strictly stronger than `Bash(git *)`, and must not slip past."""
    diff = contains(parent, P(allow=["Read", "Bash"], ask=["Write"],
                              deny=["Bash(sudo *)"]))
    assert not diff.ok


def test_dropping_a_deny_is_escalation(parent):
    diff = contains(parent, P(allow=["Read", "Bash(git *)"], ask=["Write"]))
    assert not diff.ok
    assert diff.dropped_deny


def test_allowing_something_the_parent_denies_is_escalation(parent):
    diff = contains(parent, P(allow=["Bash(sudo apt install *)"],
                              deny=["Bash(sudo *)"]))
    assert not diff.ok
    assert diff.undenied


def test_asking_for_something_the_parent_denies_is_escalation(parent):
    """`ask` is weaker than `allow`, but not weak enough to reach a denied tool."""
    diff = contains(parent, P(ask=["Bash(sudo apt *)"], deny=["Bash(sudo *)"]))
    assert not diff.ok
    assert diff.undenied


def test_asking_for_something_the_parent_cannot_permit_is_escalation(parent):
    diff = contains(parent, P(ask=["WebFetch"], deny=["Bash(sudo *)"]))
    assert not diff.ok
    assert diff.widened_ask


# ==========================================================================
# permission modes
# ==========================================================================


def test_permission_modes_are_ordered():
    assert mode_rank("plan") < mode_rank("default") < mode_rank("acceptEdits") \
        < mode_rank("bypassPermissions")


def test_raising_the_permission_mode_is_escalation(parent):
    diff = contains(parent, P(allow=["Read", "Bash(git *)"], ask=["Write"],
                              deny=["Bash(sudo *)"],
                              default_mode="bypassPermissions"))
    assert not diff.ok
    assert diff.mode_escalation == ("bypassPermissions", "default")


def test_lowering_the_permission_mode_is_narrowing(parent):
    assert ok(parent, P(allow=["Read", "Bash(git *)"], ask=["Write"],
                        deny=["Bash(sudo *)"], default_mode="plan"))


def test_an_unknown_mode_is_treated_as_maximally_permissive():
    """A mode we do not recognise must not be assumed harmless."""
    assert mode_rank("something-new") > mode_rank("bypassPermissions")
    diff = contains(P(), P(default_mode="something-new"))
    assert not diff.ok


# ==========================================================================
# the envelope: pre-authorising a range instead of clicking every time
# ==========================================================================


def test_an_explicit_envelope_lets_a_parent_grant_beyond_its_own_policy(tmp_path):
    """The less human-invasive escape hatch.

    A container is confined to Read, but the operator has decided its children
    may also run git. That is one decision in a config file, rather than an
    approval prompt on every spawn.
    """
    from capwrap.config import load_config_data

    config = load_config_data({
        "name": "supervisor",
        "runtime": {
            "permissions": {"allow": ["Read"], "deny": ["Bash(sudo *)"]},
            "permission_envelope": {
                "allow": ["Read", "Bash(git *)"], "deny": ["Bash(sudo *)"],
            },
        },
    }, base_dir=tmp_path)

    envelope = config.runtime.envelope_policy()
    own = config.runtime.permissions.to_policy()

    # The child may exceed the parent's own policy...
    child = P(allow=["Read", "Bash(git status)"], deny=["Bash(sudo *)"])
    assert ok(envelope, child)
    assert not ok(own, child)

    # ...but not the envelope.
    assert not ok(envelope, P(allow=["Bash(*)"], deny=["Bash(sudo *)"]))


def test_the_envelope_defaults_to_the_containers_own_policy(tmp_path):
    from capwrap.config import load_config_data

    config = load_config_data({
        "name": "plain",
        "runtime": {"permissions": {"allow": ["Read"]}},
    }, base_dir=tmp_path)
    assert config.runtime.envelope_policy().to_settings() == {"allow": ["Read"]}


def test_settings_round_trip(tmp_path):
    from capwrap.config import load_config_data

    config = load_config_data({
        "name": "x",
        "runtime": {"permissions": {
            "allow": ["Read", "Bash(git *)"],
            "ask": ["Write"],
            "deny": ["Bash(sudo *)"],
            "default_mode": "acceptEdits",
        }},
    }, base_dir=tmp_path)
    assert config.runtime.permissions.to_policy().to_settings() == {
        "allow": ["Read", "Bash(git *)"],
        "ask": ["Write"],
        "deny": ["Bash(sudo *)"],
        "defaultMode": "acceptEdits",
    }


def test_an_unknown_permission_mode_is_rejected_in_config(tmp_path):
    from capwrap.config import load_config_data
    from capwrap.errors import ConfigError

    with pytest.raises(ConfigError, match="default_mode"):
        load_config_data(
            {"name": "x", "runtime": {"permissions": {"default_mode": "yolo"}}},
            base_dir=tmp_path,
        )
