"""Where capwrap keeps its runtime state on the host.

Layout under the state root (``$CAPWRAP_STATE`` or ``~/.local/state/capwrap``)::

    capwrap.db                  audit log + persisted capability tables
    daemon.sock                 host control socket (the CLI talks to this)
    containers/<name>/
        agent.sock              bound to /run/capwrap.sock inside the sandbox
        shared/                 bound to /shared; delegated files land here
        upper/<slug>/           overlay upper dirs, one per overlay mount
        work/<slug>/            overlay work dirs (must share a fs with upper)
        merged/<slug>/          fuse-overlayfs mountpoints (fuse backend only)
        copies/<slug>/          private copies for mode="copy"
        worktrees/<slug>/       git worktrees for mode="worktree"
        files/                  staged files injected by [[files]]
        home/                   the sandbox's HOME

One directory per container keeps teardown a single rmtree, and keeps every
container's private state trivially unreachable from any other sandbox: nothing
under ``containers/<other>`` is ever mounted into ``<name>``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: Path of the agent-facing control socket *inside* a sandbox.
GUEST_SOCKET = "/run/capwrap.sock"
#: Where the guest-side tools (capctl, hook.py) are mounted inside a sandbox.
GUEST_TOOLS = "/opt/capwrap"
#: Where delegated dataspaces appear inside a sandbox.
GUEST_SHARED = "/shared"
#: Auto-approval policy inside a sandbox. On the /run tmpfs rather than under
#: GUEST_TOOLS, because bwrap cannot bind a new file into a read-only mount.
GUEST_POLICY = "/run/capwrap-policy.json"
#: HOME inside a sandbox.
GUEST_HOME = "/home/agent"
#: Where the main .git dir of a worktree's origin repo is mounted.
GUEST_GITDIR_ROOT = "/gitdir"

_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def force_rmtree(path: "Path") -> None:
    """Delete a container's state, including directories we cannot enter.

    Plain `shutil.rmtree` is not enough here: the kernel creates an overlayfs
    work directory as ``work/work`` with mode 000, so removal fails with
    EACCES and a container's state can never be cleaned up.  We own the
    directory, so restoring a traversable mode and retrying is safe.
    """
    import shutil
    import stat

    if not path.exists():
        return

    # Walk top-down and make each directory traversable *before* descending into
    # it, so the overlayfs work dir stops being a wall.
    for root, dirs, _files in os.walk(path, topdown=True):
        for name in dirs:
            try:
                os.chmod(os.path.join(root, name), stat.S_IRWXU)
            except OSError:
                pass

    shutil.rmtree(path, ignore_errors=True)


def slugify(value: str) -> str:
    """Turn a mount destination into a filesystem-safe directory name.

    ``/work/src`` becomes ``work-src``.  Used to give each mount its own upper,
    work and copy directory without nesting them by their in-sandbox path.
    """
    slug = _SLUG_RE.sub("-", value.strip("/")) or "root"
    return slug.strip("-") or "root"


def state_root() -> Path:
    """Root of all host-side runtime state."""
    env = os.environ.get("CAPWRAP_STATE")
    if env:
        return Path(env).expanduser().resolve()
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base).expanduser().resolve() / "capwrap"


def db_path() -> Path:
    return state_root() / "capwrap.db"


def daemon_socket() -> Path:
    return state_root() / "daemon.sock"


def container_root(name: str) -> Path:
    return state_root() / "containers" / name


class ContainerPaths:
    """Resolved host-side paths for one container."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.root = container_root(name)

    @property
    def socket(self) -> Path:
        return self.root / "agent.sock"

    @property
    def shared(self) -> Path:
        return self.root / "shared"

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def files(self) -> Path:
        return self.root / "files"

    def upper(self, dest: str) -> Path:
        return self.root / "upper" / slugify(dest)

    def work(self, dest: str) -> Path:
        return self.root / "work" / slugify(dest)

    def merged(self, dest: str) -> Path:
        return self.root / "merged" / slugify(dest)

    def copy(self, dest: str) -> Path:
        return self.root / "copies" / slugify(dest)

    def worktree(self, dest: str) -> Path:
        return self.root / "worktrees" / slugify(dest)

    def ensure(self) -> None:
        """Create the directories that always exist, regardless of config."""
        for path in (self.root, self.shared, self.home, self.files):
            path.mkdir(parents=True, exist_ok=True)
        # The socket is bind-mounted into the sandbox, and bwrap can only bind a
        # path that already exists.  The daemon replaces this with a real socket
        # when it binds; until then it is an empty placeholder file.
        self.root.chmod(0o700)
