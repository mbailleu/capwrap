"""Per-container git checkouts for ``mode = "worktree"``.

Overlaying a git repo works, but it leaves you reconciling agents by diffing
overlay upper directories.  Giving each agent its own branch in its own worktree
means integration is an ordinary ``git merge``.

Two sharing policies:

``share = "objects"``
    A real ``git worktree`` off the source repo.  One object store, branches
    visible from the host repo immediately.  The main ``.git`` is mounted into
    the sandbox, so an agent can in principle touch another agent's refs -- this
    is the fast, cooperative mode.

``share = "none"``
    ``git clone --local``: a self-contained repo with hardlinked objects.  No
    shared writable state at all.  Integrate with ``git fetch <path> <branch>``
    from the source repo.

The absolute-path problem
-------------------------
A linked worktree is glued to its repo by two files holding *host* absolute
paths:

* ``<worktree>/.git``                        -> ``gitdir: <main>/.git/worktrees/<id>``
* ``<main>/.git/worktrees/<id>/gitdir``      -> ``<worktree>/.git``

Inside the sandbox the main repo lives at ``/gitdir/<slug>``, not at its host
path, so the first file does not resolve and git reports the famously unhelpful
"not a git repository".

We fix only the first file, and we fix it *without touching the host copy*: a
rewritten version is staged in the container's state directory and bind-mounted
over ``<dest>/.git``.  The host's copy stays correct, so ``git -C <worktree>``
still works from the host after the container is gone.

The second file is deliberately left alone.  It is read by ``git worktree
list`` and ``git worktree prune``, both of which run on the *host*, where the
recorded path is the correct one.  Rewriting it would make the host believe the
worktree had vanished and prune it out from under a running agent.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..errors import SandboxError


@dataclass
class WorktreeResult:
    """What a prepared worktree needs from the sandbox."""

    #: Host path of the checkout, to be bound at the mount's `dest`.
    worktree: Path
    #: Host path of the repo's main .git directory, or None for a full clone.
    main_gitdir: Path | None
    #: Staged replacement for `<dest>/.git`, or None when no rewrite is needed.
    dotgit_file: Path | None
    #: Where `main_gitdir` must appear inside the sandbox.
    guest_gitdir: str | None
    branch: str | None
    #: Set when a stale checkout was renamed out of the way, so the caller can
    #: say so rather than leaving it to be discovered later.
    quarantined: Path | None = None


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run git, raising `SandboxError` with git's own message on failure."""
    exe = shutil.which("git")
    if not exe:
        raise SandboxError("git is not installed, but a mount uses mode='worktree'")
    proc = subprocess.run(
        [exe, *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise SandboxError(f"git {' '.join(args)}: {detail}")
    return proc.stdout.strip()


def repo_root(src: Path) -> Path:
    """Absolute path of the working tree root containing `src`."""
    return Path(_git("rev-parse", "--show-toplevel", cwd=src))


def main_git_dir(src: Path) -> Path:
    """Absolute path of the repo's shared .git directory.

    ``--git-common-dir`` rather than ``--git-dir`` so that pointing capwrap at a
    worktree of another repo still finds the real store.
    """
    out = _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=src)
    return Path(out)


def branch_exists(src: Path, branch: str) -> bool:
    exe = shutil.which("git") or "git"
    proc = subprocess.run(
        [exe, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=str(src), capture_output=True, check=False,
    )
    return proc.returncode == 0


def prepare_worktree(
    src: Path,
    target: Path,
    guest_dest: str,
    guest_gitdir_root: str,
    slug: str,
    staging: Path,
    *,
    share: str = "objects",
    branch: str | None = None,
    base: str = "HEAD",
    detach: bool = False,
) -> WorktreeResult:
    """Create (or reuse) a per-container checkout of `src` at `target`.

    `target` is the host path; `guest_dest` is where it will appear inside the
    sandbox.  `staging` is a directory for the rewritten ``.git`` file.
    """
    if share == "none":
        return _prepare_clone(src, target, branch=branch, base=base)
    return _prepare_linked_worktree(
        src, target, guest_dest, guest_gitdir_root, slug, staging,
        branch=branch, base=base, detach=detach,
    )


def _prepare_clone(
    src: Path, target: Path, *, branch: str | None, base: str
) -> WorktreeResult:
    """`git clone --local`: hardlinked objects, fully independent repo.

    Not ``--shared``: an alternates file would leave the clone depending on the
    source repo's objects surviving a gc.  Hardlinks cost the same and cannot
    rot that way.
    """
    if not (target / ".git").exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and any(target.iterdir()):
            raise SandboxError(f"clone target {target} exists and is not empty")
        shutil.rmtree(target, ignore_errors=True)
        args = ["clone", "--local"]
        if _crosses_fs(src, target):
            # Hardlinks cannot span filesystems; git would fail rather than
            # silently copy.
            args.append("--no-hardlinks")
        _git(*args, str(src), str(target))
        if branch:
            _git("checkout", "-B", branch, base, cwd=target)
    return WorktreeResult(
        worktree=target,
        main_gitdir=None,
        dotgit_file=None,
        guest_gitdir=None,
        branch=branch,
    )


def _crosses_fs(a: Path, b: Path) -> bool:
    """True when `a` and `b` are on different filesystems (hardlinks impossible)."""
    probe = b if b.exists() else b.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return a.stat().st_dev != probe.stat().st_dev
    except OSError:
        return True


def _prepare_linked_worktree(
    src: Path,
    target: Path,
    guest_dest: str,
    guest_gitdir_root: str,
    slug: str,
    staging: Path,
    *,
    branch: str | None,
    base: str,
    detach: bool,
) -> WorktreeResult:
    root = repo_root(src)
    gitdir = main_git_dir(src)
    guest_gitdir = f"{guest_gitdir_root.rstrip('/')}/{slug}"

    quarantined = _quarantine_if_orphaned(target, gitdir)

    if not (target / ".git").exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        # Clear stale registrations before adding. `capwrap clean` (and anything
        # else that removes a container's state directory) deletes the checkout
        # without telling the repo, which then refuses to re-create it:
        #   fatal: '<path>' is a missing but already registered worktree
        # Pruning only drops entries whose directory is genuinely gone, so a
        # sibling container's live worktree is never touched.
        _git("worktree", "prune", cwd=root)

        args = ["worktree", "add"]
        if detach:
            args += ["--detach", str(target), base]
        elif branch and branch_exists(root, branch):
            # Reattaching to an agent's existing branch after its container was
            # destroyed with on_destroy="keep".
            args += [str(target), branch]
        elif branch:
            args += ["-b", branch, str(target), base]
        else:
            args += [str(target), base]
        _git(*args, cwd=root)

    dotgit = target / ".git"
    if not dotgit.is_file():
        raise SandboxError(
            f"expected {dotgit} to be a gitfile; got a directory. "
            "Is this really a linked worktree?"
        )

    # `gitdir: <main>/.git/worktrees/<id>` -> the same admin dir under its
    # in-sandbox path.  The admin dir's name is chosen by git and need not match
    # our slug, so read it back rather than assuming.
    admin_dir = Path(dotgit.read_text().split(":", 1)[1].strip())
    try:
        relative = admin_dir.relative_to(gitdir)
    except ValueError:
        raise SandboxError(
            f"worktree {target} points at {admin_dir}, which is not inside {gitdir}"
        ) from None

    staging.mkdir(parents=True, exist_ok=True)
    staged = staging / f"{slug}.gitfile"
    staged.write_text(f"gitdir: {guest_gitdir}/{relative.as_posix()}\n")

    return WorktreeResult(
        worktree=target,
        main_gitdir=gitdir,
        dotgit_file=staged,
        guest_gitdir=guest_gitdir,
        branch=branch,
        quarantined=quarantined,
    )


def _quarantine_if_orphaned(target: Path, gitdir: Path) -> Path | None:
    """Move a worktree aside if the repo no longer knows about it.

    A container's state outlives its source repo.  Re-clone the repo, or delete
    and recreate it, and the checkout under the container's state directory is
    still there with a ``.git`` file pointing at an admin directory that no
    longer exists.  `prepare` would happily reuse it, because `.git` exists --
    and the agent would then meet:

        fatal: not a git repository: /gitdir/work/worktrees/work

    with nothing anywhere explaining why.  So detect the orphan and rename it out
    of the way, leaving a fresh worktree to be created in its place.

    Renamed rather than deleted: it may hold work the agent had not committed,
    and that is not ours to throw away.  Returns the new path so the caller can
    tell someone.
    """
    dotgit = target / ".git"
    if not dotgit.is_file():
        return None  # absent, or a full clone with a real .git directory

    try:
        admin = Path(dotgit.read_text().split(":", 1)[1].strip())
    except (OSError, IndexError):
        admin = None

    if admin is not None and admin.is_dir():
        try:
            admin.relative_to(gitdir)
            return None  # still attached to this repo
        except ValueError:
            pass  # points into a different repo

    stamp = time.strftime("%Y%m%d-%H%M%S")
    aside = target.with_name(f"{target.name}.orphaned-{stamp}")
    target.rename(aside)
    return aside


def remove_worktree(src: Path, target: Path) -> None:
    """Detach a worktree from its repo and delete it. Best effort."""
    try:
        root = repo_root(src)
    except SandboxError:
        shutil.rmtree(target, ignore_errors=True)
        return
    try:
        _git("worktree", "remove", "--force", str(target), cwd=root)
    except SandboxError:
        shutil.rmtree(target, ignore_errors=True)
        try:
            _git("worktree", "prune", cwd=root)
        except SandboxError:
            pass


def current_branch(worktree: Path) -> str | None:
    """Branch checked out in `worktree`, or None when detached/unavailable."""
    try:
        name = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree)
    except SandboxError:
        return None
    return None if name == "HEAD" else name
