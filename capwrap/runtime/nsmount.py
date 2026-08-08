"""Mounting into the namespaces of a *running* container.

`SharedDirMapper` (the default) materialises a dataspace by copying or linking
it into a host directory that is already bind-mounted into the sandbox. That
works everywhere and needs no privilege, but it is not a mount: the two
containers do not end up looking at the same filesystem object, and a directory
cannot be aliased at all.

This module does the real thing. The sequence is:

1. **On the host**, `open_tree(src, OPEN_TREE_CLONE | AT_RECURSIVE)` detaches a
   copy of the source mount tree and hands back a file descriptor for it. This
   has to happen first, because once we enter the container's mount namespace
   the host's filesystem is no longer reachable by path -- bwrap pivot_root'd
   away from it. A detached mount fd survives the namespace switch; a path does
   not.
2. **Fork**, because `setns` is one-way; the parent has to stay where it is.
3. `setns(pidfd, CLONE_NEWUSER | CLONE_NEWNS)` -- both namespaces in a single
   call. Joining them one at a time fails with EPERM: `mntns_install` checks
   `CAP_SYS_ADMIN` against the user namespace recorded in the *pending*
   credential set, so the user namespace has to be applied in the same
   operation rather than before it.
4. `move_mount(tree_fd, "", AT_FDCWD, dest, MOVE_MOUNT_F_EMPTY_PATH)` attaches
   the detached tree at its destination inside the container.

Privilege
---------
This needs `CAP_SYS_ADMIN` **on the host**, which an ordinary user does not
have. An unprivileged daemon can still join the container's namespaces and even
reads back a full capability set there -- but `mount(2)` and `open_tree(2)`
still return EPERM, because the check is made against the user namespace that
owns the *source* mount namespace, and that is the host's.

Measured on this host: as an ordinary user every mount attempt inside a joined
container returns EPERM; as root, the identical code succeeds. So this backend
is real, and `available()` reports whether the daemon can actually use it rather
than guessing from a version number.
"""

from __future__ import annotations

import ctypes
import errno
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..errors import SandboxError

_libc = ctypes.CDLL("libc.so.6", use_errno=True)

# Syscall numbers, asm-generic (aarch64, riscv64, and the newer ports).
_SYS = {
    "capget": 90,
    "setns": 268,
    "open_tree": 428,
    "move_mount": 429,
    "pidfd_open": 434,
}
#: x86-64 disagrees about almost all of them.
_SYS_X86_64 = {
    "capget": 125,
    "setns": 308,
    "open_tree": 428,
    "move_mount": 429,
    "pidfd_open": 434,
}
if os.uname().machine in ("x86_64", "amd64"):
    _SYS = _SYS_X86_64

CLONE_NEWNS = 0x00020000
CLONE_NEWUSER = 0x10000000

AT_FDCWD = -100
AT_RECURSIVE = 0x8000
OPEN_TREE_CLONE = 0x1
OPEN_TREE_CLOEXEC = 0o2000000
MOVE_MOUNT_F_EMPTY_PATH = 0x00000004

MNT_DETACH = 0x00000002


def _fail(what: str) -> OSError:
    code = ctypes.get_errno()
    return OSError(code, os.strerror(code), what)


def pidfd_open(pid: int) -> int:
    fd = _libc.syscall(_SYS["pidfd_open"], pid, 0)
    if fd < 0:
        raise _fail(f"pidfd_open({pid})")
    return fd


def setns(fd: int, mask: int) -> None:
    if _libc.syscall(_SYS["setns"], fd, mask) < 0:
        raise _fail(f"setns(mask={mask:#x})")


def open_tree(path: str, flags: int = OPEN_TREE_CLONE | AT_RECURSIVE) -> int:
    fd = _libc.syscall(_SYS["open_tree"], AT_FDCWD, str(path).encode(), flags)
    if fd < 0:
        raise _fail(f"open_tree({path})")
    return fd


def move_mount(tree_fd: int, dest: str) -> None:
    rc = _libc.syscall(
        _SYS["move_mount"], tree_fd, b"", AT_FDCWD, str(dest).encode(),
        MOVE_MOUNT_F_EMPTY_PATH,
    )
    if rc < 0:
        raise _fail(f"move_mount(-> {dest})")


def umount2(target: str, flags: int = MNT_DETACH) -> None:
    if _libc.umount2(str(target).encode(), flags) < 0:
        raise _fail(f"umount2({target})")


def container_pid(supervisor_pid: int) -> int | None:
    """The descendant of `supervisor_pid` that is actually inside the sandbox.

    bwrap forks: the process we spawned stays outside as a monitor, and only a
    child of it enters the new namespaces. Mounting into the monitor would do
    nothing at all, so find the one whose user namespace differs from ours.
    """
    mine = os.readlink("/proc/self/ns/user")
    pending, seen = [supervisor_pid], set()
    while pending:
        pid = pending.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        try:
            if os.readlink(f"/proc/{pid}/ns/user") != mine:
                return pid
            kids = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        except OSError:
            continue
        pending += [int(k) for k in kids]
    return None


def _in_namespaces(pid: int, work, timeout: float = 20.0):
    """Run `work()` inside `pid`'s user and mount namespaces, in a forked child.

    The child reports back through a pipe opened *before* the switch -- anything
    it opens by path afterwards lands in the container's filesystem, not ours,
    which is a memorable way to lose an error message.
    """
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - runs in a forked child
        os.close(read_fd)
        try:
            setns(pidfd_open(pid), CLONE_NEWUSER | CLONE_NEWNS)
            work()
            os.write(write_fd, b"ok")
            os._exit(0)
        except BaseException as exc:  # noqa: BLE001 - reported, then exit
            os.write(write_fd, f"{type(exc).__name__}: {exc}".encode()[:4000])
            os._exit(1)

    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as pipe:
        message = pipe.read().decode(errors="replace")
    deadline = time.monotonic() + timeout
    status = 1
    while time.monotonic() < deadline:
        done, status = os.waitpid(child, os.WNOHANG)
        if done:
            break
        time.sleep(0.02)
    else:  # pragma: no cover - the child is a few syscalls long
        os.kill(child, 9)
        os.waitpid(child, 0)
        raise SandboxError(f"mount helper for pid {pid} timed out")

    if os.waitstatus_to_exitcode(status) != 0:
        raise SandboxError(message or "mount helper failed")


def mount_into(pid: int, source: Path, dest: str, readonly: bool = False) -> None:
    """Bind `source` (a host path) at `dest` inside the container running as `pid`.

    `dest` is a path *inside* the container. Its parent must already exist and
    be writable there -- typically somewhere under /shared, which is a plain
    bind of a host directory.
    """
    source = Path(source)
    if not source.exists():
        raise SandboxError(f"cannot map {source}: no such path on the host")

    inner = container_pid(pid)
    if inner is None:
        raise SandboxError(f"no sandboxed process found under pid {pid}")

    # Detach a copy of the tree while the host filesystem is still reachable.
    try:
        tree = open_tree(str(source))
    except OSError as exc:
        if exc.errno == errno.EPERM:
            raise SandboxError(
                f"cannot detach {source} for mapping: CAP_SYS_ADMIN is required "
                "on the host. Run the daemon with that capability, or use the "
                "'shared' mapping backend."
            ) from None
        raise SandboxError(f"open_tree({source}): {exc}") from None

    def work() -> None:
        os.makedirs(dest, exist_ok=True)
        move_mount(tree, dest)
        if readonly:
            # Remounting read-only is a separate step for a bind; without it the
            # mount carries the source's write permission.
            _remount_readonly(dest)

    try:
        _in_namespaces(inner, work)
    finally:
        os.close(tree)


def _remount_readonly(dest: str) -> None:  # pragma: no cover - runs post-setns
    MS_BIND, MS_REMOUNT, MS_RDONLY = 4096, 32, 1
    rc = _libc.mount(None, str(dest).encode(), None, MS_BIND | MS_REMOUNT | MS_RDONLY, None)
    if rc < 0:
        raise _fail(f"remount ro({dest})")


def unmount_from(pid: int, dest: str) -> None:
    """Detach whatever `mount_into` put at `dest` inside the container."""
    inner = container_pid(pid)
    if inner is None:
        return  # the container is gone; its mounts went with it

    def work() -> None:
        try:
            umount2(dest)
        except OSError as exc:
            if exc.errno != errno.EINVAL:  # not mounted: nothing to undo
                raise

    _in_namespaces(inner, work)


# --------------------------------------------------------------------------
# availability
# --------------------------------------------------------------------------

_PROBE_SCRIPT = """
import os, sys
sys.path.insert(0, {root!r})
from capwrap.runtime import nsmount
nsmount.mount_into(int(sys.argv[1]), {src!r}, "/shared/probe")
"""


def available(bwrap: str | None = None) -> tuple[bool, str]:
    """Can this process actually mount into a live container?

    Answered by doing it, not by inspecting versions: the failure is a
    capability check deep in the kernel, and nothing observable from outside
    predicts it reliably.
    """
    from . import probe as probe_mod

    bwrap = bwrap or probe_mod.find_bwrap()
    if not bwrap:
        return False, "bwrap not found"

    with tempfile.TemporaryDirectory(prefix="capwrap-nsmount-") as tmp:
        source = Path(tmp) / "src"
        source.mkdir()
        (source / "marker").write_text("live\n")

        sandbox = subprocess.Popen(
            [bwrap, "--unshare-user", "--unshare-pid", "--unshare-ipc",
             "--unshare-uts", "--ro-bind", "/usr", "/usr",
             "--symlink", "usr/lib", "/lib", "--symlink", "usr/bin", "/bin",
             "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
             "--tmpfs", "/shared", "/bin/sleep", "10"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.6)
            mount_into(sandbox.pid, source, "/shared/probe")
        except SandboxError as exc:
            return False, str(exc).split("\n")[0]
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            sandbox.terminate()
            with __import__("contextlib").suppress(Exception):
                sandbox.wait(timeout=5)

    return True, "can mount into a running container"


if __name__ == "__main__":  # pragma: no cover - manual check
    ok, detail = available()
    print(f"live remapping: {'available' if ok else 'unavailable'} -- {detail}")
    sys.exit(0 if ok else 1)
