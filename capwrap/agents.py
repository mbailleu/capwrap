"""Agent profiles: what each agent's guest-side injection looks like.

capwrap makes a sandboxed agent useful by writing into the agent's own
configuration: settings files, approval shims, skills.  Every agent spells those
differently -- Claude Code reads `~/.claude/settings.json`, opencode reads
`~/.config/opencode/opencode.json` and loads plugins from its own plugins dir,
pi reads neither.  A profile captures those differences in one place so
`runtime.fsprep` stays agent-agnostic: it asks the profile where things live and
what shape they take, instead of hard-coding Claude.

The profile also decides *how* capwrap talks to the agent.  `hook_protocol`
names the guest-side approval shim installed when `approvals="capwrap"`; every
shim speaks the same guest->daemon protocol over the socket bwrap already
mounts, so the operator sees one inbox no matter which agent is asking.
`permission_encoder` names the translation from a normalized `Policy` into the
agent's native permission shape, so `runtime.permissions` works for every agent
that has one.

Role prompts are the third thing a profile decides.  When `runtime.role_prompt`
is set, the file is bound at GUEST_ROLE_PROMPT and each agent is told to read it
in its own way: a CLI flag inserted after the binary for claude and pi, an
`instructions` entry in opencode.json for opencode, and nothing but the bound
file for generic (which has no mechanism capwrap could wire).

`runtime.model` rides the same per-profile wiring: claude and pi get a `--model`
flag after the binary, opencode gets a `model` key in its (merged)
opencode.json, and generic gets nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import ContainerConfig, MountSpec
from .errors import ConfigError
from .paths import GUEST_HOME, GUEST_POLICY, GUEST_ROLE_PROMPT, GUEST_TOOLS


@dataclass(frozen=True)
class AgentProfile:
    """Everything fsprep needs to know about one agent's guest-side layout."""

    name: str
    #: Guest path of the agent's native settings file capwrap may write
    #: permission rules into, or None if the agent has none.
    settings_path: str | None
    #: Key of the encoder translating a normalized Policy into that file's
    #: native shape.  None = the agent has no native permission system.
    permission_encoder: str | None
    #: Which guest-side approval shim to install when approvals="capwrap".
    #: "opencode2" is the v2 plugin API; opencode v1 has no reliable in-process
    #: hook (its `permission.ask` was declared but never wired), so v1 gets None.
    hook_protocol: str | None  # "claude" | "opencode2" | "pi" | None
    #: Guest dest for the capctl skill, or None if unknown/unread.
    skill_path: str | None
    #: Host-side, non-interactive command that explains a permission request
    #: with this agent's own harness -- the binary and credentials the
    #: operator already has, never a new dependency.  "{prompt}" and
    #: "{model}" are placeholders (see fill_explain_argv); None = no
    #: explainer for this agent.
    explain_argv: tuple[str, ...] | None


_PROFILES: dict[str, AgentProfile] = {
    "claude": AgentProfile(
        name="claude",
        settings_path=f"{GUEST_HOME}/.claude/settings.json",
        permission_encoder="claude",
        hook_protocol="claude",
        skill_path=f"{GUEST_HOME}/.claude/skills/capwrap/SKILL.md",
        # Print mode with the mutating tools denied: the explainer describes,
        # it must not act.  Best-effort deny-list, not a sandbox -- a tool
        # added to a future claude version is not covered until it is listed
        # here.  The model rides the host claude's own gateway settings, so
        # no credentials are wired here.
        # The prompt goes before --disallowedTools: the flag is variadic and
        # would swallow a trailing prompt as another tool name.
        explain_argv=("claude", "--print", "{prompt}",
                      "--disallowedTools", "Bash,Write,Edit,NotebookEdit,"
                      "WebFetch,WebSearch,Task"),
    ),
    # opencode v1: permission rules via opencode.json work, but approval
    # routing does not -- the plugin hook that would intercept prompts exists
    # in the type system yet is never invoked (upstream #7006/#9229), so
    # claiming support would silently leave prompts in the agent's own TUI.
    "opencode": AgentProfile(
        name="opencode",
        settings_path=f"{GUEST_HOME}/.config/opencode/opencode.json",
        permission_encoder="opencode",
        hook_protocol=None,
        skill_path=f"{GUEST_HOME}/.config/opencode/skills/capwrap/SKILL.md",
        # v1's CLI has no tools-off flag for `run`; the explainer runs it in a
        # scratch cwd with a prompt that demands a direct answer (see
        # explain.py).  v2 has no `run` subcommand at all, so both opencode
        # profiles explain through the v1 binary -- same credentials, and the
        # container's model rides --model.
        explain_argv=("opencode", "run", "--model", "{model}", "{prompt}"),
    ),
    # opencode v2 (beta, installs as `opencode2`): the permission.evaluate
    # plugin hook is genuinely wired and blocks until it resolves, which is
    # what makes approval routing possible.  v2 reads its own config dir
    # (~/.config/opencode2, discovered via `debug config`), not v1's -- each
    # container has its own HOME and stages only its own agent's files, so
    # the two never see each other's injections.  Run both binaries in one
    # container and v1 will log a plugin-load error for the v2 shim on every
    # startup -- noisy, not fatal.  Note v2 also stores credentials in a
    # SQLite db, not v1's auth.json, so their credential mounts differ too.
    "opencode2": AgentProfile(
        name="opencode2",
        settings_path=f"{GUEST_HOME}/.config/opencode2/opencode.json",
        permission_encoder="opencode",
        hook_protocol="opencode2",
        skill_path=f"{GUEST_HOME}/.config/opencode2/skills/capwrap/SKILL.md",
        explain_argv=("opencode", "run", "--model", "{model}", "{prompt}"),
    ),
    "pi": AgentProfile(
        name="pi",
        settings_path=None,
        permission_encoder=None,
        hook_protocol="pi",
        skill_path=f"{GUEST_HOME}/.pi/agent/skills/capwrap/SKILL.md",
        # --no-tools is the hard guarantee: the explainer must not act.
        # --thinking mirrors the example's runtime command: some models
        # (glm-5.3-flash) refuse to run without an explicit level.
        explain_argv=("pi", "--print", "--no-session", "--no-tools",
                      "--thinking", "high",
                      "--model", "{model}", "{prompt}"),
    ),
    "generic": AgentProfile(
        name="generic",
        settings_path=None,
        permission_encoder=None,
        hook_protocol=None,
        skill_path=None,
        explain_argv=None,
    ),
}


def get_profile(name: str) -> AgentProfile:
    """Look up a profile by name, or fail loudly on a typo."""
    try:
        return _PROFILES[name]
    except KeyError:
        raise ConfigError(
            f"unknown agent profile {name!r}; known: {sorted(_PROFILES)}"
        ) from None


@dataclass(frozen=True)
class Injection:
    """One file to stage into the container's state dir and bind read-only.

    Exactly one of `content` (written from a string) or `src` (copied from a
    host file) is set; `runtime.fsprep._stage_all` does the actual staging.
    """

    staged_name: str
    dest: str
    content: str | None = None
    src: Path | None = None
    mode: int = 0o444

    def __post_init__(self) -> None:
        if (self.content is None) == (self.src is None):
            raise ValueError(
                f"injection {self.staged_name!r} needs exactly one of content or src"
            )


def _claude_hook(config: ContainerConfig) -> list[Injection]:
    """Claude Code settings wiring up the PreToolUse hook, plus the policy file.

    The settings are bound rather than written into HOME, because injected
    files are applied after every mount: a config that mounts its own
    $HOME/.claude (to bring in credentials) would otherwise shadow the hook
    registration and silently disable approval routing.  For the same reason
    the injection is a merge, not a replacement: the user's own settings --
    an env block routing claude through a local gateway, model preferences --
    are read from the covering mount and carried into the merged file, with
    capwrap's hooks and permissions replacing theirs outright (operator
    policy wins).
    """
    settings: dict = _read_user_settings(
        config, f"{GUEST_HOME}/.claude/settings.json"
    ) or {}
    settings["hooks"] = {
        "PreToolUse": [{
            "matcher": "*",
            "hooks": [{
                "type": "command",
                "command": f"{GUEST_TOOLS}/hook.py",
                "timeout": 3600,
            }],
        }],
    }
    permissions = config.runtime.permissions.to_policy().to_settings()
    if permissions:
        settings["permissions"] = permissions
    return [
        Injection(
            staged_name="claude-settings.json",
            content=json.dumps(settings, indent=2) + "\n",
            dest=f"{GUEST_HOME}/.claude/settings.json",
        ),
        _policy_injection(config),
    ]


def _opencode_hook(config: ContainerConfig) -> list[Injection]:
    """opencode v2 plugin that diverts prompts, plus the policy file.

    Uses v2's `permission.evaluate` hook, which runs for every tool call and
    blocks until it resolves -- v1's `permission.ask` is declared but never
    triggered, which is why the v1 profile has no shim at all.  The plugin and
    opencode.json are different files, so approvals and native permission
    rules compose without ever needing to merge JSON.
    """
    return [
        Injection(
            staged_name="opencode-plugin.ts",
            src=Path(__file__).resolve().parent / "guest" / "opencode-plugin.ts",
            dest=f"{GUEST_HOME}/.config/opencode2/plugins/capwrap.ts",
        ),
        _policy_injection(config),
    ]


def _pi_hook(config: ContainerConfig) -> list[Injection]:
    """pi extension that diverts prompts, plus the policy file."""
    return [
        Injection(
            staged_name="pi-extension.ts",
            src=Path(__file__).resolve().parent / "guest" / "pi-extension.ts",
            dest=f"{GUEST_HOME}/.pi/agent/extensions/capwrap-gate.ts",
        ),
        _policy_injection(config),
    ]


def _normalize_rule(rule: str) -> str:
    """Normalize one policy rule for the guest-side matchers.

    Both normalizations live here so the shims stay case- and syntax-dumb:
    the tool name is lowercased (claude capitalises "Read", opencode does
    not -- one policy fragment must match both), and a trailing ":*" -- the
    Claude prefix convention -- is rewritten to a plain glob "*", which is
    what opencode's matcher and the shims understand.  The pattern body
    keeps its case: paths are case-sensitive.
    """
    if "(" in rule and rule.endswith(")"):
        name, _, pattern = rule.partition("(")
        pattern = pattern[:-1]
        if pattern.endswith(":*"):
            pattern = pattern[:-2] + "*"
        return f"{name.lower()}({pattern})"
    return rule.lower()


def _policy_injection(config: ContainerConfig) -> Injection:
    """The auto-decisions file, bound read-only at GUEST_POLICY.

    `fallback` is decided here, on the host, so the shims stay dumb: "ask"
    when the encoded permission block gives the agent's own prompt a floor
    to fall back to, "deny" when it does not.  A shim that cannot reach the
    daemon follows it -- with an ask floor, falling through to the agent's
    own prompt is safe; without one, the agent's own decision would likely
    be "allow" and an unreachable daemon must not silently disable
    governance.
    """
    policy = config.runtime.permissions.to_policy()
    return Injection(
        staged_name="policy.json",
        content=json.dumps({
            "allow": [_normalize_rule(r) for r in config.runtime.auto_allow],
            "deny": [_normalize_rule(r) for r in config.runtime.auto_deny],
            "fallback": "ask" if policy.ask else "deny",
        }, indent=2) + "\n",
        dest=GUEST_POLICY,
    )


def _claude_settings(config: ContainerConfig) -> Injection:
    """Claude Code settings carrying only the permission rules.

    The native-approvals half of the claude encoder: same file the hook
    settings use, so under `approvals="capwrap"` the rules merge into the hook
    settings instead and this builder is not called at all.  Like the hook
    path, it merges over the user's own settings rather than replacing them.
    """
    settings = _read_user_settings(
        config, f"{GUEST_HOME}/.claude/settings.json"
    ) or {}
    settings["permissions"] = config.runtime.permissions.to_policy().to_settings()
    return Injection(
        staged_name="claude-settings.json",
        content=json.dumps(settings, indent=2) + "\n",
        dest=f"{GUEST_HOME}/.claude/settings.json",
    )


def guest_injections(profile: AgentProfile, config: ContainerConfig) -> list[Injection]:
    """Every file capwrap injects for this agent: approval shim, permission
    rules, role prompt.  The composition rules differ per profile and are the
    reason this is one function: claude's hook settings already merge the
    permission rules into the same file, opencode's live in a separate
    opencode.json that also carries the role prompt's `instructions` entry, and
    pi/generic have no native permission system at all.  `runtime.fsprep` calls
    this once and stages whatever comes back.
    """
    injections: list[Injection] = []

    if config.runtime.approvals == "capwrap":
        if profile.hook_protocol is None:
            raise ConfigError(
                f"agent {profile.name!r} has no approval shim; set approvals='native' "
                "or pick an agent with shim support"
            )
        if profile.hook_protocol == "claude":
            injections.extend(_claude_hook(config))
        elif profile.hook_protocol == "opencode2":
            injections.extend(_opencode_hook(config))
        elif profile.hook_protocol == "pi":
            injections.extend(_pi_hook(config))

    policy = config.runtime.permissions.to_policy()
    if profile.permission_encoder == "opencode":
        # One opencode.json carries the permission block, the role prompt's
        # instructions entry and the model; skip the file entirely when none of
        # them is set.  It is a different file from the approval plugin, so
        # approvals and permissions compose in both modes -- and this also fixes
        # the old silent drop of permissions under approvals="capwrap", which
        # used to skip the opencode.json entirely.
        if (
            not policy.is_empty
            or config.runtime.role_prompt is not None
            or config.runtime.model is not None
        ):
            injections.append(_opencode_settings(profile, config))
    elif profile.permission_encoder == "claude":
        if config.runtime.approvals != "capwrap" and not policy.is_empty:
            # Under capwrap the hook settings already merged the rules.
            injections.append(_claude_settings(config))
    elif not policy.is_empty:
        # pi/generic: no native permission system, in either approvals mode.
        # Under capwrap the rules used to be silently dropped, which is
        # worse than refusing to start.
        raise ConfigError(
            f"agent {profile.name!r} has no native permission system; use "
            "auto_allow/auto_deny with approvals='capwrap' instead"
        )

    if config.runtime.role_prompt is not None:
        # Bound for every profile, including generic -- generic users wire the
        # file into their command themselves.
        injections.append(
            Injection(
                staged_name="role-prompt.md",
                src=config.runtime.role_prompt,
                dest=GUEST_ROLE_PROMPT,
            )
        )

    return injections


def _opencode_settings(profile: AgentProfile, config: ContainerConfig) -> Injection:
    """The opencode.json carrying permission rules and/or the role prompt.

    opencode reads a single config file, so both halves land in the same dict:
    `permission` encodes the policy, `instructions` points the system prompt at
    the bound role prompt, and `model` selects the agent's model.  Any of the
    three may be absent; callers skip the file entirely when all are, so a
    role-prompt-only container still gets its instructions entry without
    inventing an empty permission block.

    opencode's config is config-dense -- providers, MCP servers and agents all
    live in the same file -- so when a mount covers the settings path (e.g. the
    examples mount the host's ~/.config/opencode into the sandbox), the user's
    real config is read from the host and capwrap's keys are merged on top
    rather than replacing the file.  That is unlike Claude's settings.json,
    where the hook/permissions merge already happens into one file.  capwrap's
    own keys must win because they are the operator's policy: `permission` and
    `model` replace outright, `instructions` appends to any the user already
    set, and the `agent` block merges per agent so the model pin lands inside
    the user's existing agent definitions.
    """
    settings: dict = {}
    policy = config.runtime.permissions.to_policy()
    if not policy.is_empty:
        settings["permission"] = policy.to_opencode()
    if config.runtime.role_prompt is not None:
        settings["instructions"] = [GUEST_ROLE_PROMPT]
    if config.runtime.model is not None:
        settings["model"] = config.runtime.model
        # opencode v2 migrates the legacy top-level `model` but its built-in
        # agents (Build, Plan, ...) pin their own model and ignore it, so the
        # per-agent pin is what actually selects the model there.  v1 reads
        # both; the agent entry wins there too.
        settings["agent"] = {"build": {"model": config.runtime.model}}

    assert profile.settings_path is not None, "opencode encoder implies a settings file"
    user = _read_user_settings(config, profile.settings_path)
    if user is not None:
        merged = dict(user)
        for key, value in settings.items():
            if key == "instructions" and isinstance(merged.get("instructions"), list):
                merged["instructions"] = list(merged["instructions"]) + list(value)
            elif key == "agent" and isinstance(merged.get("agent"), dict) and isinstance(value, dict):
                # Per-agent entries merge one level deep: capwrap's model pin
                # lands inside the user's agent block without discarding their
                # other agent definitions (orchestrator, council, ...).
                agents = dict(merged["agent"])
                for name, patch in value.items():
                    entry = dict(agents.get(name) or {})
                    entry.update(patch if isinstance(patch, dict) else {})
                    agents[name] = entry
                merged["agent"] = agents
            else:
                merged[key] = value
        settings = merged
    return Injection(
        staged_name="opencode-settings.json",
        content=json.dumps(settings, indent=2) + "\n",
        dest=profile.settings_path,
    )


def _strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas from JSONC.

    opencode configs may be JSONC; json.loads cannot read comments or
    trailing commas.  A state-machine strip (not a regex) so a "//" inside a
    string value survives: URLs like "https://..." are common in provider
    configs.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        if c == ",":
            # A trailing comma -- the next non-whitespace character closes the
            # object or array -- is legal JSONC; drop it.
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _read_user_settings(config: ContainerConfig, settings_path: str) -> dict | None:
    """The user's real settings file on the host, if a mount covers it.

    Works for any agent's settings path -- opencode.json, claude's
    settings.json -- because the mechanics are the same: find the covering
    mount, read the host original.  Returns None when no mount's `dest` is a
    parent of `settings_path` -- the fresh-file case, where capwrap's keys
    are all the file contains.  When a covering mount exists, the user's
    config lives on the host at ``m.src / rel``; a missing file falls back
    to ``{}`` silently (nothing of the user's to lose), while an
    unparseable one raises -- shadowing it would delete their config.  The
    deepest covering mount wins, since that is the one whose
    contents the sandbox actually sees.
    """
    best: MountSpec | None = None
    best_rel: str | None = None
    for mount in config.mounts:
        if mount.src is None:
            continue
        prefix = mount.dest.rstrip("/") + "/"
        if not settings_path.startswith(prefix):
            continue
        rel = settings_path[len(prefix):]
        if best is None or len(mount.dest) > len(best.dest):
            best, best_rel = mount, rel
    if best is None or best_rel is None:
        return None
    src = best.src
    if src is None:
        return None
    try:
        data = json.loads(_strip_jsonc((src / best_rel).read_text()))
    except FileNotFoundError:
        # The mount covers the directory but the file does not exist yet:
        # nothing of the user's to lose, capwrap's keys are all of it.
        return {}
    except (json.JSONDecodeError, ValueError) as exc:
        # Unparseable even after stripping comments.  Refuse to start rather
        # than shadow the user's config with capwrap-only keys -- that would
        # silently delete every provider, MCP server and agent they
        # configured, and the agent would fail far from the cause.
        raise ConfigError(
            f"{src / best_rel} is not parseable as JSON or JSONC ({exc}); "
            "capwrap would have to shadow it with its own keys, losing your "
            "config.  Fix the file's syntax and retry."
        ) from exc
    return data if isinstance(data, dict) else {}


def fill_explain_argv(
    profile: AgentProfile, prompt: str, model: str | None
) -> list[str]:
    """The host-side argv that asks this agent's harness to explain `prompt`.

    "{prompt}" becomes the prompt; "{model}" becomes the container's model,
    and when there is none the flag that targeted it is dropped -- the
    harness's own default beats an empty flag value.
    """
    if profile.explain_argv is None:
        raise ConfigError(f"agent {profile.name!r} has no explainer")
    argv: list[str] = []
    for token in profile.explain_argv:
        if token == "{prompt}":
            argv.append(prompt)
        elif token == "{model}":
            if model is None:
                if argv and argv[-1].startswith("-"):
                    argv.pop()
            else:
                argv.append(model)
        else:
            argv.append(token)
    return argv


def command_flags(profile: AgentProfile, config: ContainerConfig) -> list[str]:
    """CLI flags to insert after the agent binary: role prompt and model.

    Role-prompt flags: ``[]`` unless `config.runtime.role_prompt` is set --
    claude gets ``--append-system-prompt-file``, pi gets
    ``--append-system-prompt``, and everyone else gets nothing (opencode reads
    the prompt via its `instructions` config entry; generic has no flag at all).

    Model flags: claude and pi get ``--model <model>`` when
    `config.runtime.model` is set.  opencode/opencode2 get no flag -- the model
    lands in the merged opencode.json instead -- and generic has no mechanism.

    pi's resolvePromptInput reads the file at launch and would silently treat a
    missing path as literal text -- which is why the role-prompt flag is only
    emitted when capwrap also binds the file: the staged-files mechanism
    guarantees it exists before exec.
    """
    flags: list[str] = []
    if config.runtime.role_prompt is not None:
        if profile.name == "claude":
            flags += ["--append-system-prompt-file", GUEST_ROLE_PROMPT]
        elif profile.name == "pi":
            flags += ["--append-system-prompt", GUEST_ROLE_PROMPT]
    if config.runtime.model is not None:
        if profile.name in ("claude", "pi"):
            flags += ["--model", config.runtime.model]
    return flags