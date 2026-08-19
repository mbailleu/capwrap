# A team of role-specialised agents

Seven roles, each a container: orchestrator, explorer, programmer, tester,
reviewer, writer, architect.

```bash
examples/setup-demo.sh                  # or point the mounts at your own repo
export ANTHROPIC_AUTH_TOKEN=sk-ant-...  # or use env_file
capwrap up examples/team/*.toml
```

Each gets its own git branch (`capwrap/<role>`), so they work simultaneously
without coordinating, and you integrate with `git merge`.

## How a prompt reaches an agent

Four mechanisms, three of which these configs use:

| where | how | survives compaction? | best for |
|---|---|---|---|
| **system prompt** | `--append-system-prompt-file /prompts/<role>.md` | yes | who the agent *is* |
| **project memory** | a `CLAUDE.md` in the worktree | yes, re-read | house rules, conventions |
| **injected files** | `[[files]]` / a `ro` mount | it is just a file | reference material |
| **first message** | `claude -p "..."` in `runtime.command` | no | the task, not the role |

The role goes in the **system prompt** deliberately. A role stated in the first
user message is one compaction away from being forgotten, and an agent can talk
itself out of it; a system prompt is neither.

`prompts/` is mounted read-only into every container at `/prompts`, so all seven
share one copy of `house.md` — edit it once and every agent gets it.

`prompts/house.md` is *also* bound in as each worktree's `CLAUDE.md`, which
Claude reads by itself without any flag.

## Roles are enforced, not just described

Telling an agent "you are a reviewer, do not edit" is a request. These configs
make it true:

```toml
[runtime.permissions]           # reviewer
deny = ["Write", "Edit", "Bash(sudo *)"]
```

The reviewer *cannot* write, so its review can be trusted not to have changed
anything. Likewise the explorer is read-only, the architect may `Write` (design
notes) but not `Edit`, and only the orchestrator holds a factory:

```toml
[caps.factory]                  # orchestrator only
quota        = { containers = 3 }
child_rights = ["send", "inspect", "read_output", "write_input"]
```

`child_rights` is what makes an orchestrator work: it gets a capability on each
agent it spawns, so it can watch (`capctl screen`) and steer (`capctl keys`)
them. The ceiling is set here, in the config *you* write, not by the
orchestrator.

## Adapting it

- Point the `/work` mount at your own repo.
- Add a role: drop `prompts/<name>.md` next to the others and copy a config.
- Give an agent something to read: another `mode = "ro"` mount.
- Tighten a role: shrink its `[runtime.permissions]` and its `[caps] peers`.
