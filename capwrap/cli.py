"""``capwrap`` -- the host-side command line."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from .config import load_config
from .errors import CapwrapError
from .paths import ContainerPaths, force_rmtree, state_root
from .runtime import bwrap as bwrap_mod
from .runtime import fsprep, probe


def _guest_tools_dir() -> Path:
    return Path(__file__).resolve().parent / "guest"


def cmd_doctor(args: argparse.Namespace) -> int:
    report = probe.run_all()
    print("capwrap doctor\n")
    print(probe.format_report(report, color=sys.stdout.isatty()))
    return 0 if report.ok else 1


def _resolve_backend(requested: str) -> str:
    if requested != "auto":
        return requested
    backend = probe.run_all().overlay_backend
    if backend is None:
        raise CapwrapError(
            "no overlay backend available on this host; run `capwrap doctor`"
        )
    return backend


def _needs_overlay(config) -> bool:
    return any(m.mode == "overlay" for m in config.mounts)


def cmd_run(args: argparse.Namespace) -> int:
    """Prepare a container's filesystem and exec into it.

    The foreground, no-daemon path: useful for developing a config and for
    poking around inside a sandbox by hand.  `capwrap up` is the managed
    equivalent that the capability kernel and the web UI drive.
    """
    config = load_config(args.config)
    if args.name:
        config.name = args.name
    config.validate_sources()

    backend = _resolve_backend(args.overlay_backend) if _needs_overlay(config) else "kernel"

    bwrap_bin = probe.find_bwrap()
    if not bwrap_bin:
        raise CapwrapError("bwrap not found; run `capwrap doctor`")

    paths = ContainerPaths(config.name)
    prepared = fsprep.prepare(config, paths, overlay_backend=backend)

    if args.command:
        config.runtime.command = list(args.command)

    argv = bwrap_mod.build_argv(
        config, prepared, paths,
        bwrap=bwrap_bin,
        guest_tools=_guest_tools_dir(),
    )

    if args.dry_run:
        print(f"# container: {config.name}")
        print(f"# state:     {paths.root}")
        print(f"# overlay:   {backend}")
        for line in fsprep.describe(prepared):
            print(f"# mount:     {line}")
        print()
        print(bwrap_mod.render(argv))
        prepared.cleanup()
        return 0

    if not args.quiet:
        print(f"capwrap: {config.name} -> {' '.join(config.runtime.command)}", file=sys.stderr)

    try:
        os.execv(argv[0], argv)
    except OSError as exc:
        prepared.cleanup()
        raise CapwrapError(f"failed to exec {argv[0]}: {exc}") from None
    return 0  # pragma: no cover - execv does not return


def cmd_show(args: argparse.Namespace) -> int:
    """Validate a config and print what it resolves to."""
    config = load_config(args.config)
    if not args.no_check:
        config.validate_sources()
    paths = ContainerPaths(config.name)

    print(f"name:     {config.name}")
    print(f"command:  {' '.join(config.runtime.command)}")
    print(f"cwd:      {config.runtime.cwd}")
    print(f"network:  {config.sandbox.network}")
    print(f"state:    {paths.root}")
    print("mounts:")
    for m in config.mounts:
        detail = f"{m.mode:9s} {m.dest}"
        if m.src:
            detail += f"  <- {m.src}"
        if m.mode == "worktree":
            detail += f"  [branch={m.branch or 'detached'} share={m.share}]"
        print(f"  {detail}")
    if config.files:
        print("files:")
        for f in config.files:
            origin = str(f.src) if f.src else "<inline>"
            print(f"  {f.dest}  <- {origin}")
    print("caps:")
    print(f"  parent: {', '.join(config.caps.parent) or 'none'}")
    if config.caps.factory:
        q = config.caps.factory.quota.containers
        print(f"  factory: {', '.join(config.caps.factory.rights)} (quota {q})")
    for p in config.caps.peers:
        print(f"  peer {p.container}: {', '.join(p.rights)}")
    for d in config.caps.dataspaces:
        print(f"  dataspace {d.path}: {', '.join(d.rights)}")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    """Delete a container's host-side state directory."""
    paths = ContainerPaths(args.name)
    if not paths.root.exists():
        print(f"nothing to clean for {args.name}")
        return 0
    if not args.yes:
        print(f"would remove {paths.root}")
        print("re-run with --yes to actually delete it")
        return 1
    force_rmtree(paths.root)
    print(f"removed {paths.root}")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    """Run the daemon, the containers and the web interface together.

    All in one process and one event loop, so an approval clicked in the browser
    resolves the future a blocked agent is waiting on directly.
    """
    import asyncio
    import socket

    import uvicorn

    from .daemon import Daemon
    from .web.app import create_app

    configs = [load_config(path) for path in args.configs]
    for config in configs:
        config.validate_sources()

    # Bind before starting anything. uvicorn only reports a bind failure once
    # it is already serving, which would otherwise leave containers running
    # behind a web interface that never came up.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((args.host, args.port))
    except OSError as exc:
        listener.close()
        raise CapwrapError(
            f"cannot bind {args.host}:{args.port}: {exc}. "
            "Another capwrap may already be running -- check with "
            f"`ss -ltn | grep {args.port}`, or pass a different --port."
        ) from None
    listener.listen(2048)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"capwrap: WARNING -- serving on {args.host}, and the web interface has\n"
            "         no authentication. Anyone who can reach this port can type\n"
            "         into any agent's terminal. Prefer binding to a VPN address,\n"
            "         or an SSH tunnel from the client.\n",
            file=sys.stderr,
        )

    async def run() -> None:
        daemon = Daemon()
        for config in configs:
            daemon.register(config)
        # Two passes, so configs may refer to each other in any order.
        daemon.link_all_peers()

        if not args.no_start:
            for config in configs:
                await daemon.start(config.name)

        app = create_app(daemon)
        server = uvicorn.Server(uvicorn.Config(
            app, log_level="warning", access_log=False,
        ))

        print(f"capwrap: {len(configs)} container(s) registered")
        for config in configs:
            print(f"  - {config.name}")
        shown = "127.0.0.1" if args.host == "0.0.0.0" else args.host
        print(f"\n  web interface: http://{shown}:{args.port}\n", flush=True)

        try:
            await server.serve(sockets=[listener])
        finally:
            await daemon.shutdown()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    root = state_root()
    print(root)
    containers = root / "containers"
    if containers.is_dir():
        for child in sorted(containers.iterdir()):
            print(f"  {child.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capwrap",
        description="Capability-governed bubblewrap containers for AI agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="check that this host can run capwrap")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("run", help="prepare and enter a container in the foreground")
    p.add_argument("config", help="path to a container .toml")
    p.add_argument("--name", help="override the container name")
    p.add_argument("--dry-run", action="store_true", help="print the bwrap command and exit")
    p.add_argument("--quiet", "-q", action="store_true")
    p.add_argument(
        "--overlay-backend", choices=["auto", "kernel", "fuse"], default="auto",
    )
    p.set_defaults(func=cmd_run, command=[])

    p = sub.add_parser("show", help="validate a config and show what it resolves to")
    p.add_argument("config")
    p.add_argument("--no-check", action="store_true",
                   help="skip checking that source paths exist")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser(
        "up", help="run containers under the daemon, with the web interface"
    )
    p.add_argument("configs", nargs="+", help="one or more container .toml files")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8420)
    p.add_argument("--no-start", action="store_true",
                   help="register the containers but do not launch them")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("clean", help="remove a container's host-side state")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("state", help="print the state directory and known containers")
    p.set_defaults(func=cmd_state)

    return parser


def _split_command(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split argv at the first bare ``--``.

    Everything after it is the command to run inside the sandbox.  Done by hand
    rather than with `argparse.REMAINDER`, which starts hoovering at the first
    positional and would swallow capwrap's own flags:
    ``capwrap run cfg.toml --dry-run`` would treat ``--dry-run`` as the command.
    """
    try:
        cut = argv.index("--")
    except ValueError:
        return argv, []
    return argv[:cut], argv[cut + 1 :]


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    raw, inner_command = _split_command(raw)

    parser = build_parser()
    args = parser.parse_args(raw)
    if inner_command:
        args.command = inner_command
    try:
        return args.func(args)
    except CapwrapError as exc:
        print(f"capwrap: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
