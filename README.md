# capwrap

Run several AI coding agents at once, on the same repo and the same data, without
them coordinating — and without giving any of them more authority than you meant to.

Two ideas, stacked:

**Filesystem isolation removes the need to coordinate.** Each agent gets an
*overlay* over shared data (it sees everything, its writes are private) or a
*git worktree* on its own branch (integration is an ordinary `git merge`). No
locking, no clobbering, no turn-taking.

**A capability system decides who may do what.** Modelled on Fiasco.OC/L4Re: a
host daemon holds all authority, each container gets only unforgeable local
capabilities, and rights can only ever *shrink* when delegated. One place to
revoke, and revocation is recursive.

On top of that, one web console: every agent's live terminal, the parent tree,
the capability graph, and a single approval queue so five agents don't mean five
windows to babysit.

---

## Quick start

```bash
scripts/install-apparmor-profile.sh     # once, needs sudo — see "Host setup"
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'

.venv/bin/capwrap doctor                # check the host can sandbox
examples/setup-demo.sh                  # build a playground repo + database

.venv/bin/capwrap up examples/agents/dev-a.toml examples/agents/dev-b.toml
# → http://127.0.0.1:8420
```

Two agents now share one repo and one database and cannot see each other's work:

```bash
ls ~/capwrap-demo/db                        # seed data, untouched
git -C ~/capwrap-demo/repo branch           # capwrap/dev-a, capwrap/dev-b, main
```

---

## Host setup

capwrap needs `bubblewrap`. Via nix:

```nix
# ~/.config/home-manager/home.nix
home.packages = with pkgs; [ bubblewrap fuse-overlayfs git ];
```

**On Ubuntu, a nix-installed bwrap does not work out of the box.** Ubuntu sets
`kernel.apparmor_restrict_unprivileged_userns=1`, which forces any *unconfined*
program creating a user namespace into the `unprivileged_userns` AppArmor
profile — and that profile denies capabilities inside the namespace, so bwrap
fails at its first step:

```
bwrap: setting up uid map: Permission denied
```

Ubuntu ships an exemption for bubblewrap, but it attaches by path to
`/usr/bin/bwrap` only. `scripts/install-apparmor-profile.sh` installs the same
policy attached to the nix store path, globbed so it survives nixpkgs updates:

```bash
sudo scripts/install-apparmor-profile.sh
```

`capwrap doctor` diagnoses this precisely rather than leaving you with the
message above. Alternatives, if you prefer: `apt install bubblewrap`, or
disabling the restriction system-wide (weakens the host — not recommended for a
project about sandboxing).

A consequence worth knowing: the stacked profile denies capabilities to bwrap's
*children*, so **nested bwrap inside a sandbox cannot work**. Agents never create
sandboxes directly — they invoke a factory capability and the daemon does it.
That is the right design anyway.

---

## Container configs

```toml
name = "dev-a"

[runtime]
command = ["claude"]
cwd     = "/work"
approvals = "capwrap"                    # route tool prompts to the web console
auto_allow = ["Read", "Bash(git log*)"]  # things not worth waking a human for

[sandbox]
network = false                          # default
unshare = ["pid", "ipc", "uts", "cgroup"]

[[mounts]]                               # a repo → private branch + checkout
src    = "~/proj"
dest   = "/work"
mode   = "worktree"
branch = "capwrap/dev-a"

[[mounts]]                               # shared data → private writes
src  = "~/db"
dest = "/db"
mode = "overlay"

[[files]]                                # inject scaffolding
dest    = "/work/ROLE.md"
content = "You are agent A."

[caps]                                   # initial authority — this is all it gets
peers      = [{ container = "dev-b", rights = ["send", "inspect"] }]
dataspaces = [{ path = "~/ref", rights = ["read", "map", "delegate"] }]
factory    = { rights = ["create"], quota = { containers = 2 } }
```

### Mount modes

| mode | what the agent gets | writes go to |
|---|---|---|
| `ro` | read-only view | nowhere |
| `rw` | the real directory | the host, immediately |
| `tmpfs` | empty scratch | discarded with the container |
| `copy` | a private copy | its own copy |
| `overlay` | shared contents | a private upper dir |
| `worktree` | its own branch + checkout | its own branch |

`overlay` is right for data; `worktree` is right for a git repo, because you
integrate with `git merge` instead of diffing overlay upper directories by hand.
`share = "none"` uses `git clone --local` instead of a linked worktree when you
don't want agents sharing an object store.

### Nesting

Modes can be nested to any depth — a read-only tree with a writable overlay part
way down and a private copy below that:

```toml
[[mounts]]
src = "~/proj"; dest = "/a"; mode = "ro"          # visible, immutable
[[mounts]]
src = "~/proj/b"; dest = "/a/b"; mode = "overlay" # writes to a private upper
[[mounts]]
src = "~/proj/b/c"; dest = "/a/b/c"; mode = "copy"  # a private copy
```

Deeper mounts shadow shallower ones, so `/a` stays read-only while `/a/b` and
`/a/b/c` are writable, each into its own place. **Declaration order doesn't
matter**: bwrap applies mounts in argv order, so capwrap sorts them parents-first
before emitting. Without that, writing the child mount first would silently give
you a read-only `/a/b/c` with no error anywhere.

One constraint comes from bwrap: **the mountpoint must already exist in the
parent mount's source.** Mounting at `/a/b` when the host's `~/proj` has no `b/`
fails with `Can't mkdir: Read-only file system`, because bwrap cannot create a
directory inside a read-only bind. Create the directory on the host first, or
make the parent `rw`/`overlay` rather than `ro`.

---

## The capability model

Every container has a **capability table** mapping small integers — *slots* — to
objects. Agent A's slot 3 and agent B's slot 3 are unrelated, and neither can
name an object it wasn't given. There is no by-name variant of any operation, so
there is nothing to enumerate or guess.

```
$ capctl caps
SLOT  KIND       LABEL       RIGHTS
1     container  self        delegate,inspect,kill,read_output,send,signal
2     gate       operator    inspect,send
3     dataspace  /ref        delegate,map,read
4     container  peer:dev-b  inspect,send
```

**Rights only shrink.** Delegation goes through a mapping database that refuses
to pass on rights the delegator doesn't hold. This applies to spawning too: a
container created through a factory gets capabilities *derived from its
spawner's*, so a factory can never amplify authority.

**Revocation is recursive.** Revoking a capability kills its entire delegation
subtree — including things the recipient passed to agents you've never heard of.
Without that, "revoke" would be a lie after the first hop.

Agents use `capctl` inside the sandbox:

```bash
capctl caps                        # what am I allowed to do?
capctl send peer:dev-b "build ok"  # by label or by slot
capctl recv --wait                 # read my mailbox
capctl grant peer:dev-b 3 --rights read     # delegate, diminished
capctl revoke 3                    # take back everything I gave from slot 3
capctl map peer:dev-b 3 findings   # put a dataspace in their /shared
capctl spawn factory child.toml    # create a child, within quota
capctl ask "may I install curl?"   # ask the human, and block for an answer
```

### Identity

Each container's socket is bound at `/run/capwrap.sock` inside its sandbox and
at `<state>/containers/<name>/agent.sock` on the host. **The daemon knows who is
calling because it knows which socket accepted the connection.** No request
carries an identity, so there is no token to steal, forge or replay.

---

## Approvals

With `approvals = "capwrap"`, a Claude Code `PreToolUse` hook diverts every
permission prompt to the operator's queue in the web UI. The agent blocks inside
the hook until you answer, and your answer resolves the very future it is
waiting on — one process, one event loop, no polling.

`auto_allow` / `auto_deny` decide the boring cases locally, matching either a
bare tool name (`"Read"`) or `Tool(glob)` against its main argument
(`"Bash(git *)"`). The policy file is bound read-only, because an agent with a
shell would otherwise just widen its own permissions.

If the daemon is unreachable the hook returns `ask`, falling back to Claude's own
prompt. It never defaults to `allow` — that would silently disable every check.

### Trying it

```bash
examples/setup-demo.sh
capwrap up examples/agents/claude-approval-demo.toml
# open http://127.0.0.1:8420
```

Claude is asked to create a file. `Read`/`Glob`/`Grep` are auto-allowed, so
within a few seconds exactly one thing reaches your queue:

```
claude-approve
Write: /work/hello.txt
{"tool": "Write", "input": {"file_path": "/work/hello.txt", "content": "hello"}}
[Allow] [Deny] [Open]
```

**Allow** → the file appears on branch `capwrap/claude-approve` and Claude
replies `DONE`. **Deny** → the file is never created and Claude tells you it was
blocked. Either way the decision is in the audit log next to the request.

## Agents discover capctl on their own

Every container gets a Claude Code skill at
`$HOME/.claude/skills/capwrap/SKILL.md` describing the capability model and the
`capctl` commands, so an agent finds out how to message a peer or ask you a
question without it being repeated in every prompt. Turn it off with
`[runtime] capctl_skill = false`.

## Granting authority while things are running

Capabilities are not frozen at spawn. In the **Capabilities** tab, select a
container and use **Grant a capability…** to hand it authority over another
container, picking the exact rights. The operator holds a root capability on
everything, so this is an ordinary delegation through the kernel — audited like
any other, and revocable (recursively) from the same table.

The same thing over HTTP:

```bash
curl -X POST localhost:8420/api/caps/grant -H 'Content-Type: application/json' \
  -d '{"holder":"dev-a","target_container":"dev-b","rights":["send","inspect","kill"]}'
# -> {"holder":"dev-a","slot":5,"rights":[...],"label":"peer:dev-b#2"}
```

A second grant on the same container gets a distinct label (`peer:dev-b#2`), so
`capctl kill peer:dev-b#2` stays unambiguous.

## Killing a container cleanly

`capwrap stop`, the web UI's **Stop**, `capctl kill`, and a daemon crash all end
with **nothing left running**. The guarantee comes from the PID namespace rather
than from signalling: bwrap is pid 1 inside it, and when pid 1 exits the kernel
SIGKILLs every remaining process in the namespace. That covers processes which
`setsid` themselves out of the container's process group, which plain
`kill(-pgid)` would miss.

Because the guarantee depends on it, `sandbox.unshare` must include `pid`; a
config that leaves it out is rejected rather than silently leaking processes.
`--die-with-parent` extends the same property to a daemon crash — `kill -9` on
the daemon tears the namespace down too. Both cases are covered by tests.

To also remove a container's host-side state (overlay upper dirs, copies,
worktree):

```bash
capwrap clean <name> --yes
```

---

## Reaching the console from another machine

`capwrap up` binds `127.0.0.1:8420` by default. `--host` changes that:

```bash
capwrap up agents/*.toml --host 0.0.0.0            # every interface
capwrap up agents/*.toml --host 100.73.186.227     # a VPN address only
```

**The web interface has no authentication.** Anyone who can reach the port can
read every agent's terminal, type into it, spawn and destroy containers, and
grant capabilities — and a container that carries your Anthropic credentials
makes that a credential leak as well as a shell. `--host 0.0.0.0` on an untrusted
network hands all of that to the network.

Two safe ways to use it from a laptop, in order of preference:

```bash
# 1. Bind to a VPN address only. Nothing is exposed to the local network.
capwrap up agents/*.toml --host <your-tailscale-ip>

# 2. Keep it on loopback and tunnel in over SSH.
ssh -N -L 8420:127.0.0.1:8420 pi.local     # from the laptop; then use localhost:8420
```

Binding to anything other than loopback prints a warning saying as much.

## Commands

```
capwrap doctor                  check the host, with specific fixes
capwrap show <config>           validate a config and show what it resolves to
capwrap run <config>            prepare and enter one container, no daemon
capwrap run <config> --dry-run  print the bwrap command it would run
capwrap up <configs...>         run containers under the daemon + web UI
capwrap clean <name> --yes      delete a container's host-side state
capwrap state                   where state lives, and what's in it
```

---

## Layout

```
capwrap/
  config.py        TOML → validated ContainerConfig
  daemon.py        containers, PTYs, sockets; implements the kernel's effects
  kernel/          rights · captable · mapdb · objects · audit · kernel
  runtime/         probe · fsprep · gitwt · bwrap · supervisor
  ipc/             protocol · per-container socket server · mailboxes
  guest/           capctl, hook.py — the only things an agent sees
  web/             FastAPI + a vanilla-JS console, xterm.js vendored locally
tests/             81 tests; `-m sandbox` ones need a working bwrap
```

The split that matters: `kernel/` decides what is permitted and performs no I/O;
`daemon.py` performs the effects. So the whole permission model is testable
without a sandbox, a daemon or a filesystem — and every decision passes one
choke point, which is what makes the audit log trustworthy.

```bash
.venv/bin/pytest                  # everything
.venv/bin/pytest -m sandbox       # only the ones that really launch containers
```

---

## Status

Working: all mount modes, the capability kernel with recursive revocation,
per-container IPC, agent-to-agent messaging, dataspace mapping, factories with
quotas, the web console, and approval routing.

Not yet: **live remapping into a running container.** `capctl map` currently
materialises into the target's `/shared`, which is a host directory already bound
into the sandbox, so it appears immediately. True dynamic bind-mounting (the
daemon entering the target's user and mount namespaces via `nsenter`, where it
holds `CAP_SYS_ADMIN`) slots in behind the same `materialise`/`unmaterialise`
interface in `daemon.py`.
