"""Agent profiles: lookup, guest-side injections, and policy encoding.

These are pure functions over a validated config -- nothing touches a sandbox --
so they run anywhere.  The fsprep integration tests at the bottom drive
`prepare` exactly like test_fsprep.py does, checking which files land in the
container's state dir for each agent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from capwrap import agents
from capwrap.config import load_config_data
from capwrap.errors import ConfigError
from capwrap.kernel.policy import Policy
from capwrap.paths import (
    GUEST_HOME,
    GUEST_POLICY,
    GUEST_ROLE_PROMPT,
    GUEST_TOOLS,
    ContainerPaths,
)
from capwrap.runtime import bwrap, fsprep


def make(raw: dict, base_dir):
    return load_config_data(raw, base_dir=base_dir)


def files_by_dest(prepared) -> dict[str, Path]:
    """Map guest destination -> staged host file for a PreparedFs."""
    return {dest: src for src, dest in prepared.files}


def loads(injection: agents.Injection) -> dict:
    """Parse a content injection, asserting it is not a source copy."""
    assert injection.content is not None
    return json.loads(injection.content)


# --------------------------------------------------------------------------
# profile lookup
# --------------------------------------------------------------------------


def test_known_profile_names_resolve():
    for name in ("claude", "opencode", "opencode2", "pi", "generic"):
        assert agents.get_profile(name).name == name


def test_unknown_profile_name_raises_config_error():
    with pytest.raises(ConfigError, match="unknown agent profile"):
        agents.get_profile("copilot")


def test_profile_fields_match_the_documented_layout():
    claude = agents.get_profile("claude")
    assert claude.settings_path == f"{GUEST_HOME}/.claude/settings.json"
    assert claude.permission_encoder == "claude"
    assert claude.hook_protocol == "claude"
    assert claude.skill_path == f"{GUEST_HOME}/.claude/skills/capwrap/SKILL.md"

    opencode = agents.get_profile("opencode")
    assert opencode.settings_path == f"{GUEST_HOME}/.config/opencode/opencode.json"
    assert opencode.permission_encoder == "opencode"
    assert opencode.hook_protocol is None, \
        "v1's permission.ask is declared but never wired upstream"
    assert opencode.skill_path == f"{GUEST_HOME}/.config/opencode/skills/capwrap/SKILL.md"

    opencode2 = agents.get_profile("opencode2")
    assert opencode2.settings_path == f"{GUEST_HOME}/.config/opencode2/opencode.json", \
        "v2 reads its own config dir, not v1's ~/.config/opencode"
    assert opencode2.permission_encoder == "opencode"
    assert opencode2.hook_protocol == "opencode2"
    assert opencode2.skill_path == f"{GUEST_HOME}/.config/opencode2/skills/capwrap/SKILL.md"

    pi = agents.get_profile("pi")
    assert pi.settings_path is None
    assert pi.permission_encoder is None
    assert pi.hook_protocol == "pi"
    assert pi.skill_path == f"{GUEST_HOME}/.pi/agent/skills/capwrap/SKILL.md"

    generic = agents.get_profile("generic")
    assert generic.settings_path is None
    assert generic.permission_encoder is None
    assert generic.hook_protocol is None
    assert generic.skill_path is None


def test_unknown_agent_in_config_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="agent"):
        make({"name": "x", "runtime": {"agent": "copilot"}}, tmp_path)


# --------------------------------------------------------------------------
# Injection validation
# --------------------------------------------------------------------------


def test_injection_needs_exactly_one_of_content_or_src():
    with pytest.raises(ValueError, match="exactly one of content or src"):
        agents.Injection(staged_name="x", dest="/x", content="a", src=Path("/a"))
    with pytest.raises(ValueError, match="exactly one of content or src"):
        agents.Injection(staged_name="x", dest="/x")


def test_injection_accepts_content_or_src_alone():
    assert agents.Injection(staged_name="x", dest="/x", content="a").content == "a"
    assert agents.Injection(staged_name="x", dest="/x", src=Path("/a")).src == Path("/a")


# --------------------------------------------------------------------------
# hook injections (approvals="capwrap")
# --------------------------------------------------------------------------


def _config(tmp_path, **runtime):
    return make({"name": "a", "runtime": runtime}, tmp_path)


def test_claude_hook_wires_pretooluse_and_merges_permissions(tmp_path):
    config = _config(
        tmp_path,
        approvals="capwrap",
        auto_allow=["Read"],
        auto_deny=["Bash(sudo *)"],
        permissions={"allow": ["Glob"], "deny": ["Bash(curl *)"]},
    )
    injections = agents.guest_injections(agents.get_profile("claude"), config)

    settings = loads(injections[0])
    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook["type"] == "command"
    assert hook["command"] == f"{GUEST_TOOLS}/hook.py"
    # A non-empty policy is merged into the same settings file.
    assert settings["permissions"] == {"allow": ["Glob"], "deny": ["Bash(curl *)"]}
    assert injections[0].dest == f"{GUEST_HOME}/.claude/settings.json"

    policy = loads(injections[1])
    # Rules arrive normalised: lowercase tool names, Claude's ":*" prefix
    # syntax rewritten to a glob.  No ask rules configured -> no ask floor.
    assert policy == {
        "allow": ["read"],
        "deny": ["bash(sudo *)"],
        "fallback": "deny",
    }
    assert injections[1].dest == GUEST_POLICY


def test_claude_hook_with_empty_policy_omits_permissions(tmp_path):
    config = _config(tmp_path, approvals="capwrap")
    injections = agents.guest_injections(agents.get_profile("claude"), config)
    settings = loads(injections[0])
    assert "permissions" not in settings


def test_opencode2_hook_stages_the_real_plugin_and_policy(tmp_path):
    config = _config(tmp_path, approvals="capwrap", auto_allow=["Read"])
    injections = agents.guest_injections(agents.get_profile("opencode2"), config)

    plugin = injections[0]
    assert plugin.src is not None and plugin.src.is_file()
    assert plugin.src.name == "opencode-plugin.ts"
    assert plugin.dest == f"{GUEST_HOME}/.config/opencode2/plugins/capwrap.ts"

    policy = loads(injections[1])
    assert policy == {"allow": ["read"], "deny": [], "fallback": "deny"}
    assert injections[1].dest == GUEST_POLICY


def test_pi_hook_stages_the_real_extension_and_policy(tmp_path):
    config = _config(tmp_path, approvals="capwrap", auto_deny=["Bash(sudo *)"])
    injections = agents.guest_injections(agents.get_profile("pi"), config)

    extension = injections[0]
    assert extension.src is not None and extension.src.is_file()
    assert extension.src.name == "pi-extension.ts"
    assert extension.dest == f"{GUEST_HOME}/.pi/agent/extensions/capwrap-gate.ts"

    policy = loads(injections[1])
    assert policy == {"allow": [], "deny": ["bash(sudo *)"], "fallback": "deny"}
    assert injections[1].dest == GUEST_POLICY


@pytest.mark.parametrize("name", ["opencode", "generic"])
def test_hookless_agents_raise_for_capwrap_approvals(tmp_path, name):
    config = _config(tmp_path, approvals="capwrap")
    with pytest.raises(ConfigError, match="no approval shim"):
        agents.guest_injections(agents.get_profile(name), config)


# --------------------------------------------------------------------------
# permission injections (approvals="native")
# --------------------------------------------------------------------------


def test_claude_permission_injection_shape(tmp_path):
    config = _config(
        tmp_path,
        permissions={"allow": ["Read"], "deny": ["Bash(sudo *)"]},
    )
    injections = agents.guest_injections(agents.get_profile("claude"), config)
    assert len(injections) == 1
    assert injections[0].dest == f"{GUEST_HOME}/.claude/settings.json"
    assert loads(injections[0]) == {
        "permissions": {"allow": ["Read"], "deny": ["Bash(sudo *)"]},
    }


def test_opencode_permission_injection_lowercases_tools(tmp_path):
    config = _config(
        tmp_path,
        agent="opencode",
        permissions={"allow": ["Read", "Bash(git *)"], "deny": ["Bash(sudo *)"]},
    )
    injections = agents.guest_injections(agents.get_profile("opencode"), config)
    assert len(injections) == 1
    assert injections[0].dest == f"{GUEST_HOME}/.config/opencode/opencode.json"
    assert loads(injections[0]) == {
        "permission": {
            "read": "allow",
            "bash": {"git *": "allow", "sudo *": "deny"},
        },
    }


@pytest.mark.parametrize("name", ["pi", "generic"])
def test_agents_without_a_permission_system_raise(tmp_path, name):
    config = _config(tmp_path, permissions={"allow": ["Read"]})
    with pytest.raises(ConfigError, match="no native permission system"):
        agents.guest_injections(agents.get_profile(name), config)


# --------------------------------------------------------------------------
# guest_injections composition
# --------------------------------------------------------------------------


def _role(tmp_path, text="# you are the role\n") -> Path:
    path = tmp_path / "role.md"
    path.write_text(text)
    return path


def test_claude_capwrap_merges_permissions_into_the_hook_settings(tmp_path):
    """One settings.json carries both the hook and the permission rules."""
    config = _config(
        tmp_path,
        approvals="capwrap",
        permissions={"allow": ["Glob"], "deny": ["Bash(curl *)"]},
    )
    injections = agents.guest_injections(agents.get_profile("claude"), config)

    settings = [i for i in injections if i.dest.endswith("settings.json")]
    assert len(settings) == 1, "no separate permissions-only settings file"
    merged = loads(settings[0])
    assert "hooks" in merged
    assert merged["permissions"] == {"allow": ["Glob"], "deny": ["Bash(curl *)"]}
    assert any(i.dest == GUEST_POLICY for i in injections)


def test_claude_capwrap_merges_over_the_user_settings(tmp_path):
    """The user's own settings -- an env block routing claude through a local
    gateway -- survive the hook injection; capwrap's hooks and permissions
    replace theirs outright."""
    src = tmp_path / "claude"
    src.mkdir()
    (src / "settings.json").write_text(json.dumps({
        "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:3456",
                "ANTHROPIC_AUTH_TOKEN": "unused"},
        "model": "claude-opus-4-8",
    }))
    config = make({
        "name": "ca",
        "runtime": {
            "approvals": "capwrap",
            "permissions": {"allow": ["Read"]},
        },
        "mounts": [{"src": str(src), "dest": f"{GUEST_HOME}/.claude", "mode": "copy"}],
    }, tmp_path)
    injections = agents.guest_injections(agents.get_profile("claude"), config)

    settings = loads(injections[0])
    assert settings["env"] == {"ANTHROPIC_BASE_URL": "http://127.0.0.1:3456",
                               "ANTHROPIC_AUTH_TOKEN": "unused"}
    assert settings["model"] == "claude-opus-4-8"
    assert "hooks" in settings
    assert settings["permissions"] == {"allow": ["Read"]}


def test_claude_native_merges_over_the_user_settings_too(tmp_path):
    src = tmp_path / "claude"
    src.mkdir()
    (src / "settings.json").write_text(json.dumps({
        "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:3456"},
    }))
    config = make({
        "name": "ca",
        "runtime": {"permissions": {"allow": ["Read"]}},
        "mounts": [{"src": str(src), "dest": f"{GUEST_HOME}/.claude", "mode": "copy"}],
    }, tmp_path)
    injections = agents.guest_injections(agents.get_profile("claude"), config)
    settings = loads(injections[0])
    assert settings["env"] == {"ANTHROPIC_BASE_URL": "http://127.0.0.1:3456"}
    assert settings["permissions"] == {"allow": ["Read"]}
    assert "hooks" not in settings


def test_claude_native_permissions_produce_a_settings_only_injection(tmp_path):
    config = _config(tmp_path, permissions={"allow": ["Read"]})
    injections = agents.guest_injections(agents.get_profile("claude"), config)
    assert len(injections) == 1
    assert injections[0].dest == f"{GUEST_HOME}/.claude/settings.json"
    assert loads(injections[0]) == {"permissions": {"allow": ["Read"]}}


def test_opencode2_capwrap_permissions_compose_with_opencode_json(tmp_path):
    """The approval plugin and the permission block are different files."""
    config = _config(
        tmp_path,
        agent="opencode2",
        approvals="capwrap",
        permissions={"allow": ["Read"]},
    )
    injections = agents.guest_injections(agents.get_profile("opencode2"), config)

    dests = [i.dest for i in injections]
    assert f"{GUEST_HOME}/.config/opencode2/plugins/capwrap.ts" in dests
    assert GUEST_POLICY in dests

    settings = [i for i in injections if i.dest.endswith("opencode.json")]
    assert len(settings) == 1
    assert loads(settings[0]) == {"permission": {"read": "allow"}}


def test_opencode2_capwrap_role_prompt_gets_instructions_only(tmp_path):
    role = _role(tmp_path)
    config = _config(
        tmp_path,
        agent="opencode2",
        approvals="capwrap",
        role_prompt="role.md",
    )
    injections = agents.guest_injections(agents.get_profile("opencode2"), config)

    settings = [i for i in injections if i.dest.endswith("opencode.json")]
    assert len(settings) == 1
    assert loads(settings[0]) == {"instructions": [GUEST_ROLE_PROMPT]}

    bound = [i for i in injections if i.dest == GUEST_ROLE_PROMPT]
    assert len(bound) == 1
    assert bound[0].src == role.resolve()


def test_opencode_native_permissions_and_role_prompt_share_one_file(tmp_path):
    role = _role(tmp_path)
    config = _config(
        tmp_path,
        agent="opencode",
        permissions={"allow": ["Read"]},
        role_prompt="role.md",
    )
    injections = agents.guest_injections(agents.get_profile("opencode"), config)

    settings = [i for i in injections if i.dest.endswith("opencode.json")]
    assert len(settings) == 1
    assert loads(settings[0]) == {
        "permission": {"read": "allow"},
        "instructions": [GUEST_ROLE_PROMPT],
    }


def test_opencode_with_neither_permissions_nor_role_prompt_skips_the_file(
    tmp_path,
):
    config = _config(tmp_path, agent="opencode")
    assert agents.guest_injections(agents.get_profile("opencode"), config) == []


# --------------------------------------------------------------------------
# opencode.json read-merge-write
# --------------------------------------------------------------------------


def _opencode_config(tmp_path, **runtime):
    """A config whose mount covers opencode2's settings dir with a tmp dir.

    Returns (config, src_dir); the user's real opencode.json lives at
    ``src_dir / "opencode.json"``.  The opencode2 profile is the merge-heavy
    one (v2 reads ~/.config/opencode2), so the mount targets that dir.
    """
    src = tmp_path / "oc"
    src.mkdir()
    config = make({
        "name": "oc",
        "runtime": runtime,
        "mounts": [{
            "src": str(src),
            "dest": f"{GUEST_HOME}/.config/opencode2",
            "mode": "ro",
        }],
    }, tmp_path)
    return config, src


def _opencode_settings(config) -> dict:
    injections = agents.guest_injections(agents.get_profile("opencode2"), config)
    settings = [i for i in injections if i.dest.endswith("opencode.json")]
    assert len(settings) == 1
    return loads(settings[0])


def test_opencode_merge_preserves_user_config_and_adds_permission(tmp_path):
    config, src = _opencode_config(tmp_path, permissions={"allow": ["Read"]})
    (src / "opencode.json").write_text(json.dumps({
        "provider": {"x": {}},
        "agent": {"y": {}},
        "mcp": {"z": {}},
    }))
    assert _opencode_settings(config) == {
        "provider": {"x": {}},
        "agent": {"y": {}},
        "mcp": {"z": {}},
        "permission": {"read": "allow"},
    }


def test_opencode_merge_appends_instructions_to_the_user_array(tmp_path):
    config, src = _opencode_config(tmp_path, role_prompt="role.md")
    _role(tmp_path)
    (src / "opencode.json").write_text(json.dumps({
        "instructions": ["/home/agent/notes.md"],
    }))
    assert _opencode_settings(config) == {
        "instructions": ["/home/agent/notes.md", GUEST_ROLE_PROMPT],
    }


def test_opencode_merge_sets_instructions_when_the_user_has_none(tmp_path):
    config, src = _opencode_config(tmp_path, role_prompt="role.md")
    _role(tmp_path)
    (src / "opencode.json").write_text(json.dumps({"provider": {"x": {}}}))
    assert _opencode_settings(config) == {
        "provider": {"x": {}},
        "instructions": [GUEST_ROLE_PROMPT],
    }


def test_opencode_merge_model_overrides_the_user_model(tmp_path):
    config, src = _opencode_config(tmp_path, model="opencode-go/glm-5.3-flash")
    (src / "opencode.json").write_text(json.dumps({"model": "old"}))
    assert _opencode_settings(config) == {
        "model": "opencode-go/glm-5.3-flash",
        "agent": {"build": {"model": "opencode-go/glm-5.3-flash"}},
    }


def test_opencode_merge_preserves_user_config_and_adds_model(tmp_path):
    config, src = _opencode_config(tmp_path, model="opencode-go/glm-5.3-flash")
    (src / "opencode.json").write_text(json.dumps({"provider": {"x": {}}}))
    assert _opencode_settings(config) == {
        "provider": {"x": {}},
        "model": "opencode-go/glm-5.3-flash",
        "agent": {"build": {"model": "opencode-go/glm-5.3-flash"}},
    }


def test_opencode_merge_agent_pin_lands_inside_the_user_agent_block(tmp_path):
    """v2 ignores the legacy top-level model for built-in agents; the per-agent
    pin merges into the user's agent block without discarding their agents."""
    config, src = _opencode_config(tmp_path, model="opencode-go/glm-5.3-flash")
    (src / "opencode.json").write_text(json.dumps({
        "agent": {"orchestrator": {"model": "opencode/glm-5.3-flash"}},
    }))
    assert _opencode_settings(config) == {
        "model": "opencode-go/glm-5.3-flash",
        "agent": {
            "orchestrator": {"model": "opencode/glm-5.3-flash"},
            "build": {"model": "opencode-go/glm-5.3-flash"},
        },
    }


def test_opencode_merge_unparseable_user_file_is_a_config_error(tmp_path):
    """A file json.loads cannot read even after comment-stripping is refused,
    never shadowed: merging capwrap-only keys would delete the user's config."""
    config, src = _opencode_config(tmp_path, permissions={"allow": ["Read"]})
    (src / "opencode.json").write_text(
        '{"provider": {"x": {}}, trailing comma here,}'
    )
    with pytest.raises(ConfigError, match="not parseable"):
        _opencode_settings(config)


def test_opencode_merge_strips_jsonc_comments_and_merges(tmp_path):
    """JSONC configs (comments allowed) are read, not silently dropped."""
    config, src = _opencode_config(tmp_path, permissions={"allow": ["Read"]})
    (src / "opencode.json").write_text(
        '// my providers\n'
        '{\n'
        '  "provider": {"x": {"options": {"baseURL": "https://api.example.com/v1"}}},\n'
        '  /* block comment */\n'
        '  "mcp": {"z": {}},\n'
        '}\n'
    )
    assert _opencode_settings(config) == {
        "provider": {"x": {"options": {"baseURL": "https://api.example.com/v1"}}},
        "mcp": {"z": {}},
        "permission": {"read": "allow"},
    }


def test_policy_rules_are_normalized_for_the_guest_matchers(tmp_path):
    """Tool names lowercase, Claude's ":*" prefix syntax becomes a glob --
    decided once here, so the three shims stay case- and syntax-dumb."""
    config = _config(
        tmp_path,
        approvals="capwrap",
        auto_allow=["Read", "Bash(git log:*)"],
        auto_deny=["Bash(sudo *)"],
    )
    injections = agents.guest_injections(agents.get_profile("opencode2"), config)
    policy = loads(injections[1])
    assert policy["allow"] == ["read", "bash(git log*)"]
    assert policy["deny"] == ["bash(sudo *)"]


def test_policy_fallback_follows_the_ask_floor(tmp_path):
    """With ask rules the shim may fall through to the agent's own prompt;
    without any, an unreachable daemon must deny outright."""
    with_ask = _config(
        tmp_path,
        approvals="capwrap",
        permissions={"ask": ["Write"]},
    )
    policy = loads(
        agents.guest_injections(agents.get_profile("opencode2"), with_ask)[1]
    )
    assert policy["fallback"] == "ask"

    without_ask = _config(tmp_path, approvals="capwrap", auto_allow=["Read"])
    policy = loads(
        agents.guest_injections(agents.get_profile("opencode2"), without_ask)[1]
    )
    assert policy["fallback"] == "deny"


def test_opencode_merge_missing_user_file_falls_back_to_capwrap_keys(tmp_path):
    config, _src = _opencode_config(tmp_path, permissions={"allow": ["Read"]})
    assert _opencode_settings(config) == {"permission": {"read": "allow"}}


def test_opencode_without_covering_mount_is_a_fresh_file(tmp_path):
    config = _config(tmp_path, agent="opencode2", permissions={"allow": ["Read"]})
    assert _opencode_settings(config) == {"permission": {"read": "allow"}}


def test_opencode_model_lands_in_a_fresh_file_without_a_mount(tmp_path):
    config = _config(tmp_path, agent="opencode2", model="opencode-go/glm-5.3-flash")
    assert _opencode_settings(config) == {
        "model": "opencode-go/glm-5.3-flash",
        "agent": {"build": {"model": "opencode-go/glm-5.3-flash"}},
    }


def test_opencode_merge_reads_through_a_home_level_mount(tmp_path):
    """A mount on $HOME covers the config too; rel is computed from the dest."""
    src = tmp_path / "home"
    (src / ".config" / "opencode2").mkdir(parents=True)
    (src / ".config" / "opencode2" / "opencode.json").write_text(json.dumps({
        "provider": {"x": {}},
    }))
    config = make({
        "name": "oc",
        "runtime": {"agent": "opencode2", "permissions": {"allow": ["Read"]}},
        "mounts": [{"src": str(src), "dest": GUEST_HOME, "mode": "ro"}],
    }, tmp_path)
    assert _opencode_settings(config) == {
        "provider": {"x": {}},
        "permission": {"read": "allow"},
    }


def test_opencode_merge_deepest_covering_mount_wins(tmp_path):
    """When two mounts cover the file, the deepest one is what the sandbox sees."""
    home = tmp_path / "home"
    oc = home / ".config" / "opencode2"
    oc.mkdir(parents=True)
    (home / ".config" / "opencode2" / "opencode.json").write_text(json.dumps({
        "provider": {"shallow": {}},
    }))
    (oc / "opencode.json").write_text(json.dumps({
        "provider": {"deep": {}},
    }))
    config = make({
        "name": "oc",
        "runtime": {"agent": "opencode2", "permissions": {"allow": ["Read"]}},
        "mounts": [
            {"src": str(home), "dest": GUEST_HOME, "mode": "ro"},
            {"src": str(oc), "dest": f"{GUEST_HOME}/.config/opencode2", "mode": "ro"},
        ],
    }, tmp_path)
    assert _opencode_settings(config) == {
        "provider": {"deep": {}},
        "permission": {"read": "allow"},
    }


@pytest.mark.parametrize("approvals", ["capwrap", "native"])
def test_pi_with_permissions_raises_in_both_approval_modes(tmp_path, approvals):
    config = _config(
        tmp_path,
        agent="pi",
        approvals=approvals,
        permissions={"allow": ["Read"]},
    )
    with pytest.raises(ConfigError, match="no native permission system"):
        agents.guest_injections(agents.get_profile("pi"), config)


def test_generic_role_prompt_binds_only_the_file(tmp_path):
    role = _role(tmp_path)
    config = _config(tmp_path, agent="generic", role_prompt="role.md")
    injections = agents.guest_injections(agents.get_profile("generic"), config)
    assert len(injections) == 1
    assert injections[0].dest == GUEST_ROLE_PROMPT
    assert injections[0].src == role.resolve()


# --------------------------------------------------------------------------
# command_flags
# --------------------------------------------------------------------------


def test_command_flags_claude_and_pi_point_at_the_bound_file(tmp_path):
    _role(tmp_path)
    claude = _config(tmp_path, agent="claude", role_prompt="role.md")
    assert agents.command_flags(agents.get_profile("claude"), claude) == [
        "--append-system-prompt-file", GUEST_ROLE_PROMPT,
    ]

    pi = _config(tmp_path, agent="pi", role_prompt="role.md")
    assert agents.command_flags(agents.get_profile("pi"), pi) == [
        "--append-system-prompt", GUEST_ROLE_PROMPT,
    ]


@pytest.mark.parametrize("name", ["opencode", "opencode2", "generic"])
def test_command_flags_are_empty_for_agents_without_a_cli_flag(tmp_path, name):
    _role(tmp_path)
    config = _config(tmp_path, agent=name, role_prompt="role.md")
    assert agents.command_flags(agents.get_profile(name), config) == []


@pytest.mark.parametrize("name", ["claude", "opencode", "opencode2", "pi", "generic"])
def test_command_flags_are_empty_without_a_role_prompt(tmp_path, name):
    config = _config(tmp_path, agent=name)
    assert agents.command_flags(agents.get_profile(name), config) == []


def test_command_flags_claude_and_pi_emit_the_model_flag(tmp_path):
    claude = _config(tmp_path, agent="claude", model="opencode-go/glm-5.3-flash")
    assert agents.command_flags(agents.get_profile("claude"), claude) == [
        "--model", "opencode-go/glm-5.3-flash",
    ]

    pi = _config(tmp_path, agent="pi", model="opencode-go/glm-5.3-flash")
    assert agents.command_flags(agents.get_profile("pi"), pi) == [
        "--model", "opencode-go/glm-5.3-flash",
    ]


def test_command_flags_combine_role_prompt_and_model_for_claude(tmp_path):
    _role(tmp_path)
    config = _config(
        tmp_path,
        agent="claude",
        role_prompt="role.md",
        model="opencode-go/glm-5.3-flash",
    )
    assert agents.command_flags(agents.get_profile("claude"), config) == [
        "--append-system-prompt-file", GUEST_ROLE_PROMPT,
        "--model", "opencode-go/glm-5.3-flash",
    ]


@pytest.mark.parametrize("name", ["opencode", "opencode2", "generic"])
def test_command_flags_ignore_the_model_for_flagless_agents(tmp_path, name):
    config = _config(tmp_path, agent=name, model="opencode-go/glm-5.3-flash")
    assert agents.command_flags(agents.get_profile(name), config) == []


# --------------------------------------------------------------------------
# Policy.to_opencode()
# --------------------------------------------------------------------------


def test_to_opencode_bare_rule_is_a_flat_effect():
    assert Policy.from_lists(allow=["Read"]).to_opencode() == {"read": "allow"}


def test_to_opencode_patterned_rule_is_nested():
    assert Policy.from_lists(allow=["Bash(git *)"]).to_opencode() == {
        "bash": {"git *": "allow"},
    }


def test_to_opencode_bare_star_becomes_the_catch_all_key():
    assert Policy.from_lists(allow=["Bash(*)"]).to_opencode() == {
        "bash": {"*": "allow"},
    }


def test_to_opencode_lowercases_tool_names():
    assert Policy.from_lists(ask=["WebFetch"]).to_opencode() == {"webfetch": "ask"}


def test_to_opencode_emits_deny_after_allow_for_the_same_tool():
    block = Policy.from_lists(
        allow=["Bash(git *)"], deny=["Bash(sudo *)"],
    ).to_opencode()
    assert block == {"bash": {"git *": "allow", "sudo *": "deny"}}
    assert list(block["bash"]) == ["git *", "sudo *"], "deny must come last"


def test_to_opencode_bare_rule_alongside_patterned_promotes_to_catch_all():
    block = Policy.from_lists(allow=["Bash", "Bash(git *)"]).to_opencode()
    assert block == {"bash": {"*": "allow", "git *": "allow"}}


def test_to_opencode_drops_default_mode():
    block = Policy.from_lists(
        allow=["Read"], default_mode="bypassPermissions",
    ).to_opencode()
    assert block == {"read": "allow"}


# --------------------------------------------------------------------------
# fsprep integration
# --------------------------------------------------------------------------


def test_opencode2_capwrap_approvals_stage_plugin_policy_and_skill(
    tmp_path, state_dir
):
    config = make({
        "name": "oc2",
        "runtime": {
            "agent": "opencode2",
            "approvals": "capwrap",
            "auto_allow": ["Read"],
        },
    }, tmp_path)
    files = files_by_dest(fsprep.prepare(config, ContainerPaths("oc2")))

    plugin = files[f"{GUEST_HOME}/.config/opencode2/plugins/capwrap.ts"]
    assert plugin.is_file()
    guest_plugin = (
        Path(agents.__file__).resolve().parent / "guest" / "opencode-plugin.ts"
    )
    assert plugin.read_text() == guest_plugin.read_text()

    policy = json.loads(files[GUEST_POLICY].read_text())
    assert policy == {"allow": ["read"], "deny": [], "fallback": "deny"}

    skill = files[f"{GUEST_HOME}/.config/opencode2/skills/capwrap/SKILL.md"]
    assert skill.is_file()


def test_opencode_v1_capwrap_approvals_are_rejected(tmp_path, state_dir):
    config = make({
        "name": "oc1",
        "runtime": {"agent": "opencode", "approvals": "capwrap"},
    }, tmp_path)
    with pytest.raises(ConfigError, match="no approval shim"):
        fsprep.prepare(config, ContainerPaths("oc1"))


def test_pi_native_approvals_with_permissions_are_rejected(tmp_path, state_dir):
    config = make({
        "name": "pi",
        "runtime": {
            "agent": "pi",
            "approvals": "native",
            "permissions": {"allow": ["Read"]},
        },
    }, tmp_path)
    with pytest.raises(ConfigError, match="no native permission system"):
        fsprep.prepare(config, ContainerPaths("pi"))


def test_generic_capwrap_approvals_are_rejected(tmp_path, state_dir):
    config = make({
        "name": "gen",
        "runtime": {"agent": "generic", "approvals": "capwrap"},
    }, tmp_path)
    with pytest.raises(ConfigError, match="no approval shim"):
        fsprep.prepare(config, ContainerPaths("gen"))


def test_default_agent_is_still_claude(tmp_path, state_dir):
    """Configs written before profiles existed keep producing claude settings."""
    config = make({
        "name": "legacy",
        "runtime": {"approvals": "capwrap", "auto_allow": ["Read"]},
    }, tmp_path)
    files = files_by_dest(fsprep.prepare(config, ContainerPaths("legacy")))
    assert f"{GUEST_HOME}/.claude/settings.json" in files
    assert GUEST_POLICY in files


def test_role_prompt_is_staged_and_bound_for_claude(tmp_path, state_dir):
    _role(tmp_path, "# you are the architect\n")
    config = make({
        "name": "rp",
        "runtime": {"agent": "claude", "role_prompt": "role.md"},
    }, tmp_path)
    files = files_by_dest(fsprep.prepare(config, ContainerPaths("rp")))

    staged = files[GUEST_ROLE_PROMPT]
    assert staged.is_file()
    assert staged.read_text() == "# you are the architect\n"


def test_role_prompt_is_staged_and_bound_for_generic(tmp_path, state_dir):
    _role(tmp_path, "# you are a generic agent\n")
    config = make({
        "name": "rp",
        "runtime": {"agent": "generic", "role_prompt": "role.md"},
    }, tmp_path)
    files = files_by_dest(fsprep.prepare(config, ContainerPaths("rp")))

    staged = files[GUEST_ROLE_PROMPT]
    assert staged.is_file()
    assert staged.read_text() == "# you are a generic agent\n"


def test_bwrap_inserts_the_claude_role_prompt_flag_after_the_binary(
    tmp_path, state_dir
):
    _role(tmp_path)
    config = make({
        "name": "rp",
        "runtime": {
            "agent": "claude",
            "role_prompt": "role.md",
            "command": ["claude", "-p", "task"],
        },
    }, tmp_path)
    paths = ContainerPaths("rp")
    argv = bwrap.build_argv(config, fsprep.prepare(config, paths), paths)

    command = argv[argv.index("--") + 1:]
    # Flags are appended at the END, not after argv[0]: pi's command is
    # ["node", ".../cli.js"] and `node --model` would die with exit 9.
    assert command == [
        "claude", "-p", "task", "--append-system-prompt-file", GUEST_ROLE_PROMPT,
    ]


# --------------------------------------------------------------------------
# role_prompt config resolution
# --------------------------------------------------------------------------


def test_relative_role_prompt_resolves_against_the_config_dir(tmp_path):
    role = _role(tmp_path)
    config = make({"name": "a", "runtime": {"role_prompt": "role.md"}}, tmp_path)
    assert config.runtime.role_prompt == role.resolve()


def test_validate_sources_rejects_a_missing_role_prompt(tmp_path):
    config = make({"name": "a", "runtime": {"role_prompt": "nope.md"}}, tmp_path)
    with pytest.raises(ConfigError, match="role_prompt"):
        config.validate_sources()