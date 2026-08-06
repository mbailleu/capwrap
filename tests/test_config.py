"""Config parsing, validation and path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from capwrap.config import ContainerConfig, load_config, load_config_data
from capwrap.errors import ConfigError


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "agent.toml"
    path.write_text(text)
    return path


def test_minimal_config_gets_sane_defaults(tmp_path):
    config = load_config(write(tmp_path, 'name = "solo"\n'))
    assert config.name == "solo"
    assert config.runtime.command == ["/bin/bash"]
    assert config.sandbox.network is False, "sandboxes must default to no network"
    assert config.sandbox.base == "host-ro"
    assert config.mounts == []


def test_relative_paths_resolve_against_the_config_file(tmp_path):
    (tmp_path / "data").mkdir()
    config = load_config(write(tmp_path, """
        name = "rel"
        [[mounts]]
        src = "data"
        dest = "/data"
        mode = "ro"
    """))
    assert config.mounts[0].src == tmp_path / "data"


def test_worktree_branch_defaults_to_the_container_name(tmp_path):
    config = load_config(write(tmp_path, """
        name = "agent-7"
        [[mounts]]
        src = "."
        dest = "/work"
        mode = "worktree"
    """))
    assert config.mounts[0].branch == "capwrap/agent-7"


def test_unknown_key_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="typo"):
        load_config(write(tmp_path, 'name = "x"\ntypo = 1\n'))


def test_relative_mount_dest_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="absolute"):
        load_config(write(tmp_path, """
            name = "x"
            [[mounts]]
            src = "."
            dest = "work"
        """))


def test_two_mounts_cannot_share_a_destination(tmp_path):
    with pytest.raises(ConfigError, match="both target"):
        load_config(write(tmp_path, """
            name = "x"
            [[mounts]]
            src = "."
            dest = "/work"
            [[mounts]]
            src = "."
            dest = "/work"
            mode = "rw"
        """))


def test_tmpfs_must_not_have_a_source(tmp_path):
    with pytest.raises(ConfigError, match="must not set 'src'"):
        load_config(write(tmp_path, """
            name = "x"
            [[mounts]]
            src = "."
            dest = "/scratch"
            mode = "tmpfs"
        """))


def test_non_tmpfs_mount_requires_a_source(tmp_path):
    with pytest.raises(ConfigError, match="requires 'src'"):
        load_config(write(tmp_path, """
            name = "x"
            [[mounts]]
            dest = "/data"
            mode = "ro"
        """))


def test_worktree_only_options_are_rejected_elsewhere(tmp_path):
    with pytest.raises(ConfigError, match="only valid with mode='worktree'"):
        load_config(write(tmp_path, """
            name = "x"
            [[mounts]]
            src = "."
            dest = "/data"
            mode = "ro"
            branch = "feature"
        """))


def test_file_needs_exactly_one_source(tmp_path):
    with pytest.raises(ConfigError, match="exactly one of"):
        load_config(write(tmp_path, """
            name = "x"
            [[files]]
            dest = "/a"
            src = "a"
            content = "b"
        """))


def test_bad_right_names_point_at_the_typo():
    from capwrap.kernel.rights import parse_rights

    with pytest.raises(ValueError, match="unknown right 'sned'"):
        parse_rights(["send", "sned"])


def test_container_name_must_be_path_safe(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, 'name = "../escape"\n'))


def test_validate_sources_reports_a_missing_path(tmp_path):
    config = load_config(write(tmp_path, """
        name = "x"
        [[mounts]]
        src = "nope"
        dest = "/data"
        mode = "ro"
    """))
    with pytest.raises(ConfigError, match="does not exist"):
        config.validate_sources()


def test_validate_sources_requires_a_repo_for_worktree_mode(tmp_path):
    (tmp_path / "plain").mkdir()
    config = load_config(write(tmp_path, """
        name = "x"
        [[mounts]]
        src = "plain"
        dest = "/work"
        mode = "worktree"
    """))
    with pytest.raises(ConfigError, match="needs a git repo"):
        config.validate_sources()


def test_inline_config_validates_without_touching_disk():
    """The daemon validates agent-submitted configs this way."""
    config = load_config_data({"name": "child"}, base_dir=Path("/nonexistent"))
    assert isinstance(config, ContainerConfig)
    assert config.name == "child"
