"""Filesystem preparation and the bwrap argv it produces.

These run without sandboxing: `fsprep` only manipulates host directories, and
`bwrap.build_argv` is a pure function.  The `sandbox`-marked tests at the bottom
check that the resulting command actually behaves as advertised.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from capwrap.config import load_config_data
from capwrap.errors import SandboxError
from capwrap.paths import ContainerPaths
from capwrap.runtime import bwrap as bwrap_mod
from capwrap.runtime import fsprep, gitwt


def make(raw: dict, base_dir):
    return load_config_data(raw, base_dir=base_dir)


def argv_for(config, paths, prepared, **kw) -> list[str]:
    return bwrap_mod.build_argv(config, prepared, paths, **kw)


def pairs(argv: list[str], flag: str) -> list[tuple[str, str]]:
    """All (src, dest) pairs for a two-argument bwrap flag."""
    return [
        (argv[i + 1], argv[i + 2])
        for i, tok in enumerate(argv)
        if tok == flag
    ]


# --------------------------------------------------------------------------
# mount modes
# --------------------------------------------------------------------------


def test_ro_and_rw_map_to_the_right_bwrap_flags(tmp_path, state_dir):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    config = make({
        "name": "m",
        "mounts": [
            {"src": "a", "dest": "/a", "mode": "ro"},
            {"src": "b", "dest": "/b", "mode": "rw"},
        ],
    }, tmp_path)
    paths = ContainerPaths("m")
    prepared = fsprep.prepare(config, paths)
    argv = argv_for(config, paths, prepared)

    assert (str(tmp_path / "a"), "/a") in pairs(argv, "--ro-bind")
    assert (str(tmp_path / "b"), "/b") in pairs(argv, "--bind")


def test_copy_mode_makes_a_private_copy(tmp_path, state_dir):
    src = tmp_path / "data"
    src.mkdir()
    (src / "seed.txt").write_text("original\n")

    config = make({
        "name": "c",
        "mounts": [{"src": "data", "dest": "/data", "mode": "copy"}],
    }, tmp_path)
    paths = ContainerPaths("c")
    prepared = fsprep.prepare(config, paths)

    copied = paths.copy("/data")
    assert (copied / "seed.txt").read_text() == "original\n"

    # The copy is what gets bound, and it is writable.
    assert (str(copied), "/data") in pairs(argv_for(config, paths, prepared), "--bind")

    # Mutating the copy must not touch the source.
    (copied / "seed.txt").write_text("changed\n")
    assert (src / "seed.txt").read_text() == "original\n"


def test_overlay_creates_upper_and_work_dirs(tmp_path, state_dir):
    src = tmp_path / "db"
    src.mkdir()
    config = make({
        "name": "o",
        "mounts": [{"src": "db", "dest": "/db", "mode": "overlay"}],
    }, tmp_path)
    paths = ContainerPaths("o")
    prepared = fsprep.prepare(config, paths, overlay_backend="kernel")

    assert paths.upper("/db").is_dir()
    assert paths.work("/db").is_dir()

    argv = argv_for(config, paths, prepared)
    i = argv.index("--overlay-src")
    assert argv[i + 1] == str(src)
    # --overlay-src applies to the *next* --overlay, so order matters.
    assert argv[i + 2] == "--overlay"
    assert argv[i + 3 : i + 6] == [
        str(paths.upper("/db")), str(paths.work("/db")), "/db",
    ]


def test_two_containers_get_separate_upper_dirs(tmp_path, state_dir):
    src = tmp_path / "db"
    src.mkdir()
    spec = {"mounts": [{"src": "db", "dest": "/db", "mode": "overlay"}]}

    uppers = set()
    for name in ("agent-a", "agent-b"):
        config = make({"name": name, **spec}, tmp_path)
        paths = ContainerPaths(name)
        fsprep.prepare(config, paths)
        uppers.add(paths.upper("/db"))

    assert len(uppers) == 2, "agents must not share an overlay upper dir"


def test_tmpfs_size_is_converted_to_bytes(tmp_path, state_dir):
    config = make({
        "name": "t",
        "mounts": [{"dest": "/scratch", "mode": "tmpfs", "size": "128m"}],
    }, tmp_path)
    paths = ContainerPaths("t")
    argv = argv_for(config, paths, fsprep.prepare(config, paths))
    i = argv.index("--size")
    assert argv[i + 1] == str(128 * 1024 * 1024)
    assert argv[i + 2 : i + 4] == ["--tmpfs", "/scratch"]


def test_overlay_without_a_backend_fails_loudly(tmp_path, state_dir):
    (tmp_path / "db").mkdir()
    config = make({
        "name": "o",
        "mounts": [{"src": "db", "dest": "/db", "mode": "overlay"}],
    }, tmp_path)
    with pytest.raises(SandboxError, match="no overlay backend"):
        fsprep.prepare(config, ContainerPaths("o"), overlay_backend="none")


# --------------------------------------------------------------------------
# nested mounts
# --------------------------------------------------------------------------


def _nested_config(tmp_path, order):
    """/a read-only, /a/b overlay, /a/b/c copy -- declared in `order`."""
    root = tmp_path / "nest"
    (root / "a" / "b" / "c").mkdir(parents=True)
    (root / "a" / "a.txt").write_text("a\n")
    (root / "a" / "b" / "b.txt").write_text("b\n")
    (root / "a" / "b" / "c" / "c.txt").write_text("c\n")

    specs = {
        "/a": {"src": str(root / "a"), "dest": "/a", "mode": "ro"},
        "/a/b": {"src": str(root / "a" / "b"), "dest": "/a/b", "mode": "overlay"},
        "/a/b/c": {"src": str(root / "a" / "b" / "c"), "dest": "/a/b/c", "mode": "copy"},
    }
    return make({"name": "nest", "mounts": [specs[d] for d in order]}, tmp_path)


@pytest.mark.parametrize(
    "order",
    [
        ["/a", "/a/b", "/a/b/c"],
        ["/a/b/c", "/a/b", "/a"],
        ["/a/b", "/a/b/c", "/a"],
    ],
)
def test_nested_mounts_are_ordered_parents_first(tmp_path, state_dir, order):
    """Config order must not change the result.

    bwrap applies mounts in argv order, so emitting `--ro-bind ... /a` after an
    overlay at `/a/b` would bury it -- the agent would get a read-only /a/b and
    nothing would report an error.
    """
    config = _nested_config(tmp_path, order)
    paths = ContainerPaths("nest")
    argv = argv_for(config, paths, fsprep.prepare(config, paths))

    positions = {}
    for i, token in enumerate(argv):
        if token in ("--ro-bind", "--bind", "--overlay"):
            # --overlay UPPER WORK DEST, vs --{ro-,}bind SRC DEST
            dest = argv[i + 3] if token == "--overlay" else argv[i + 2]
            positions.setdefault(dest, i)

    assert positions["/a"] < positions["/a/b"] < positions["/a/b/c"], (
        f"declared as {order}, emitted in the wrong order: {positions}"
    )


@pytest.mark.sandbox
def test_nested_mount_modes_each_take_effect(tmp_path, state_dir, run_in_sandbox):
    """Three different modes at three depths of one tree, all live."""
    config = _nested_config(tmp_path, ["/a/b/c", "/a/b", "/a"])  # worst-case order
    result = run_in_sandbox(config, """
        find /a -name '*.txt' | sort
        touch /a/nope 2>&1 | head -1
        echo overlay > /a/b/new.txt   && echo 'b writable'
        echo copied  > /a/b/c/new.txt && echo 'c writable'
    """)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "/a/a.txt" in out and "/a/b/b.txt" in out and "/a/b/c/c.txt" in out
    assert "Read-only file system" in out, "the /a bind should still be read-only"
    assert "b writable" in out and "c writable" in out

    # Nothing reached the host tree: the overlay upper and the private copy
    # absorbed both writes.
    source = tmp_path / "nest" / "a"
    assert not (source / "b" / "new.txt").exists()
    assert not (source / "b" / "c" / "new.txt").exists()

    paths = ContainerPaths("nest")
    assert (paths.upper("/a/b") / "new.txt").read_text() == "overlay\n"
    assert (paths.copy("/a/b/c") / "new.txt").read_text() == "copied\n"


# --------------------------------------------------------------------------
# injected files
# --------------------------------------------------------------------------


def test_inline_file_is_staged_and_bound_read_only(tmp_path, state_dir):
    config = make({
        "name": "f",
        "files": [{"dest": "/work/ROLE.md", "content": "you are a test\n"}],
    }, tmp_path)
    paths = ContainerPaths("f")
    prepared = fsprep.prepare(config, paths)

    staged, dest = prepared.files[0]
    assert staged.read_text() == "you are a test\n"
    assert dest == "/work/ROLE.md"
    assert (str(staged), dest) in pairs(argv_for(config, paths, prepared), "--ro-bind")


def test_file_mode_is_applied(tmp_path, state_dir):
    config = make({
        "name": "f",
        "files": [{"dest": "/run/secret", "content": "s", "mode": "0600"}],
    }, tmp_path)
    prepared = fsprep.prepare(config, ContainerPaths("f"))
    staged, _ = prepared.files[0]
    assert staged.stat().st_mode & 0o777 == 0o600


# --------------------------------------------------------------------------
# git worktree mode
# --------------------------------------------------------------------------


def test_worktree_creates_a_branch_and_checkout(tmp_path, state_dir, git_repo):
    config = make({
        "name": "wt",
        "mounts": [{
            "src": str(git_repo), "dest": "/work",
            "mode": "worktree", "base": "main",
        }],
    }, tmp_path)
    paths = ContainerPaths("wt")
    fsprep.prepare(config, paths)

    worktree = paths.worktree("/work")
    assert (worktree / "README.md").exists()
    assert gitwt.current_branch(worktree) == "capwrap/wt"

    branches = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=git_repo, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert "capwrap/wt" in branches
    assert "main" in branches


def test_worktree_dotgit_is_rewritten_to_the_in_sandbox_path(
    tmp_path, state_dir, git_repo
):
    """The gotcha this mode lives or dies on.

    The worktree's `.git` file holds a host absolute path to the repo's admin
    directory.  Inside the sandbox the repo lives at /gitdir/<slug>, so an
    unrewritten path makes every git command fail with "not a git repository".
    """
    config = make({
        "name": "wt",
        "mounts": [{
            "src": str(git_repo), "dest": "/work",
            "mode": "worktree", "base": "main",
        }],
    }, tmp_path)
    paths = ContainerPaths("wt")
    prepared = fsprep.prepare(config, paths)

    staged = dict((dest, src) for src, dest in prepared.files)
    assert "/work/.git" in staged, "the rewritten gitfile must be bound over .git"

    rewritten = staged["/work/.git"].read_text()
    assert rewritten.startswith("gitdir: /gitdir/work/worktrees/")
    assert str(git_repo) not in rewritten, "no host paths may leak into the sandbox"

    # The host's own copy is untouched, so `git -C <worktree>` still works from
    # the host after the container is gone.
    on_host = (paths.worktree("/work") / ".git").read_text()
    assert on_host.startswith(f"gitdir: {git_repo}")


def test_host_worktree_administration_is_left_alone(tmp_path, state_dir, git_repo):
    """`git worktree list` on the host must still resolve.

    If we rewrote the repo-side `gitdir` file too, the host would think the
    worktree had vanished and `git worktree prune` would delete it out from
    under a running agent.
    """
    config = make({
        "name": "wt",
        "mounts": [{"src": str(git_repo), "dest": "/work",
                    "mode": "worktree", "base": "main"}],
    }, tmp_path)
    paths = ContainerPaths("wt")
    fsprep.prepare(config, paths)

    subprocess.run(["git", "worktree", "prune"], cwd=git_repo, check=True,
                   capture_output=True)
    listing = subprocess.run(
        ["git", "worktree", "list"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert str(paths.worktree("/work")) in listing, "prune ate the live worktree"


def test_worktree_binds_the_shared_object_store(tmp_path, state_dir, git_repo):
    config = make({
        "name": "wt",
        "mounts": [{"src": str(git_repo), "dest": "/work",
                    "mode": "worktree", "share": "objects", "base": "main"}],
    }, tmp_path)
    paths = ContainerPaths("wt")
    prepared = fsprep.prepare(config, paths)
    argv = argv_for(config, paths, prepared)
    # rw, because committing writes objects into the shared store.
    assert (str(git_repo / ".git"), "/gitdir/work") in pairs(argv, "--bind")


def test_share_none_makes_a_standalone_clone(tmp_path, state_dir, git_repo):
    config = make({
        "name": "cl",
        "mounts": [{"src": str(git_repo), "dest": "/work",
                    "mode": "worktree", "share": "none", "base": "main"}],
    }, tmp_path)
    paths = ContainerPaths("cl")
    prepared = fsprep.prepare(config, paths)

    clone = paths.worktree("/work")
    assert (clone / ".git").is_dir(), "a clone has a real .git directory"
    # Nothing from the origin repo needs mounting.
    assert all(m.dest != "/gitdir/work" for m in prepared.mounts)
    assert not any(dest == "/work/.git" for _s, dest in prepared.files)


def test_injected_files_are_excluded_from_the_agents_repo(
    tmp_path, state_dir, git_repo
):
    config = make({
        "name": "wt",
        "mounts": [{"src": str(git_repo), "dest": "/work",
                    "mode": "worktree", "base": "main"}],
        "files": [{"dest": "/work/ROLE.md", "content": "scaffolding\n"}],
    }, tmp_path)
    paths = ContainerPaths("wt")
    fsprep.prepare(config, paths)

    assert "/ROLE.md" in (paths.home / ".gitexclude").read_text()
    assert "excludesFile" in (paths.home / ".gitconfig").read_text()


def test_worktree_survives_its_state_being_deleted(tmp_path, state_dir, git_repo):
    """`capwrap clean` removes the checkout without telling the repo.

    The repo keeps the worktree registered, and a later `git worktree add` for
    the same path fails with "missing but already registered worktree" -- so
    recreating a cleaned container would be impossible without pruning first.
    """
    from capwrap.paths import force_rmtree

    config = make({
        "name": "wt",
        "mounts": [{"src": str(git_repo), "dest": "/work",
                    "mode": "worktree", "base": "main"}],
    }, tmp_path)
    paths = ContainerPaths("wt")
    fsprep.prepare(config, paths)

    force_rmtree(paths.root)          # what `capwrap clean --yes` does
    fsprep.prepare(config, paths)     # must not raise

    assert (paths.worktree("/work") / "README.md").exists()
    assert gitwt.current_branch(paths.worktree("/work")) == "capwrap/wt"


def test_pruning_does_not_disturb_a_sibling_worktree(tmp_path, state_dir, git_repo):
    """The prune above must only clear entries whose directory is really gone."""
    from capwrap.paths import force_rmtree

    def config_for(name):
        return make({
            "name": name,
            "mounts": [{"src": str(git_repo), "dest": "/work",
                        "mode": "worktree", "base": "main"}],
        }, tmp_path)

    alive = ContainerPaths("alive")
    fsprep.prepare(config_for("alive"), alive)
    (alive.worktree("/work") / "in-progress.txt").write_text("work\n")

    doomed = ContainerPaths("doomed")
    fsprep.prepare(config_for("doomed"), doomed)
    force_rmtree(doomed.root)

    fsprep.prepare(config_for("doomed"), doomed)  # triggers the prune

    assert (alive.worktree("/work") / "in-progress.txt").exists(), \
        "pruning removed a live sibling worktree"
    listing = subprocess.run(
        ["git", "worktree", "list"], cwd=git_repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert str(alive.worktree("/work")) in listing


def test_worktree_is_reused_across_restarts(tmp_path, state_dir, git_repo):
    config = make({
        "name": "wt",
        "mounts": [{"src": str(git_repo), "dest": "/work",
                    "mode": "worktree", "base": "main"}],
    }, tmp_path)
    paths = ContainerPaths("wt")
    fsprep.prepare(config, paths)
    (paths.worktree("/work") / "in-progress.txt").write_text("agent work\n")

    fsprep.prepare(config, paths)  # restart
    assert (paths.worktree("/work") / "in-progress.txt").exists(), \
        "restarting a container must not discard the agent's work"


# --------------------------------------------------------------------------
# sandbox shape
# --------------------------------------------------------------------------


def test_network_is_unshared_by_default(tmp_path, state_dir):
    config = make({"name": "n"}, tmp_path)
    paths = ContainerPaths("n")
    argv = argv_for(config, paths, fsprep.prepare(config, paths))
    assert "--unshare-net" in argv


def test_enabling_network_binds_resolver_config(tmp_path, state_dir):
    """DNS must work, including when /etc/resolv.conf is a symlink into /run.

    systemd-resolved hosts symlink it to /run/systemd/resolve/stub-resolv.conf,
    and /run is a fresh tmpfs in the sandbox -- so the *resolved* file has to be
    bound at its own path, or the symlink dangles and name lookup fails.
    """
    config = make({"name": "n", "sandbox": {"network": True}}, tmp_path)
    paths = ContainerPaths("n")
    argv = argv_for(config, paths, fsprep.prepare(config, paths))
    assert "--unshare-net" not in argv

    bound = {dest for _s, dest in pairs(argv, "--ro-bind")}
    resolved = str(Path("/etc/resolv.conf").resolve())
    assert resolved in bound, f"resolver config not reachable; bound: {sorted(bound)}"


def test_hostname_requires_a_uts_namespace(tmp_path, state_dir):
    config = make({
        "name": "n",
        "sandbox": {"hostname": "boxy", "unshare": ["pid"]},
    }, tmp_path)
    paths = ContainerPaths("n")
    with pytest.raises(SandboxError, match="requires 'uts'"):
        argv_for(config, paths, fsprep.prepare(config, paths))


def test_capwrap_env_and_shared_dir_are_always_present(tmp_path, state_dir):
    config = make({"name": "n"}, tmp_path)
    paths = ContainerPaths("n")
    argv = argv_for(config, paths, fsprep.prepare(config, paths))
    assert (str(paths.shared), "/shared") in pairs(argv, "--bind")
    i = argv.index("CAPWRAP_SOCKET")
    assert argv[i + 1] == "/run/capwrap.sock"


def test_config_env_overrides_defaults(tmp_path, state_dir):
    config = make({"name": "n", "runtime": {"env": {"PATH": "/custom"}}}, tmp_path)
    paths = ContainerPaths("n")
    argv = argv_for(config, paths, fsprep.prepare(config, paths))
    setenvs = [
        (argv[i + 1], argv[i + 2])
        for i, t in enumerate(argv) if t == "--setenv"
    ]
    assert dict(setenvs)["PATH"] == "/custom"


# --------------------------------------------------------------------------
# the real thing
# --------------------------------------------------------------------------


@pytest.mark.sandbox
def test_overlay_isolates_two_agents_for_real(tmp_path, state_dir, run_in_sandbox):
    """The claim the whole project rests on, checked against a live sandbox."""
    shared = tmp_path / "db"
    shared.mkdir()
    (shared / "seed.txt").write_text("seed\n")

    def config_for(name):
        return make({
            "name": name,
            "mounts": [{"src": str(shared), "dest": "/db", "mode": "overlay"}],
        }, tmp_path)

    a = run_in_sandbox(config_for("iso-a"), "echo A > /db/a.txt && ls /db")
    assert a.returncode == 0, a.stderr
    b = run_in_sandbox(config_for("iso-b"), "echo B > /db/b.txt && ls /db")
    assert b.returncode == 0, b.stderr

    # Neither agent sees the other's file...
    assert "b.txt" not in a.stdout
    assert "a.txt" not in b.stdout
    # ...and the host tree is untouched by both.
    assert sorted(p.name for p in shared.iterdir()) == ["seed.txt"]


@pytest.mark.sandbox
def test_read_only_mount_really_is_read_only(tmp_path, state_dir, run_in_sandbox):
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "STYLE.md").write_text("rules\n")

    config = make({
        "name": "ro-test",
        "mounts": [{"src": str(ref), "dest": "/ref", "mode": "ro"}],
    }, tmp_path)
    result = run_in_sandbox(config, "touch /ref/new 2>&1; cat /ref/STYLE.md")
    assert "Read-only file system" in result.stdout
    assert "rules" in result.stdout
    assert not (ref / "new").exists()


@pytest.mark.sandbox
def test_sandbox_cannot_see_the_host_home(tmp_path, state_dir, run_in_sandbox):
    config = make({"name": "priv"}, tmp_path)
    result = run_in_sandbox(config, "ls /home 2>&1")
    assert "maurice" not in result.stdout
    assert "agent" in result.stdout


@pytest.mark.sandbox
def test_git_works_inside_a_worktree_sandbox(
    tmp_path, state_dir, git_repo, run_in_sandbox
):
    """End-to-end proof that the .git rewriting and object-store bind line up."""
    config = make({
        "name": "wt-live",
        "mounts": [{"src": str(git_repo), "dest": "/work",
                    "mode": "worktree", "base": "main"}],
        "runtime": {"cwd": "/work"},
    }, tmp_path)

    result = run_in_sandbox(config, """
        set -e
        git rev-parse --abbrev-ref HEAD
        echo 'agent change' >> README.md
        git -c user.email=a@x -c user.name=A commit -qam 'agent commit'
        git log --oneline -1 --format=%s
    """)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.split()
    assert "capwrap/wt-live" in result.stdout
    assert "agent commit" in result.stdout

    # The commit is visible from the host repo, on the agent's branch only.
    log = subprocess.run(
        ["git", "log", "--oneline", "--format=%s", "capwrap/wt-live"],
        cwd=git_repo, capture_output=True, text=True, check=True,
    ).stdout
    assert "agent commit" in log
    main_log = subprocess.run(
        ["git", "log", "--oneline", "--format=%s", "main"],
        cwd=git_repo, capture_output=True, text=True, check=True,
    ).stdout
    assert "agent commit" not in main_log
    assert lines  # keep the linter honest about the unused split
