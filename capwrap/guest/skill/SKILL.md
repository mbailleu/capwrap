---
name: capwrap
description: Talk to other agents and to the human operator from inside a capwrap container. Use when you need to message another agent, share files with one, ask the operator for permission or a decision, check what you are allowed to do, or start a helper container. Triggers on "message the other agent", "ask the operator", "what am I allowed to do", "share this with", "spawn a helper".
---

# Working inside a capwrap container

You are running in a sandbox. You cannot see the host filesystem, other agents'
files, or anything you were not explicitly given. Everything you *can* do
outside your own filesystem goes through one command: `capctl`.

## What you hold

```
capctl caps
```

Lists your **capabilities**. Each is a numbered slot with a label and a set of
rights:

```
SLOT  KIND       LABEL       RIGHTS
1     container  self        delegate,inspect,kill,read_output,send,signal
2     gate       operator    inspect,send
3     dataspace  /ref        delegate,map,read
4     container  peer:dev-b  inspect,send
```

You can refer to a capability by slot number or by label — `capctl send 4 "hi"`
and `capctl send peer:dev-b "hi"` are the same thing. **You cannot name anything
you do not hold a capability for.** If a command fails with `no_such_cap`, you
were not given that authority; do not try to work around it, ask the operator.

## Asking the human

```
capctl ask "May I add a dependency on requests?"
```

Blocks until the operator answers in their console, then exits 0 for allow and
non-zero for deny — so `capctl ask "..." && do-the-thing` does the right thing.
Use this whenever you are about to do something you are not sure is wanted.
The operator sees every agent's questions in one queue, so asking is cheap.

## Talking to other agents

```
capctl send peer:dev-b "I've pushed the parser fix to capwrap/dev-a"
capctl recv                  # read your mailbox
capctl recv --wait           # block until something arrives
```

Messages also appear as files in `/shared/inbox/`, so check there if you were
not watching. Needs `send` on that peer.

## Sharing files with another agent

```
capctl map peer:dev-b 3 findings --mode copy
```

Puts the dataspace in slot 3 into that agent's `/shared/findings`. `--mode copy`
duplicates the bytes and needs the `copy` right; `--mode map` aliases them and
needs the stronger `map` right.

## Passing on authority

```
capctl grant peer:dev-b 3 --rights read
```

Gives another agent one of your own capabilities. **You can only ever give away
rights you hold, and only if your capability has `delegate`.** Prefer granting
the smallest useful subset.

```
capctl revoke 3
```

Takes back everything you granted from slot 3 — recursively, including anything
the recipient passed on further. Add `--self-too` to drop your own copy as well.

## Starting a helper

```
capctl spawn factory /shared/child-config.json
```

Only if you hold a `factory` capability, and only within its quota. The child
starts with **no more authority than you have**, and only what its config asks
for and you can actually grant.

## Etiquette

- Check `capctl caps` before assuming you can do something.
- Ask the operator rather than guessing, and rather than working around a denial.
- When you finish a piece of work, tell the agents who depend on it.
- Your git branch is yours alone; commit freely, nobody else sees your worktree.
