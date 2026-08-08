"""Dataspace mapping backends.

`ds_map` decides *whether* a container may hand a dataspace to another; a
`Mapper` carries it out. The two backends differ in what "map" actually means,
so these tests pin down both the shared semantics and the differences.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from capwrap.errors import CapwrapError
from capwrap.runtime import nsmount
from capwrap.runtime.mapper import NsMountMapper, SharedDirMapper, select


@dataclass
class FakeTarget:
    """The slice of a container a mapper touches."""

    name: str
    shared_dir: Path
    pid: int | None = None


@pytest.fixture
def target(tmp_path) -> FakeTarget:
    shared = tmp_path / "shared"
    shared.mkdir()
    return FakeTarget(name="receiver", shared_dir=shared)


@pytest.fixture
def source(tmp_path) -> Path:
    src = tmp_path / "findings"
    src.mkdir()
    (src / "notes.md").write_text("the bug is in the parser\n")
    return src


# ==========================================================================
# shared-directory backend
# ==========================================================================


def test_copy_duplicates_the_bytes(target, source):
    mapper = SharedDirMapper()
    mapper.materialise(target, source, "findings", "copy")

    landed = target.shared_dir / "findings" / "notes.md"
    assert landed.read_text() == "the bug is in the parser\n"

    # A copy is independent: changing the original does not change the copy.
    (source / "notes.md").write_text("changed\n")
    assert landed.read_text() == "the bug is in the parser\n"


def test_map_aliases_rather_than_copying(target, source):
    mapper = SharedDirMapper()
    mapper.materialise(target, source, "findings", "map")

    landed = target.shared_dir / "findings"
    assert landed.is_symlink(), "map must alias, not duplicate"
    assert (landed / "notes.md").read_text() == "the bug is in the parser\n"


def test_unmaterialise_removes_what_was_placed(target, source):
    mapper = SharedDirMapper()
    token = mapper.materialise(target, source, "findings", "copy")
    assert (target.shared_dir / "findings").exists()

    mapper.unmaterialise(target, token)
    assert not (target.shared_dir / "findings").exists()
    assert source.exists(), "revoking a mapping must not touch the source"


def test_remapping_the_same_name_replaces_it(target, source):
    mapper = SharedDirMapper()
    mapper.materialise(target, source, "findings", "copy")
    (source / "notes.md").write_text("second version\n")
    mapper.materialise(target, source, "findings", "copy")

    landed = target.shared_dir / "findings" / "notes.md"
    assert landed.read_text() == "second version\n"


@pytest.mark.parametrize("name", ["../escape", "/etc/passwd", "a/b", "..", ""])
def test_a_mapping_cannot_escape_the_shared_directory(target, source, name):
    """The name comes from an agent, so it is not trusted."""
    mapper = SharedDirMapper()
    if name == "":
        with pytest.raises(CapwrapError):
            mapper.materialise(target, source, name, "copy")
        return

    mapper.materialise(target, source, name, "copy")
    landed = list(target.shared_dir.iterdir())
    assert len(landed) == 1
    assert landed[0].parent == target.shared_dir, "escaped /shared"


# ==========================================================================
# live-mount backend
# ==========================================================================


def test_nsmount_uses_a_copy_for_copy_mode(target, source):
    """A copy has no aliasing to preserve, and survives a restart; a mount does not."""
    mapper = NsMountMapper()
    token = mapper.materialise(target, source, "findings", "copy")

    assert token.startswith("shared:")
    assert (target.shared_dir / "findings" / "notes.md").exists()


def test_nsmount_needs_a_running_container(target, source):
    mapper = NsMountMapper()
    target.pid = None
    with pytest.raises(CapwrapError, match="not running"):
        mapper.materialise(target, source, "findings", "map")


def test_unmaterialise_handles_a_token_from_the_other_backend(target, source):
    """Backends can change between a map and its revocation; tokens say which."""
    token = SharedDirMapper().materialise(target, source, "findings", "copy")
    NsMountMapper().unmaterialise(target, token)
    assert not (target.shared_dir / "findings").exists()


# ==========================================================================
# selection
# ==========================================================================


def test_explicit_shared_backend_is_honoured():
    mapper, _ = select("shared")
    assert isinstance(mapper, SharedDirMapper)


def test_auto_never_fails(monkeypatch):
    """An unprivileged daemon must keep working, not break at the first map."""
    monkeypatch.setattr(nsmount, "available", lambda *a, **k: (False, "no privilege"))
    mapper, detail = select("auto")
    assert isinstance(mapper, SharedDirMapper)
    assert detail == "no privilege"


def test_auto_prefers_live_mounting_when_it_works(monkeypatch):
    monkeypatch.setattr(nsmount, "available", lambda *a, **k: (True, "works"))
    mapper, _ = select("auto")
    assert isinstance(mapper, NsMountMapper)


def test_requesting_an_unusable_backend_is_an_error(monkeypatch):
    """Silently degrading a backend the operator asked for would hide the fact
    that mappings are copies rather than aliases."""
    monkeypatch.setattr(nsmount, "available", lambda *a, **k: (False, "EPERM"))
    with pytest.raises(CapwrapError, match="unusable"):
        select("nsmount")


def test_unknown_backend_is_rejected():
    with pytest.raises(CapwrapError, match="unknown mapping backend"):
        select("magic")


# ==========================================================================
# the real thing, when the daemon is privileged enough
# ==========================================================================


@pytest.mark.sandbox
def test_live_mount_into_a_running_container(tmp_path, state_dir, require_sandbox):
    """Skipped unless this process can actually mount into a container.

    Needs CAP_SYS_ADMIN on the host; an ordinary user gets EPERM from
    `open_tree`, which is why `select("auto")` falls back.
    """
    ok, detail = nsmount.available()
    if not ok:
        pytest.skip(f"live remapping unavailable: {detail}")

    import subprocess
    import time

    from capwrap.runtime import probe

    source = tmp_path / "payload"
    source.mkdir()
    (source / "marker.txt").write_text("mounted live\n")

    sandbox = subprocess.Popen(
        [probe.find_bwrap(), "--unshare-user", "--unshare-pid", "--unshare-ipc",
         "--unshare-uts", "--ro-bind", "/usr", "/usr",
         "--symlink", "usr/lib", "/lib", "--symlink", "usr/bin", "/bin",
         "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
         "--tmpfs", "/shared", "/bin/sleep", "30"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.8)
        nsmount.mount_into(sandbox.pid, source, "/shared/payload")

        inner = nsmount.container_pid(sandbox.pid)
        seen = subprocess.run(
            ["nsenter", "-t", str(inner), "-U", "-m", "--preserve-credentials",
             "cat", "/shared/payload/marker.txt"],
            capture_output=True, text=True,
        )
        assert seen.stdout == "mounted live\n", seen.stderr

        # A real mount aliases: a write on the host is visible inside at once.
        (source / "marker.txt").write_text("changed on the host\n")
        again = subprocess.run(
            ["nsenter", "-t", str(inner), "-U", "-m", "--preserve-credentials",
             "cat", "/shared/payload/marker.txt"],
            capture_output=True, text=True,
        )
        assert again.stdout == "changed on the host\n", "not aliased"

        nsmount.unmount_from(sandbox.pid, "/shared/payload")
        gone = subprocess.run(
            ["nsenter", "-t", str(inner), "-U", "-m", "--preserve-credentials",
             "ls", "/shared/payload"],
            capture_output=True, text=True,
        )
        assert "marker.txt" not in gone.stdout
    finally:
        sandbox.terminate()
