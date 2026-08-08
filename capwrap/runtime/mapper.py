"""How a delegated dataspace actually reaches the container that was given it.

`CapKernel.ds_map` decides *whether* one container may hand a dataspace to
another. What happens as a result is a `Mapper`, and there are two, with
genuinely different semantics:

`SharedDirMapper`
    Copies or symlinks into the target's `/shared`, which is a plain host
    directory already bound into the sandbox. Needs no privilege and works
    everywhere, but it is not a mount: `mode="map"` degrades to a symlink, which
    only resolves if the target is itself inside the bind.

`NsMountMapper`
    A real bind mount into the running container's mount namespace. True
    aliasing -- both containers look at the same filesystem objects, writes on
    one side appear on the other, and a directory can be mapped as a directory.
    Requires `CAP_SYS_ADMIN` on the host, so the daemon has to be privileged.

`select` picks the strongest one the daemon can actually use, by trying it.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol

from ..errors import CapwrapError
from . import nsmount


class MapTarget(Protocol):
    """The bit of a container a mapper needs: where its /shared lives, and its pid."""

    @property
    def name(self) -> str: ...

    @property
    def shared_dir(self) -> Path: ...

    @property
    def pid(self) -> int | None: ...


def _safe_name(dest_name: str) -> str:
    """A single path component, so a mapping cannot escape /shared."""
    name = dest_name.strip("/").replace("..", "_").replace("/", "_")
    if not name:
        raise CapwrapError("a destination name is required")
    return name


class SharedDirMapper:
    """Materialise into the target's /shared directory. No privilege required."""

    name = "shared"
    supports_live_mount = False

    def materialise(self, target: MapTarget, source: Path, dest_name: str,
                    mode: str) -> str:
        safe = _safe_name(dest_name)
        destination = target.shared_dir / safe
        _clear(destination)

        if mode == "copy":
            if source.is_dir():
                shutil.copytree(source, destination, symlinks=True)
            else:
                shutil.copy2(source, destination)
        else:
            # A symlink aliases the two containers to the same bytes, which is
            # what distinguishes MAP from COPY -- but only resolves inside the
            # sandbox if the source is itself reachable there.
            destination.symlink_to(source)
        return f"{self.name}:{target.name}:{safe}"

    def unmaterialise(self, target: MapTarget, token: str) -> None:
        _, _, safe = token.rpartition(":")
        _clear(target.shared_dir / safe)


class NsMountMapper:
    """Bind-mount into the live container. Real aliasing; needs CAP_SYS_ADMIN."""

    name = "nsmount"
    supports_live_mount = True

    def __init__(self) -> None:
        self._fallback = SharedDirMapper()

    def materialise(self, target: MapTarget, source: Path, dest_name: str,
                    mode: str) -> str:
        # A copy has no aliasing to preserve, so there is nothing a mount buys
        # it -- and a copy keeps working after the container restarts, which a
        # mount does not.
        if mode == "copy":
            return self._fallback.materialise(target, source, dest_name, mode)

        if target.pid is None:
            raise CapwrapError(
                f"{target.name} is not running; a live mapping needs a running "
                "container. Use mode='copy', or start it first."
            )

        safe = _safe_name(dest_name)
        guest_path = f"/shared/{safe}"
        nsmount.mount_into(
            target.pid, source, guest_path, readonly=(mode == "ro"),
        )
        return f"{self.name}:{target.name}:{safe}"

    def unmaterialise(self, target: MapTarget, token: str) -> None:
        backend, _, rest = token.partition(":")
        _, _, safe = rest.rpartition(":")
        if backend != self.name:
            self._fallback.unmaterialise(target, token)
            return
        if target.pid is not None:
            nsmount.unmount_from(target.pid, f"/shared/{safe}")
        _clear(target.shared_dir / safe)


def _clear(path: Path) -> None:
    """Remove whatever is at `path`, mount or not."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def select(requested: str = "auto") -> tuple[object, str]:
    """Choose a mapper. Returns it and a one-line reason.

    "auto" prefers live mounting and falls back, so an unprivileged daemon keeps
    working rather than failing at the first `capctl map`.
    """
    if requested == "shared":
        return SharedDirMapper(), "configured"
    if requested == "nsmount":
        ok, detail = nsmount.available()
        if not ok:
            raise CapwrapError(f"mapping_backend='nsmount' is unusable: {detail}")
        return NsMountMapper(), detail
    if requested != "auto":
        raise CapwrapError(f"unknown mapping backend {requested!r}")

    ok, detail = nsmount.available()
    return (NsMountMapper(), detail) if ok else (SharedDirMapper(), detail)
