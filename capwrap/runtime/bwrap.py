"""Turn a config plus prepared mounts into a bubblewrap command line.

Pure function of its inputs -- it never touches the filesystem -- so the whole
sandbox shape is unit-testable without actually sandboxing anything.

Argument order is load-bearing.  bwrap applies operations in sequence against
the new root, so the base binds must come first and the container's own mounts
after, otherwise a `--ro-bind /usr /usr` would land on top of a mount the config
had already placed under /usr.
"""

from __future__ import annotations

import os
from pathlib import Path

from .. import agents
from ..config import ContainerConfig
from ..errors import SandboxError
from ..paths import (
    GUEST_GITDIR_ROOT,
    GUEST_HOME,
    GUEST_POLICY,
    GUEST_PROXY_PORT,
    GUEST_PROXY_SOCKET,
    GUEST_SHARED,
    GUEST_SIGNING_KEY,
    GUEST_SOCKET,
    GUEST_TOOLS,
    ContainerPaths,
)
from .fsprep import PreparedFs, ResolvedMount

#: Bound read-only when sandbox.base == "host-ro".  Missing entries are skipped,
#: so this works on both merged-/usr and split-/usr layouts.
#:
#: /nix is included because the host's tooling (bwrap itself, git, node) may live
#: in the nix store; without it a nix-installed binary cannot find its
#: interpreter.  /opt is deliberately absent so that GUEST_TOOLS can be mounted
#: at /opt/capwrap -- bwrap cannot mkdir inside a read-only bind.
HOST_RO_PATHS = ["/usr", "/lib", "/lib64", "/lib32", "/bin", "/sbin", "/etc", "/nix"]

#: /etc files that must be present for name resolution when networking is on.
RESOLV_PATHS = ["/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"]


def build_argv(
    config: ContainerConfig,
    prepared: PreparedFs,
    paths: ContainerPaths,
    *,
    bwrap: str = "bwrap",
    guest_tools: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> list[str]:
    """Full argv, from the bwrap binary through to the agent's command.

    The environment is *not* part of this; use `build_env` and pass the result as
    the process environment.
    """
    argv: list[str] = [bwrap]
    argv += _namespace_args(config)
    argv += _base_args(config)
    # capwrap's own mounts (HOME, /shared, the socket) go before the config's,
    # so a config can layer over them -- e.g. mounting a private copy of an
    # agent's credential directory at $HOME/.claude.
    argv += _capwrap_args(paths, guest_tools, proxied=config.proxied_network)
    argv += _mount_args(prepared.mounts)
    # Injected files come last and therefore win over every mount.
    argv += _file_args(prepared.files)
    # Note: no --setenv here. The environment is handed to bwrap as its own, and
    # inherited by the child, so secrets stay out of the world-readable argv.
    # See `build_env`.
    argv += ["--chdir", config.runtime.cwd]
    argv += ["--"]
    command = _entry_command(config)
    # Profile decoration: claude and pi read the bound role prompt via a CLI
    # flag, and the runtime model rides the same slot; opencode gets the model
    # in its merged opencode.json instead.  Flags are APPENDED, not inserted
    # after argv[0]: pi's command is ["node", ".../cli.js"], and `node --model`
    # dies with exit 9 (invalid node option).  Commander-style parsers accept
    # flags after positionals, so the end is safe for every wired agent.
    flags = agents.command_flags(agents.get_profile(config.runtime.agent), config)
    if flags:
        command = [*command, *flags]
    argv += command
    return argv


def _entry_command(config: ContainerConfig) -> list[str]:
    """What actually runs inside: the agent, or the relay wrapping it.

    A proxied container has no route to the host, so the proxy is reachable only
    over a bind-mounted unix socket -- which no HTTP client can be pointed at.
    The relay turns it into a loopback port that every client understands, and
    runs as the entry point so it cannot outlive the agent it exists for.
    """
    command = list(config.runtime.command)
    if not config.proxied_network:
        return command
    return [
        "/usr/bin/env", "python3", f"{GUEST_TOOLS}/netrelay.py",
        "--port", str(GUEST_PROXY_PORT),
        "--socket", GUEST_PROXY_SOCKET,
        "--", *command,
    ]


# --------------------------------------------------------------------------


def _namespace_args(config: ContainerConfig) -> list[str]:
    sb = config.sandbox
    args = ["--unshare-user"]

    requested = set(sb.unshare)
    if not sb.network:
        # Proxied containers land here too, and must: the proxy only means
        # anything if the container has no other way out.
        requested.add("net")
    elif "net" in requested:
        raise SandboxError(
            "sandbox.network = true conflicts with 'net' in sandbox.unshare"
        )

    for ns in ("pid", "ipc", "uts", "cgroup", "net"):
        if ns in requested:
            args.append(f"--unshare-{ns}")

    if sb.uid is not None:
        args += ["--uid", str(sb.uid)]
    if sb.gid is not None:
        args += ["--gid", str(sb.gid)]
    if sb.hostname:
        if "uts" not in requested:
            raise SandboxError(
                "sandbox.hostname requires 'uts' in sandbox.unshare"
            )
        args += ["--hostname", sb.hostname]
    if sb.die_with_parent:
        args.append("--die-with-parent")
    if sb.new_session:
        # Blocks TIOCSTI-style injection back into the controlling terminal.
        # Off by default because it also detaches the PTY that we want the agent
        # to run under; the supervisor allocates a fresh one either way.
        args.append("--new-session")
    return args


def _base_args(config: ContainerConfig) -> list[str]:
    """The read-only skeleton of a working userland."""
    if config.sandbox.base == "none":
        return ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]

    args: list[str] = []
    for path in HOST_RO_PATHS:
        p = Path(path)
        if not p.exists():
            continue
        if p.is_symlink():
            # /bin -> usr/bin on merged systems: recreate the link rather than
            # binding through it, so the sandbox keeps the same shape.
            args += ["--symlink", os.readlink(path), path]
        else:
            args += ["--ro-bind", path, path]

    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    # A writable run/ for the control socket's mountpoint and anything the agent
    # wants to drop there.
    args += ["--tmpfs", "/run", "--tmpfs", "/var/tmp"]

    if config.sandbox.network or config.proxied_network:
        # A proxied container resolves nothing itself -- the proxy does the DNS
        # -- but TLS libraries and HTTP clients still read /etc/hosts and
        # /etc/nsswitch.conf on the way past, and a dangling symlink there is an
        # error rather than a no-op.
        args += _resolver_args()
    return args


def _resolver_args() -> list[str]:
    """Make DNS work, following symlinks out of /etc.

    On systemd-resolved hosts /etc/resolv.conf is a symlink into /run, and /run
    is a fresh tmpfs inside the sandbox -- so binding /etc read-only produces a
    *dangling* symlink, and binding the file onto itself then fails with
    "Can't create file at /etc/resolv.conf". Binding the resolved target at its
    own path instead makes the existing symlink land somewhere real.
    """
    args: list[str] = []
    for path in RESOLV_PATHS:
        p = Path(path)
        if not p.exists():
            continue
        # Bind the resolved file at its own path. When the /etc entry is a
        # symlink this gives it something to point at; when it is a plain file
        # the resolved path is the path, so the same line covers both.
        real = p.resolve()
        args += ["--ro-bind", str(real), str(real)]
    return args


def _mount_depth(dest: str) -> tuple[int, str]:
    """Sort key placing parents before children.

    bwrap applies mount operations in argv order, so a `--ro-bind ... /a` emitted
    after an overlay at `/a/b` silently buries it: the agent gets a read-only
    `/a/b` and no error anywhere.  Configs are declarative, so the order someone
    happens to write `[[mounts]]` in must not change what they get -- nesting is
    resolved here instead.
    """
    parts = [p for p in dest.split("/") if p]
    return (len(parts), dest)


def _mount_args(mounts: list[ResolvedMount]) -> list[str]:
    args: list[str] = []
    # Stable sort, so mounts at the same depth keep their configured order.
    for m in sorted(mounts, key=lambda m: _mount_depth(m.dest)):
        if m.kind == "ro":
            args += ["--ro-bind", str(m.src), m.dest]
        elif m.kind == "rw":
            args += ["--bind", str(m.src), m.dest]
        elif m.kind == "tmpfs":
            if m.size:
                args += ["--size", _parse_size(m.size)]
            args += ["--tmpfs", m.dest]
        elif m.kind == "overlay":
            # --overlay-src is positional: it applies to the *next* --overlay.
            args += ["--overlay-src", str(m.lower)]
            args += ["--overlay", str(m.upper), str(m.work), m.dest]
        else:  # pragma: no cover - guarded by the type
            raise SandboxError(f"unknown mount kind {m.kind!r}")
    return args


def _capwrap_args(
    paths: ContainerPaths, guest_tools: Path | None, proxied: bool = False
) -> list[str]:
    """Mount the container's single channel to the outside world.

    The agent socket is the container's only route to the capability kernel, and
    identity is established by *which* socket a connection arrives on -- so there
    is no token an agent could steal, guess or forge.
    """
    args = [
        "--bind", str(paths.shared), GUEST_SHARED,
        "--bind", str(paths.home), GUEST_HOME,
    ]
    if paths.socket.exists():
        args += ["--bind", str(paths.socket), GUEST_SOCKET]
    if proxied and paths.proxy_socket.exists():
        args += ["--bind", str(paths.proxy_socket), GUEST_PROXY_SOCKET]
    # Read-only: the container signs with it and has no business rewriting the
    # identity its earlier posts were made under.
    if paths.signing_key.exists():
        args += ["--ro-bind", str(paths.signing_key), GUEST_SIGNING_KEY]
    if guest_tools is not None and guest_tools.exists():
        args += ["--ro-bind", str(guest_tools), GUEST_TOOLS]
    return args


def _file_args(files: list[tuple[Path, str]]) -> list[str]:
    """Individually bound files from ``[[files]]``.

    Bound rather than copied into place so that injecting a file into an overlay
    or a git worktree does not dirty it -- an injected CLAUDE.md should not turn
    up in the agent's `git status`.
    """
    args: list[str] = []
    for staged, dest in files:
        args += ["--ro-bind", str(staged), dest]
    return args


#: Environment variable names whose values must never be printed.  Matched as a
#: substring, so ANTHROPIC_AUTH_TOKEN and MY_API_KEY are both covered.
SECRET_HINTS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")


def is_secret(name: str) -> bool:
    return any(hint in name.upper() for hint in SECRET_HINTS)


def redact(env: dict[str, str]) -> dict[str, str]:
    """Env with secret values masked, for `--dry-run` and the web UI."""
    return {
        k: ("<redacted>" if is_secret(k) and v else v) for k, v in env.items()
    }


def build_env(config: ContainerConfig, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The complete environment the container's process will see.

    Handed to bwrap as *its own* environment and inherited by the child, rather
    than passed as `--setenv` pairs. That is a security requirement, not a
    style choice: argv is world-readable through /proc/<pid>/cmdline, so
    `--setenv ANTHROPIC_AUTH_TOKEN sk-ant-...` publishes the token to every
    user on the host. An inherited environment is readable only by the process
    owner (/proc/<pid>/environ is 0400).
    """
    env: dict[str, str] = {}
    if not config.sandbox.clear_env:
        # Opt-in inheritance of the daemon's whole environment.
        env.update(os.environ)

    env.update({
        "HOME": GUEST_HOME,
        "USER": "agent",
        "LOGNAME": "agent",
        # GUEST_TOOLS first, so `capctl` is on PATH without the agent being told
        # where it lives.
        "PATH": f"{GUEST_TOOLS}:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
        "SHELL": "/bin/bash",
        "TMPDIR": "/tmp",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        # Where guest tooling looks for the kernel, so capctl needs no arguments.
        "CAPWRAP_SOCKET": GUEST_SOCKET,
        "CAPWRAP_CONTAINER": config.name,
        "CAPWRAP_SHARED": GUEST_SHARED,
        "CAPWRAP_POLICY": GUEST_POLICY,
        "CAPWRAP_SIGNING_KEY": GUEST_SIGNING_KEY,
        "GIT_CONFIG_GLOBAL": f"{GUEST_HOME}/.gitconfig",
    })
    if config.proxied_network:
        # Both cases, because tooling is split on which it reads, and `no_proxy`
        # keeps the loopback relay itself from being proxied through itself.
        proxy = f"http://127.0.0.1:{GUEST_PROXY_PORT}"
        env.update({
            "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
            "http_proxy": proxy, "https_proxy": proxy,
            "ALL_PROXY": proxy, "all_proxy": proxy,
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        })
    if config.runtime.tty:
        env["TERM"] = os.environ.get("TERM", "xterm-256color")
    env.update(extra or {})

    # Named variables lifted from the daemon's environment. This is how a token
    # reaches the agent without ever being written into a config file.
    for name in config.runtime.env_from_host:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value

    # A KEY=VALUE file, for the same reason but persisted outside the repo.
    env.update(_read_env_file(config))

    # The config's own literal values win, so it can override PATH or TERM.
    env.update(config.runtime.env)
    return {k: str(v) for k, v in env.items() if v is not None}


def _read_env_file(config: ContainerConfig) -> dict[str, str]:
    """Parse `runtime.env_file`: KEY=VALUE lines, `#` comments, blanks ignored."""
    path = config.runtime.env_file
    if path is None:
        return {}
    if not path.is_file():
        raise SandboxError(f"runtime.env_file: no such file: {path}")

    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SandboxError(f"{path}:{number}: expected KEY=VALUE, got {line!r}")
        key, _, value = line.partition("=")
        value = value.strip()
        # Tolerate quoted values, which is how people write them by habit.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


_UNITS = {"k": 1024, "m": 1024**2, "g": 1024**3}


def _parse_size(size: str) -> str:
    """Normalise '256m' to a byte count for bwrap's --size."""
    text = size.strip().lower().rstrip("b")
    if not text:
        raise SandboxError(f"invalid tmpfs size {size!r}")
    if text[-1] in _UNITS:
        try:
            value = float(text[:-1])
        except ValueError:
            raise SandboxError(f"invalid tmpfs size {size!r}") from None
        return str(int(value * _UNITS[text[-1]]))
    if not text.isdigit():
        raise SandboxError(f"invalid tmpfs size {size!r}")
    return text


def render(argv: list[str]) -> str:
    """Shell-ish rendering of an argv, for `capwrap run --dry-run` and debugging."""
    import shlex

    out: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in ("--ro-bind", "--bind", "--overlay") or token == "--symlink":
            n = 4 if token == "--overlay" else 3
            out.append("  " + " ".join(shlex.quote(a) for a in argv[i : i + n]))
            i += n
        elif token in ("--setenv",):
            out.append("  " + " ".join(shlex.quote(a) for a in argv[i : i + 3]))
            i += 3
        elif token in ("--overlay-src", "--tmpfs", "--proc", "--dev", "--chdir",
                       "--uid", "--gid", "--hostname", "--size"):
            out.append("  " + " ".join(shlex.quote(a) for a in argv[i : i + 2]))
            i += 2
        else:
            out.append("  " + shlex.quote(token))
            i += 1
    return " \\\n".join(out).lstrip()
