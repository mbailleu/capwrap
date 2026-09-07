#!/usr/bin/env python3
"""Build a capwrap config for a role crossed with a persona.

Roles and personas are orthogonal, and they are not the same *kind* of thing:

- A **role** is a job and a set of capabilities. It is enforced. A reviewer with
  `Write` denied cannot write, whatever it decides about itself mid-session.
- A **persona** is a disposition. It is a prompt and nothing else, and it cannot
  be enforced at all. A "lazy" agent that you actually need to be unable to do
  something needs a different *role*, not a stronger adjective.

The capability shape of each role lives in the table below, which is the part
worth reading. The personas contribute nothing to it -- deliberately, because
pretending otherwise would be the whole mistake this example exists to avoid.

    ./compose.py architect:idealist architect:pragmatist
    capwrap up examples/roles-and-personas/built/*.toml

    ./compose.py --list
    ./compose.py --all-personas reviewer      # one role, every disposition
"""

from __future__ import annotations

import argparse
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
BUILT = HERE / "built"

#: What a role may do, as capwrap and Claude both enforce it.
#:
#: `permissions` are Claude's own rules, so a denied tool is refused inside the
#: agent. `write` says what the container gets of the repository. `factory`
#: gives it the authority to create other containers at all.
ROLES: dict[str, dict] = {
    # -- decide, do not implement -------------------------------------
    "architect": {
        "summary": "decides the shape; writes notes, not code",
        "allow": ["Read", "Glob", "Grep", "Write", "Bash(git log:*)", "Bash(git diff:*)"],
        "deny": ["Edit", "Bash(sudo *)"],
        "work": "worktree",
    },
    "designer": {
        "summary": "owns how it is used; source is read-only",
        "allow": ["Read", "Glob", "Grep", "Write"],
        "deny": ["Edit", "Bash(sudo *)"],
        "work": "worktree",
    },
    "product-owner": {
        "summary": "decides what is built and why; no repository at all",
        "allow": ["Read", "Write", "TodoWrite"],
        "deny": ["Edit", "Bash(sudo *)"],
        "work": "none",
    },
    "api-owner": {
        "summary": "owns the public surface; writes a decision log",
        "allow": ["Read", "Glob", "Grep", "Write", "Bash(git log:*)", "Bash(git diff:*)"],
        "deny": ["Edit", "Bash(sudo *)"],
        "work": "worktree",
    },
    "domain-expert": {
        "summary": "knows the problem, not the code; no repository at all",
        "allow": ["Read", "Write", "TodoWrite"],
        "deny": ["Edit", "Bash(sudo *)"],
        "work": "none",
    },

    # -- read, report, change nothing ---------------------------------
    "reviewer": {
        "summary": "reviews a diff; cannot write, by construction",
        "allow": ["Read", "Glob", "Grep", "Bash(git log:*)", "Bash(git diff:*)",
                  "Bash(git show:*)"],
        "deny": ["Write", "Edit", "Bash(sudo *)"],
        "work": "worktree",
    },
    "security-reviewer": {
        "summary": "reviews for hostile input; no network, so no unsourced claims",
        "allow": ["Read", "Glob", "Grep", "Bash(git log:*)", "Bash(git diff:*)"],
        "deny": ["Write", "Edit", "WebFetch", "WebSearch", "Bash(sudo *)"],
        "work": "worktree",
        "network": False,
    },
    "accessibility-reviewer": {
        "summary": "reviews for people using it without a mouse or without sight",
        "allow": ["Read", "Glob", "Grep"],
        "deny": ["Write", "Edit", "Bash(sudo *)"],
        "work": "worktree",
    },
    "archaeologist": {
        "summary": "works out why the code is the way it is; writes nothing",
        "allow": ["Read", "Glob", "Grep", "Bash(git log:*)", "Bash(git blame:*)",
                  "Bash(git show:*)", "Bash(git diff:*)"],
        "deny": ["Write", "Edit", "Bash(sudo *)"],
        "work": "worktree",
    },
    "user": {
        "summary": "not on the team; sees the built artifact, never the source",
        "allow": ["Read", "Bash(ls:*)"],
        "deny": ["Write", "Edit", "Glob", "Grep", "Bash(sudo *)"],
        "work": "none",
    },

    # -- write code ----------------------------------------------------
    "implementer": {
        "summary": "makes it work, in the smallest change that does",
        "allow": ["Read", "Glob", "Grep", "Write", "Edit", "Bash(git:*)"],
        "deny": ["Bash(sudo *)"],
        "work": "worktree",
    },
    "refactorer": {
        "summary": "changes how it is written, not what it does",
        "allow": ["Read", "Glob", "Grep", "Write", "Edit", "Bash(git:*)"],
        "deny": ["Bash(sudo *)"],
        "work": "worktree",
    },
    "debugger": {
        "summary": "finds out why; may watch and drive another agent's terminal",
        "allow": ["Read", "Glob", "Grep", "Write", "Edit", "Bash(git:*)"],
        "deny": ["Bash(sudo *)"],
        "work": "worktree",
    },
    "performance-engineer": {
        "summary": "measures first; every claim has a before and an after",
        "allow": ["Read", "Glob", "Grep", "Write", "Edit", "Bash(git:*)"],
        "deny": ["Bash(sudo *)"],
        "work": "worktree",
    },
    "integrator": {
        "summary": "merges branches and keeps main working",
        "allow": ["Read", "Glob", "Grep", "Write", "Edit", "Bash(git:*)"],
        "deny": ["Bash(sudo *)"],
        "work": "worktree",
    },
    "build-engineer": {
        "summary": "owns the toolchain; needs the network, so says what it pulled in",
        "allow": ["Read", "Glob", "Grep", "Write", "Edit", "Bash"],
        "deny": ["Bash(sudo *)"],
        "work": "worktree",
    },
    "technical-writer": {
        "summary": "writes the documentation; reads the source, changes none of it",
        "allow": ["Read", "Glob", "Grep", "Write", "Bash(git log:*)"],
        "deny": ["Edit", "Bash(sudo *)"],
        "work": "worktree",
    },

    # -- tests ---------------------------------------------------------
    "test-writer": {
        "summary": "writes tests, not fixes",
        "allow": ["Read", "Glob", "Grep", "Write", "Edit", "Bash(git:*)"],
        "deny": ["Bash(sudo *)"],
        "work": "worktree",
    },
    "tester": {
        "summary": "runs it and tries to break it; changes no source",
        "allow": ["Read", "Glob", "Grep", "Bash"],
        "deny": ["Write", "Edit", "Bash(sudo *)"],
        "work": "worktree",
    },

    # -- hold a factory ------------------------------------------------
    "manager": {
        "summary": "decides what is worked on and by whom; writes no code",
        "allow": ["Read", "Glob", "Grep", "Write", "TodoWrite"],
        "deny": ["Edit", "Bash(sudo *)"],
        "work": "worktree",
        "factory": {"containers": 3,
                    "child_rights": ["send", "inspect", "read_output"]},
    },
    "tech-lead": {
        "summary": "accountable for the code, and writes it; can steer its agents",
        "allow": ["Read", "Glob", "Grep", "Write", "Edit", "Bash(git:*)"],
        "deny": ["Bash(sudo *)"],
        "work": "worktree",
        "factory": {"containers": 3,
                    "child_rights": ["send", "inspect", "read_output",
                                     "write_input", "signal"]},
    },
    "orchestrator": {
        "summary": "splits the task; may spawn another orchestrator for a big piece",
        "allow": ["Read", "Glob", "Grep", "Write", "TodoWrite"],
        "deny": ["Edit", "Bash(sudo *)"],
        "work": "worktree",
        # Generous child_rights on purpose. A child's factory may only hand on
        # rights contained in its parent's, so this list is the ceiling for the
        # whole tree below this agent -- a sub-orchestrator cannot give its own
        # children anything that is not in here, and its quota is capped at
        # whatever remains of this one. Recursion is bounded by the kernel, not
        # by the orchestrator behaving itself.
        "factory": {"containers": 4,
                    "child_rights": ["send", "inspect", "read_output",
                                     "write_input", "signal"]},
    },
}

PERSONAS = sorted(p.stem for p in (HERE / "personas").glob("*.md"))

TEMPLATE = """\
# {role} \u00b7 {persona}
#
# {summary}
#
# Generated by compose.py -- edit the role and persona prompts, not this file.
#
#   capwrap up examples/roles-and-personas/built/{fname}.toml

name = "{fname}"

[runtime]
agent = "{agent}"
# Role and persona are one system prompt: it survives compaction, and the
# agent cannot talk itself out of either halfway through a session.  capwrap
# binds the file and delivers it natively for whichever agent this runs.
role_prompt = "{fname}.md"
cwd       = "/work"
tty       = true
approvals = "capwrap"

auto_allow = ["Read", "Glob", "Grep", "TodoWrite"]
auto_deny  = ["Bash(sudo *)"]

env_from_host = [{env}]

# The role's capability table. This is the half of the role that is
# *enforced*; the persona above is a disposition and enforces nothing.
[runtime.permissions]
allow = [{allow}]
ask   = ["WebFetch"]
deny  = [{deny}]

[sandbox]
network  = {network}
hostname = "{fname}"
unshare  = ["pid", "ipc", "uts", "cgroup"]

{mounts}{work}{files}[caps]
parent = ["send"]
{caps}"""

#: What each agent needs to run: the mounts that bring its binary and config
#: into the sandbox, and the host env vars that carry its credentials.  The
#: role table above is agent-agnostic -- this is the only per-agent part, and
#: `--agent` switches it.
AGENT_SETUP: dict[str, dict] = {
    "claude": {
        "mounts": [
            ("~/.local/bin/claude", "/opt/claude/claude", "ro"),
            ("~/.claude", "/home/agent/.claude", "copy"),
        ],
        "env": ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"],
        "files": True,   # house rules land at /work/CLAUDE.md
    },
    "opencode": {
        "mounts": [
            ("~/.opencode/bin", "/opt/opencode", "ro"),
            ("~/.config/opencode", "/home/agent/.config/opencode", "copy"),
            ("~/.local/share/opencode", "/home/agent/.local/share/opencode", "rw"),
        ],
        "env": ["OPENCODE_API_KEY"],
        "files": False,
    },
    "opencode2": {
        "mounts": [
            ("~/.opencode/bin", "/opt/opencode", "ro"),
            ("~/.config/opencode2", "/home/agent/.config/opencode2", "copy"),
            ("~/.local/share/opencode", "/home/agent/.local/share/opencode", "rw"),
        ],
        "env": ["OPENCODE_API_KEY"],
        "files": False,
    },
    "pi": {
        "mounts": [
            ("~/.local/lib/node_modules/@earendil-works/pi-coding-agent",
             "/opt/pi/pi-agent", "ro"),
            ("~/.pi/agent", "/home/agent/.pi/agent", "copy"),
        ],
        "env": ["OPENCODE_API_KEY"],
        "files": False,
    },
}

WORKTREE = """
[[mounts]]
src        = "~/capwrap-demo/repo"
dest       = "/work"
mode       = "worktree"
branch     = "capwrap/{name}"
base       = "main"
on_destroy = "keep"
"""

NO_REPO = """
# No repository. This role's judgement is worth something *because* it is not
# based on reading the source.
[[mounts]]
dest = "/work"
mode = "tmpfs"
size = "64m"
"""

FACTORY = """
[caps.factory]
rights       = ["create"]
quota        = {{ containers = {containers} }}
child_rights = [{child_rights}]
"""


def quote(items: list[str]) -> str:
    return ", ".join(f'"{item}"' for item in items)


def compose(role: str, persona: str, agent: str = "claude") -> pathlib.Path:
    if role not in ROLES:
        sys.exit(f"unknown role {role!r}; try --list")
    if persona not in PERSONAS:
        sys.exit(f"unknown persona {persona!r}; try --list")
    if agent not in AGENT_SETUP:
        sys.exit(f"unknown agent {agent!r}; try --list")

    spec = ROLES[role]
    setup = AGENT_SETUP[agent]
    # The agent is part of the generated name so the same role-persona pair
    # can be built for several agents without the worktree branches (which
    # carry the name) colliding.
    fname = name = f"{role}-{persona}" if agent == "claude" else f"{agent}-{role}-{persona}"
    BUILT.mkdir(exist_ok=True)

    # One prompt file, both halves, clearly separated. capwrap binds it at
    # /run/capwrap-role.md and delivers it natively for the agent -- claude
    # gets a system-prompt flag, opencode an instructions entry, pi a flag --
    # so the role and persona texts stay single copies editable on their own.
    prompt = (
        (HERE / "roles" / f"{role}.md").read_text().rstrip()
        + "\n\n---\n\n"
        + (HERE / "personas" / f"{persona}.md").read_text().rstrip()
        + "\n\n---\n\n"
        + "Your role is the job you are accountable for and is enforced by the\n"
          "capabilities you hold. Your persona is how you go about it. Where they\n"
          "pull against each other, the role wins: a reviewer with a lazy\n"
          "disposition still reviews, it just does not gold-plate the write-up.\n"
    )
    (BUILT / f"{fname}.md").write_text(prompt)
    # Copied rather than referenced with `..`, so that `built/` can be moved or
    # shipped somewhere on its own and still work.
    (BUILT / "house.md").write_text((HERE / "house.md").read_text())

    mounts = "".join(
        f"\n[[mounts]]\nsrc  = \"{src}\"\ndest = \"{dest}\"\nmode = \"{mode}\"\n"
        for src, dest, mode in setup["mounts"]
    )
    files = (
        "\n[[files]]\ndest = \"/work/CLAUDE.md\"\nsrc  = \"house.md\"\n\n"
        if setup["files"] else ""
    )

    factory = spec.get("factory")
    config = TEMPLATE.format(
        role=role, persona=persona, fname=fname, agent=agent,
        summary=spec["summary"],
        env=quote(setup["env"]),
        allow=quote(spec["allow"]), deny=quote(spec["deny"]),
        network=str(spec.get("network", True)).lower(),
        mounts=mounts, files=files,
        work=(WORKTREE.format(name=name) if spec["work"] == "worktree" else NO_REPO),
        caps=(FACTORY.format(containers=factory["containers"],
                             child_rights=quote(factory["child_rights"]))
              if factory else ""),
    )
    path = BUILT / f"{fname}.toml"
    path.write_text(config)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a capwrap config for a role crossed with a persona.",
        epilog="example: ./compose.py architect:idealist architect:pragmatist",
    )
    parser.add_argument("pairs", nargs="*", metavar="ROLE:PERSONA")
    parser.add_argument("--list", action="store_true",
                        help="show every role and persona")
    parser.add_argument("--all-personas", metavar="ROLE",
                        help="build one role against every persona")
    parser.add_argument("--agent", default="claude", choices=sorted(AGENT_SETUP),
                        help="which agent the generated configs run (default: claude)")
    args = parser.parse_args()

    if args.list:
        print(f"roles ({len(ROLES)}):")
        width = max(len(r) for r in ROLES)
        for role, spec in ROLES.items():
            mark = " [factory]" if spec.get("factory") else ""
            print(f"  {role:<{width}}  {spec['summary']}{mark}")
        print(f"\npersonas ({len(PERSONAS)}):")
        for persona in PERSONAS:
            print(f"  {persona}")
        print(f"\n{len(ROLES) * len(PERSONAS)} combinations.")
        return 0

    pairs = [p.split(":", 1) for p in args.pairs if ":" in p]
    if args.all_personas:
        pairs += [[args.all_personas, p] for p in PERSONAS]
    if not pairs:
        parser.error("give at least one ROLE:PERSONA, or --list")

    for role, persona in pairs:
        path = compose(role, persona, args.agent)
        print(f"built {path.relative_to(HERE.parent.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
