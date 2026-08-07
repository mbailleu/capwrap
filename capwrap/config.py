"""Container config files: TOML in, validated `ContainerConfig` out.

Relative paths in a config resolve against the directory holding the config file,
not the process CWD, so an examples/ directory is self-contained and configs stay
movable.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ConfigError
from .kernel.rights import Rights, parse_rights

MountMode = Literal["ro", "rw", "overlay", "copy", "tmpfs", "worktree"]
ShareMode = Literal["objects", "none"]
OnDestroy = Literal["keep", "remove"]


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MountSpec(Base):
    """One entry in ``[[mounts]]``.

    The mode decides what `runtime.fsprep` materialises on the host before the
    sandbox starts, and what bwrap arguments it turns into:

    ``ro``        `--ro-bind` straight through.
    ``rw``        `--bind` straight through; changes hit the host immediately.
    ``tmpfs``     `--tmpfs`; nothing shared, nothing persisted.
    ``copy``      a private copy under the container's state dir, bound rw.
    ``overlay``   `src` as a shared read-only lower, private upper per container.
                  The point of the whole project: agents share a view without
                  sharing writes, so they never need to coordinate.
    ``worktree``  a private git worktree on its own branch, bound rw.  Better
                  than overlay for a repo, because integrating the result is an
                  ordinary `git merge` instead of a hand-diff of upper dirs.
    """

    src: Path | None = None
    dest: str
    mode: MountMode = "ro"

    # -- tmpfs ---------------------------------------------------------
    size: str | None = Field(default=None, description="tmpfs size, e.g. '256m'")

    # -- worktree ------------------------------------------------------
    share: ShareMode = "objects"
    branch: str | None = Field(
        default=None, description="defaults to capwrap/<container name>"
    )
    base: str = Field(default="HEAD", description="commit-ish the worktree starts at")
    detach: bool = False
    on_destroy: OnDestroy = "keep"

    @field_validator("dest")
    @classmethod
    def _dest_absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"mount dest must be an absolute path, got {v!r}")
        return v.rstrip("/") or "/"

    @model_validator(mode="after")
    def _check_mode_fields(self) -> "MountSpec":
        if self.mode == "tmpfs":
            if self.src is not None:
                raise ValueError("tmpfs mounts must not set 'src'")
        elif self.src is None:
            raise ValueError(f"mode={self.mode!r} requires 'src'")

        if self.mode != "worktree":
            for field in ("branch", "detach"):
                if getattr(self, field) != MountSpec.model_fields[field].default:
                    raise ValueError(f"'{field}' is only valid with mode='worktree'")
        if self.mode != "tmpfs" and self.size is not None:
            raise ValueError("'size' is only valid with mode='tmpfs'")
        if self.detach and self.branch:
            raise ValueError("'detach' and 'branch' are mutually exclusive")
        return self

    @property
    def slug_source(self) -> str:
        return self.dest


class FileSpec(Base):
    """One entry in ``[[files]]`` -- an individual file injected into the sandbox.

    Either copied from ``src`` or written from inline ``content``.  Staged into
    the container's state dir and bound read-only, so the sandbox sees the file
    even when its parent directory is an overlay or a fresh tmpfs.
    """

    src: Path | None = None
    content: str | None = None
    dest: str
    mode: str = "0644"

    @field_validator("dest")
    @classmethod
    def _dest_absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"file dest must be an absolute path, got {v!r}")
        return v

    @field_validator("mode")
    @classmethod
    def _mode_octal(cls, v: str) -> str:
        try:
            int(v, 8)
        except ValueError:
            raise ValueError(f"mode must be octal, e.g. '0644', got {v!r}") from None
        return v

    @property
    def file_mode(self) -> int:
        return int(self.mode, 8)

    @model_validator(mode="after")
    def _one_source(self) -> "FileSpec":
        if (self.src is None) == (self.content is None):
            raise ValueError("a [[files]] entry needs exactly one of 'src' or 'content'")
        return self


class SandboxSpec(Base):
    """Namespace and identity settings -- the bwrap-shaped part of a config."""

    #: "host-ro" binds the host's /usr, /lib, /etc read-only.  "none" starts
    #: empty, for configs that bind everything they need explicitly.
    base: Literal["host-ro", "none"] = "host-ro"
    network: bool = False
    hostname: str | None = None
    uid: int | None = None
    gid: int | None = None
    unshare: list[str] = Field(default_factory=lambda: ["pid", "ipc", "uts", "cgroup"])
    die_with_parent: bool = True
    new_session: bool = False
    clear_env: bool = True

    @field_validator("unshare")
    @classmethod
    def _known_namespaces(cls, v: list[str]) -> list[str]:
        known = {"user", "pid", "ipc", "uts", "cgroup", "net"}
        bad = sorted(set(v) - known)
        if bad:
            raise ValueError(f"unknown namespace(s) {bad}; known: {sorted(known)}")
        if "pid" not in v:
            # Not negotiable. The PID namespace is what makes container teardown
            # complete: bwrap is pid 1 inside it, and when pid 1 exits the kernel
            # SIGKILLs everything else in the namespace. Without it, a process
            # that calls setsid() leaves its own process group and survives
            # `capwrap stop` entirely -- an agent could outlive its container.
            raise ValueError(
                "sandbox.unshare must include 'pid'; without a PID namespace, "
                "processes that call setsid() survive container teardown"
            )
        return v


class PermissionSpec(Base):
    """Claude Code permission rules, set from the container's own config.

    Written into the sandbox's settings.json.  Because a container can spawn
    children, this is also an escalation surface: the child's policy is checked
    against its parent's *envelope* before the spawn is allowed (see
    `capwrap.kernel.policy`).
    """

    allow: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    #: "plan" | "default" | "acceptEdits" | "bypassPermissions".
    default_mode: str | None = None

    @field_validator("default_mode")
    @classmethod
    def _known_mode(cls, v: str | None) -> str | None:
        from .kernel.policy import MODE_ORDER

        if v is not None and v not in MODE_ORDER:
            raise ValueError(f"default_mode must be one of {MODE_ORDER}, got {v!r}")
        return v

    @model_validator(mode="after")
    def _rules_parse(self) -> "PermissionSpec":
        from .kernel.policy import Rule

        for field_name in ("allow", "ask", "deny"):
            for text in getattr(self, field_name):
                rule = Rule.parse(text)
                if not rule.tool:
                    raise ValueError(f"{field_name}: {text!r} has no tool name")
        return self

    def to_policy(self):
        from .kernel.policy import Policy

        return Policy.from_lists(self.allow, self.ask, self.deny, self.default_mode)


class RuntimeSpec(Base):
    """What to run inside the sandbox."""

    command: list[str] = Field(default_factory=lambda: ["/bin/bash"])
    cwd: str = "/"
    tty: bool = True
    env: dict[str, str] = Field(default_factory=dict)
    #: Names lifted from the daemon's own environment. The way to give an agent
    #: a token without writing it into a config file that lands in git.
    env_from_host: list[str] = Field(default_factory=list)
    #: A KEY=VALUE file, read at launch. Same purpose, persisted outside the repo.
    env_file: Path | None = None
    #: How the container is told a message arrived: written to /shared/inbox
    #: ("file"), typed into its PTY ("pty"), or not at all.
    notify: Literal["file", "pty", "none"] = "file"

    #: Where the agent's permission prompts go.  "capwrap" installs a Claude
    #: Code PreToolUse hook that diverts them to the operator's web inbox, so
    #: several agents' prompts queue in one place instead of each blocking in
    #: its own terminal.  "native" leaves Claude to prompt in its own TUI.
    approvals: Literal["native", "capwrap"] = "native"
    #: Install a Claude Code skill describing capctl, so an agent discovers how
    #: to message peers and ask the operator without it being repeated in every
    #: prompt.  Inert for agents that do not read skills.
    capctl_skill: bool = True
    #: Tool patterns auto-decided without troubling the operator. Either a bare
    #: tool name (``"Read"``) or ``Tool(glob)`` matched against its main
    #: argument (``"Bash(git *)"``).
    auto_allow: list[str] = Field(default_factory=list)
    auto_deny: list[str] = Field(default_factory=list)

    #: Claude's own permission rules, merged into the sandbox's settings.json.
    permissions: PermissionSpec = Field(default_factory=PermissionSpec)
    #: The widest policy this container may give a child. Defaults to its own
    #: permissions, so children can only ever narrow. Set it explicitly to
    #: pre-authorise a range once instead of approving each spawn by hand.
    permission_envelope: PermissionSpec | None = None

    def envelope_policy(self):
        """The bound on what this container may hand to a child."""
        spec = self.permission_envelope or self.permissions
        return spec.to_policy()

    @field_validator("command")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("runtime.command must not be empty")
        return v


class PeerCap(Base):
    """An initial capability on another container."""

    container: str
    rights: list[str] = Field(default_factory=lambda: ["send"])

    @property
    def mask(self) -> Rights:
        return parse_rights(self.rights)


class DataspaceCap(Base):
    """An initial capability on a host path."""

    path: Path
    rights: list[str] = Field(default_factory=lambda: ["read"])
    label: str | None = None
    kind: Literal["dir", "file", "git_repo"] = "dir"

    @property
    def mask(self) -> Rights:
        return parse_rights(self.rights)


class Quota(Base):
    containers: int = 0


class FactoryCap(Base):
    """Authority to create new containers, and how many."""

    rights: list[str] = Field(default_factory=lambda: ["create"])
    quota: Quota = Field(default_factory=Quota)

    @property
    def mask(self) -> Rights:
        return parse_rights(self.rights)


class CapsSpec(Base):
    """The container's initial capability table.

    Everything a container may ever do starts here or is delegated to it later;
    there is no ambient authority.
    """

    #: Rights on the spawning container, so a child can talk back to its parent.
    parent: list[str] = Field(default_factory=lambda: ["send"])
    factory: FactoryCap | None = None
    peers: list[PeerCap] = Field(default_factory=list)
    dataspaces: list[DataspaceCap] = Field(default_factory=list)

    @property
    def parent_mask(self) -> Rights:
        return parse_rights(self.parent)


class ContainerConfig(Base):
    """A fully validated container definition."""

    name: str
    runtime: RuntimeSpec = Field(default_factory=RuntimeSpec)
    sandbox: SandboxSpec = Field(default_factory=SandboxSpec)
    mounts: list[MountSpec] = Field(default_factory=list)
    files: list[FileSpec] = Field(default_factory=list)
    caps: CapsSpec = Field(default_factory=CapsSpec)

    #: Directory the config was loaded from; relative paths resolve against it.
    source_dir: Path | None = Field(default=None, exclude=True)

    @field_validator("name")
    @classmethod
    def _name_safe(cls, v: str) -> str:
        if not v or not all(c.isalnum() or c in "-_." for c in v):
            raise ValueError(
                f"container name {v!r} must be non-empty and use only "
                "alphanumerics, '-', '_' or '.'"
            )
        return v

    @model_validator(mode="after")
    def _unique_destinations(self) -> "ContainerConfig":
        seen: set[str] = set()
        for m in self.mounts:
            if m.dest in seen:
                raise ValueError(f"two mounts both target {m.dest!r}")
            seen.add(m.dest)
        return self

    def resolve_paths(self, base_dir: Path) -> "ContainerConfig":
        """Make every relative path absolute, against `base_dir`.

        Also fills in the default worktree branch, which depends on the
        container name and so cannot be a field default.
        """
        self.source_dir = base_dir
        for mount in self.mounts:
            if mount.src is not None:
                mount.src = _abs(mount.src, base_dir)
            if mount.mode == "worktree" and not mount.branch and not mount.detach:
                mount.branch = f"capwrap/{self.name}"
        if self.runtime.env_file is not None:
            self.runtime.env_file = _abs(self.runtime.env_file, base_dir)
        for spec in self.files:
            if spec.src is not None:
                spec.src = _abs(spec.src, base_dir)
        for ds in self.caps.dataspaces:
            ds.path = _abs(ds.path, base_dir)
        return self

    def validate_sources(self) -> None:
        """Check that every referenced host path actually exists.

        Kept out of the pydantic validators so configs can be parsed and shown
        (in the web UI, in tests) on a machine that lacks the paths.
        """
        for mount in self.mounts:
            if mount.src is not None and not mount.src.exists():
                raise ConfigError(
                    f"mount {mount.dest}: source {mount.src} does not exist"
                )
            if mount.mode == "worktree" and mount.src is not None:
                if not (mount.src / ".git").exists():
                    raise ConfigError(
                        f"mount {mount.dest}: mode='worktree' needs a git repo, "
                        f"but {mount.src}/.git is missing"
                    )
        for spec in self.files:
            if spec.src is not None and not spec.src.is_file():
                raise ConfigError(f"file {spec.dest}: source {spec.src} is not a file")
        for ds in self.caps.dataspaces:
            if not ds.path.exists():
                raise ConfigError(f"dataspace {ds.path} does not exist")


def _abs(path: Path, base_dir: Path) -> Path:
    path = path.expanduser()
    return path if path.is_absolute() else (base_dir / path).resolve()


def load_config(path: str | Path) -> ContainerConfig:
    """Parse and validate a container config file."""
    path = Path(path).expanduser().resolve()
    try:
        raw: dict[str, Any] = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"no such config file: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None
    return load_config_data(raw, base_dir=path.parent, origin=str(path))


def load_config_data(
    raw: dict[str, Any], base_dir: Path, origin: str = "<config>"
) -> ContainerConfig:
    """Validate an already-parsed config mapping.

    Split out from `load_config` so the daemon can validate a config that an
    agent submitted over IPC without it ever touching the filesystem.
    """
    from pydantic import ValidationError

    try:
        config = ContainerConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{origin}: {_format_errors(exc)}") from None
    return config.resolve_paths(base_dir)


def _format_errors(exc: Any) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"{loc}: {err['msg']}")
    return "; ".join(lines)


#: Annotated alias used by the IPC layer for configs arriving over the wire.
InlineConfig = Annotated[dict, Field(description="a container config, as a mapping")]
