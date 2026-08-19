# Networking: what exists, and what filtering would take

## What exists

One boolean, `sandbox.network`, which maps directly onto a bwrap flag:

| setting | flag | result |
|---|---|---|
| `false` (default) | `--unshare-net` | its own network namespace, loopback only — no route anywhere |
| `true` | *(none)* | **shares the host's network namespace** |

There is no middle ground. `true` is not "network access", it is "the host's
network stack", with everything that implies: the LAN, the internet, any VPN the
host is on, and every service bound to the host's loopback.

## The immediate problem

Since the container shares the host netns, `127.0.0.1:8420` inside it is the
capwrap web console, which has no authentication. Measured:

```
container without any capability on dev-a:
  POST /api/caps/grant   -> granted itself inspect,kill,send,write_input on dev-a
  POST /api/containers/dev-a/input -> typed into dev-a's terminal
```

So `network = true` is a full bypass of the capability system. The capability
kernel governs the `/run/capwrap.sock` channel; it does not govern a TCP socket
that happens to reach the same daemon. Worth fixing regardless of filtering —
authentication on the console, or binding it to a Unix socket that no container
can see, closes it.

## Can access be limited to specific IPs or names?

Yes, several ways, with quite different costs. None is implemented.

### 1. No network namespace access at all + a brokered socket

Keep `--unshare-net`, and hand the container a Unix socket, bind-mounted in,
that the daemon serves. The container has no IP stack, so there is nothing to
filter — the daemon *is* the only egress, and it decides what it will talk to
and logs every request.

For Claude specifically this fits the tooling: the binary reads
`ANTHROPIC_UNIX_SOCKET`, so the agent can be pointed at a socket rather than a
host and never needs an IP stack. For general tools (git, pip, curl) it needs a
small relay inside the container listening on its own loopback and forwarding
over the socket, plus `HTTPS_PROXY` pointing at it.

Strongest isolation, no privilege, no new host dependencies. Most work, and the
allow-list is expressed in the daemon rather than in the kernel.

### 2. User-mode networking with `slirp4netns` or `pasta`

Give each container its own network namespace and attach a user-mode TCP/IP
stack running as an ordinary process. This is how rootless Podman works. Both
are in nixpkgs (`slirp4netns` 1.3.4, `passt` 2026_07_16).

`--disable-host-loopback` alone closes the escalation above. Filtering is
coarser than nftables but real, and `pasta` can restrict which ports and
addresses are forwarded.

Costs one extra process per container and a real dependency. This is the
conventional answer and probably the right one if the goal is "each agent gets
metered internet".

### 3. veth pair + nftables per container

A virtual interface into the container's netns, filtered on the host with
`nft` — proper allow-lists by address, port, and (with `nftables` sets updated
from DNS answers) by name.

Strongest and most conventional filtering, but creating a veth and writing host
firewall rules both need `CAP_NET_ADMIN` on the host, so the daemon has to be
privileged. Note that the dynamic-mapping work already wants
`CAP_SYS_ADMIN`, so a privileged daemon may be on the cards anyway.

### 4. DNS-only filtering

Bind-mount a `resolv.conf` pointing at a resolver the daemon runs, and answer
only for allowed names. Cheap and easy, and worth almost nothing on its own: it
stops name resolution, not connections, so anything with a literal IP walks
straight past it. Only useful layered on one of the above.

## A note on "DNS names"

Filtering *by name* is inherently approximate. Names resolve to addresses that
change, several names share an address, and TLS SNI is the only in-band hint —
which an agent controls. Anything name-based is either a proxy that terminates
the connection and inspects the request (option 1), or an address allow-list
kept in step with DNS answers (option 3). Option 1 is the only one that can
honestly say "this agent may reach api.anthropic.com and nothing else".

## Suggested order

1. Authenticate the console, or move it off a TCP port the containers can see.
   This is a bug fix, not a feature, and it is independent of everything else.
2. Option 1 for Claude agents, since `ANTHROPIC_UNIX_SOCKET` makes it cheap and
   it gives the strongest guarantee.
3. Option 2 when agents need general internet access with limits.
4. Option 3 only if the daemon becomes privileged for other reasons.
