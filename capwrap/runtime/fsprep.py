"""Host-side preparation of a container's filesystem.

`prepare` does everything that must happen *before* bwrap runs -- creating
overlay upper directories, copying trees, cutting git worktrees, staging
injected files -- and returns a list of `ResolvedMount`s, which are pure
declarative instructions for `bwrap.build_argv`.

Keeping the two apart means the argv builder never touches the filesystem, so it
can be unit-tested exhaustively without a sandbox, and the messy part has one
home.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from ..config import ContainerConfig, FileSpec, MountSpec
from ..errors import SandboxError
from ..paths import (
    GUEST_GITDIR_ROOT,
    GUEST_HOME,
    GUEST_POLICY,
    GUEST_TOOLS,
    ContainerPaths,
    slugify,
)
from . import gitwt

#: What bwrap should ultimately do for a mount, after host-side prep.
BindKind = Literal["ro", "rw", "tmpfs", "overlay"]


@dataclass
class ResolvedMount:
    """A mount reduced to something bwrap can execute directly."""

    dest: str
    kind: BindKind
    src: Path | None = None
    # overlay only
    lower: Path | None = None
    upper: Path | None = None
    work: Path | None = None
    # tmpfs only
    size: str | None = None
    #: Purely for `capwrap ps`/the web UI, to explain what an agent is looking at.
    origin: str = ""


@dataclass
class PreparedFs:
    """Everything the launcher needs after preparation."""

    mounts: list[ResolvedMount] = field(default_factory=list)
    #: (host staged file, guest destination) pairs, bound read-only.
    files: list[tuple[Path, str]] = field(default_factory=list)
    #: Undo actions, in reverse order, run when the container is destroyed.
    cleanups: list[Callable[[], None]] = field(default_factory=list)
    #: Set when any worktree mount exists; git needs safe.directory then.
    needs_git_config: bool = False
    #: Guest destinations of worktree mounts, used to keep injected files from
    #: showing up as untracked changes in the agent's repo.
    worktree_dests: list[str] = field(default_factory=list)

    def cleanup(self) -> None:
        for action in reversed(self.cleanups):
            try:
                action()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
        self.cleanups.clear()


def prepare(
    config: ContainerConfig,
    paths: ContainerPaths,
    overlay_backend: str = "kernel",
) -> PreparedFs:
    """Materialise everything `config` asks for. Idempotent across restarts."""
    paths.ensure()
    prepared = PreparedFs()

    for mount in config.mounts:
        handler = _HANDLERS[mount.mode]
        handler(mount, config, paths, prepared, overlay_backend)

    for spec in config.files:
        prepared.files.append(_stage_file(spec, paths))

    if prepared.needs_git_config:
        _write_gitconfig(paths, _worktree_relative(prepared))

    if config.runtime.approvals == "capwrap":
        prepared.files.extend(_install_approval_hook(config, paths))

    if config.runtime.capctl_skill:
        skill = Path(__file__).resolve().parent.parent / "guest" / "skill" / "SKILL.md"
        if skill.is_file():
            # Bound like the hook settings, so a config that mounts its own
            # $HOME/.claude for credentials does not shadow it.
            prepared.files.append(
                (skill, f"{GUEST_HOME}/.claude/skills/capwrap/SKILL.md")
            )

    return prepared


def _stage(path: Path, text: str, mode: int) -> Path:
    """Write a staged file, replacing any previous one.

    Staged files are often mode 0444 so an agent cannot rewrite its own policy;
    that also means a plain write fails on the second start of a container, so
    the old file is removed rather than overwritten.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    path.write_text(text)
    path.chmod(mode)
    return path


def _install_approval_hook(
    config: ContainerConfig, paths: ContainerPaths
) -> list[tuple[Path, str]]:
    """Register the PreToolUse hook that diverts prompts to the operator.

    Two files: Claude Code settings in the container's HOME wiring up the hook,
    and a policy file listing what may be auto-decided.  The policy is bound
    read-only so the agent cannot widen its own permissions by editing it --
    which it would otherwise be entirely capable of doing, since it has a shell.
    """
    settings = _stage(paths.files / "claude-settings.json", json.dumps({
        "hooks": {
            "PreToolUse": [{
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": f"{GUEST_TOOLS}/hook.py",
                    "timeout": 3600,
                }],
            }],
        },
    }, indent=2) + "\n", 0o444)

    policy = _stage(paths.files / "policy.json", json.dumps({
        "allow": config.runtime.auto_allow,
        "deny": config.runtime.auto_deny,
    }, indent=2) + "\n", 0o444)
    # Bound rather than written into HOME, because injected files are applied
    # after every mount: a config that mounts its own $HOME/.claude (to bring in
    # credentials) would otherwise shadow the hook registration and silently
    # disable approval routing.
    return [
        (settings, f"{GUEST_HOME}/.claude/settings.json"),
        (policy, GUEST_POLICY),
    ]


def _worktree_relative(prepared: PreparedFs) -> list[str]:
    """Injected file paths, relative to whichever worktree they land inside.

    An injected ROLE.md is scaffolding, not the agent's work, so it should not
    turn up in `git status`.
    """
    patterns: list[str] = []
    for _staged, dest in prepared.files:
        for wt in prepared.worktree_dests:
            if dest.startswith(wt + "/"):
                patterns.append("/" + dest[len(wt) + 1 :])
    return patterns


# --------------------------------------------------------------------------
# per-mode handlers
# --------------------------------------------------------------------------


def _prep_ro(mount, config, paths, prepared, backend) -> None:
    prepared.mounts.append(
        ResolvedMount(mount.dest, "ro", src=mount.src, origin=f"ro {mount.src}")
    )


def _prep_rw(mount, config, paths, prepared, backend) -> None:
    prepared.mounts.append(
        ResolvedMount(mount.dest, "rw", src=mount.src, origin=f"rw {mount.src}")
    )


def _prep_tmpfs(mount, config, paths, prepared, backend) -> None:
    prepared.mounts.append(
        ResolvedMount(mount.dest, "tmpfs", size=mount.size, origin="tmpfs")
    )


def _prep_copy(mount, config, paths, prepared, backend) -> None:
    """A private copy: the agent gets the contents but shares nothing."""
    target = paths.copy(mount.dest)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        assert mount.src is not None
        if mount.src.is_dir():
            shutil.copytree(mount.src, target, symlinks=True, dirs_exist_ok=False)
        else:
            shutil.copy2(mount.src, target)
    prepared.mounts.append(
        ResolvedMount(mount.dest, "rw", src=target, origin=f"copy of {mount.src}")
    )


def _prep_overlay(mount, config, paths, prepared, backend) -> None:
    """The headline mode: shared lower, private upper.

    Every container sees `src` exactly as it is on the host, and every write
    lands in a per-container upper directory.  Nothing an agent does is visible
    to any other agent or to the host tree, so concurrent agents need no
    coordination whatsoever.
    """
    assert mount.src is not None
    upper = paths.upper(mount.dest)
    work = paths.work(mount.dest)
    for d in (upper, work):
        d.mkdir(parents=True, exist_ok=True)

    if backend == "kernel":
        prepared.mounts.append(
            ResolvedMount(
                mount.dest, "overlay",
                lower=mount.src, upper=upper, work=work,
                origin=f"overlay on {mount.src}",
            )
        )
        return

    if backend == "fuse":
        merged = paths.merged(mount.dest)
        merged.mkdir(parents=True, exist_ok=True)
        _mount_fuse_overlay(mount.src, upper, work, merged)
        prepared.cleanups.append(lambda m=merged: _umount_fuse(m))
        prepared.mounts.append(
            ResolvedMount(
                mount.dest, "rw", src=merged,
                origin=f"overlay (fuse) on {mount.src}",
            )
        )
        return

    raise SandboxError(
        f"mount {mount.dest} needs an overlay, but no overlay backend is "
        "available on this host. Run `capwrap doctor`."
    )


def _prep_worktree(mount, config, paths, prepared, backend) -> None:
    """A private git branch + checkout for this container."""
    assert mount.src is not None
    slug = slugify(mount.dest)
    target = paths.worktree(mount.dest)

    result = gitwt.prepare_worktree(
        src=mount.src,
        target=target,
        guest_dest=mount.dest,
        guest_gitdir_root=GUEST_GITDIR_ROOT,
        slug=slug,
        staging=paths.files,
        share=mount.share,
        branch=mount.branch,
        base=mount.base,
        detach=mount.detach,
    )

    origin = f"git worktree {result.branch or 'detached'} of {mount.src}"
    prepared.mounts.append(
        ResolvedMount(mount.dest, "rw", src=result.worktree, origin=origin)
    )

    if result.main_gitdir is not None and result.guest_gitdir is not None:
        # Committing writes objects into the shared store, so this must be rw
        # under share="objects".  That is the documented cost of the mode.
        prepared.mounts.append(
            ResolvedMount(
                result.guest_gitdir, "rw", src=result.main_gitdir,
                origin=f"shared object store of {mount.src}",
            )
        )
    if result.dotgit_file is not None:
        prepared.files.append((result.dotgit_file, f"{mount.dest}/.git"))

    prepared.needs_git_config = True
    prepared.worktree_dests.append(mount.dest)

    if mount.on_destroy == "remove":
        src = mount.src
        prepared.cleanups.append(lambda: gitwt.remove_worktree(src, target))


_HANDLERS: dict[str, Callable] = {
    "ro": _prep_ro,
    "rw": _prep_rw,
    "tmpfs": _prep_tmpfs,
    "copy": _prep_copy,
    "overlay": _prep_overlay,
    "worktree": _prep_worktree,
}


# --------------------------------------------------------------------------
# fuse-overlayfs backend
# --------------------------------------------------------------------------


def _mount_fuse_overlay(lower: Path, upper: Path, work: Path, merged: Path) -> None:
    """Assemble the overlay on the host, so the sandbox only sees a plain bind.

    Used when the kernel refuses an unprivileged overlayfs mount.  It is also
    the shape that live re-mapping into a running container will want, since the
    daemon controls the mount from outside the sandbox.
    """
    if os.path.ismount(merged):
        return
    exe = shutil.which("fuse-overlayfs")
    if not exe:
        raise SandboxError(
            "overlay backend 'fuse' selected but fuse-overlayfs is not installed"
        )
    opts = f"lowerdir={lower},upperdir={upper},workdir={work}"
    proc = subprocess.run(
        [exe, "-o", opts, str(merged)], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise SandboxError(
            f"fuse-overlayfs on {merged} failed: {(proc.stderr or proc.stdout).strip()}"
        )


def _umount_fuse(merged: Path) -> None:
    if not os.path.ismount(merged):
        return
    for argv in (["fusermount3", "-u"], ["fusermount", "-u"], ["umount"]):
        exe = shutil.which(argv[0])
        if not exe:
            continue
        if subprocess.run([exe, *argv[1:], str(merged)], capture_output=True,
                          check=False).returncode == 0:
            return


# --------------------------------------------------------------------------
# injected files
# --------------------------------------------------------------------------


def _stage_file(spec: FileSpec, paths: ContainerPaths) -> tuple[Path, str]:
    """Copy or write an injected file into staging, ready to be bound in.

    Files are bound individually rather than written into a mount, so they
    survive being placed inside an overlay, a tmpfs, or a git worktree without
    dirtying it.
    """
    staged = paths.files / slugify(spec.dest)
    if spec.content is not None:
        _stage(staged, spec.content, spec.file_mode)
    else:
        assert spec.src is not None
        staged.parent.mkdir(parents=True, exist_ok=True)
        if staged.exists():
            staged.unlink()
        shutil.copyfile(spec.src, staged)
        staged.chmod(spec.file_mode)
    return staged, spec.dest


def _write_gitconfig(paths: ContainerPaths, excludes: list[str]) -> None:
    """Mark the bound repos as safe, in the container's own HOME.

    Git refuses to operate on a repository whose owner differs from the current
    uid.  Inside the sandbox the uid can be remapped, so ownership no longer
    matches and every git command fails with `detected dubious ownership`.  The
    sandbox boundary is what we are relying on here, not file ownership.

    Written into HOME (a plain rw bind we own) rather than bound over
    /etc/gitconfig, which may not exist on the host and lives under a read-only
    bind.  `bwrap.build_argv` also sets GIT_CONFIG_GLOBAL to this path so it is
    found regardless of how HOME ends up being interpreted.
    """
    lines = ["[safe]", "\tdirectory = *"]
    if excludes:
        exclude_file = paths.home / ".gitexclude"
        exclude_file.write_text(
            "# capwrap: files injected by [[files]], not the agent's work\n"
            + "\n".join(excludes)
            + "\n"
        )
        lines += ["[core]", f"\texcludesFile = {GUEST_HOME}/.gitexclude"]

    (paths.home / ".gitconfig").write_text("\n".join(lines) + "\n")
    (paths.home / ".gitconfig").chmod(0o644)


def describe(prepared: PreparedFs) -> list[str]:
    """Human-readable mount summary for `capwrap ps` and the web UI."""
    return [f"{m.dest} <- {m.origin or m.kind}" for m in prepared.mounts]
