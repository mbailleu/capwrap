"""The daemon: IPC attribution, message delivery, and live containers.

The first test here is the important one.  Everything else in the capability
model rests on the daemon knowing *who is calling*, and it establishes that from
the socket a connection arrived on rather than from anything the caller says.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from capwrap.config import load_config_data
from capwrap.daemon import Daemon
from capwrap.ipc.protocol import Request, Response


@pytest.fixture
async def daemon(state_dir):
    d = Daemon(audit_path=Path(state_dir) / "audit.db")
    yield d
    await d.shutdown()


def config(name: str, base_dir: Path, **extra):
    return load_config_data({"name": name, **extra}, base_dir=base_dir)


async def request(socket_path: Path, op: str, args: dict | None = None) -> Response:
    """Talk to a container's control socket the way capctl does."""
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        writer.write(Request(op=op, args=args or {}, id=1).encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=10)
        return Response.parse(line)
    finally:
        writer.close()


# ==========================================================================
# identity
# ==========================================================================


async def test_identity_comes_from_the_socket_not_the_request(daemon, tmp_path):
    """An agent cannot claim to be another container, because it never claims at all.

    Both containers run the same request with the same bytes on the wire; the
    answers differ purely because the sockets differ.
    """
    for name in ("alpha", "beta"):
        daemon.register(config(name, tmp_path))
        container = daemon.containers[name]
        container.server = await daemon._serve_container(container)

    alpha = await request(daemon.containers["alpha"].paths.socket, "whoami")
    beta = await request(daemon.containers["beta"].paths.socket, "whoami")

    assert alpha.result["container"] == "alpha"
    assert beta.result["container"] == "beta"


async def test_a_request_cannot_smuggle_in_an_actor(daemon, tmp_path):
    daemon.register(config("alpha", tmp_path))
    container = daemon.containers["alpha"]
    container.server = await daemon._serve_container(container)

    # There is no field for it, and adding one changes nothing.
    reader, writer = await asyncio.open_unix_connection(str(container.paths.socket))
    writer.write(
        json.dumps({"id": 1, "op": "whoami", "actor": "root", "container": "beta"}).encode()
        + b"\n"
    )
    await writer.drain()
    reply = Response.parse(await asyncio.wait_for(reader.readuntil(b"\n"), timeout=10))
    writer.close()

    assert reply.result["container"] == "alpha"


async def test_unknown_operations_are_refused(daemon, tmp_path):
    daemon.register(config("alpha", tmp_path))
    container = daemon.containers["alpha"]
    container.server = await daemon._serve_container(container)

    reply = await request(container.paths.socket, "kernel.destroy_container")
    assert not reply.ok
    assert reply.code == "protocol_error"


async def test_malformed_json_does_not_kill_the_connection(daemon, tmp_path):
    daemon.register(config("alpha", tmp_path))
    container = daemon.containers["alpha"]
    container.server = await daemon._serve_container(container)

    reader, writer = await asyncio.open_unix_connection(str(container.paths.socket))
    writer.write(b"{not json\n")
    await writer.drain()
    bad = Response.parse(await asyncio.wait_for(reader.readuntil(b"\n"), timeout=10))
    assert not bad.ok and bad.code == "protocol_error"

    writer.write(Request(op="whoami", id=2).encode())
    await writer.drain()
    good = Response.parse(await asyncio.wait_for(reader.readuntil(b"\n"), timeout=10))
    writer.close()
    assert good.ok, "one bad request should not poison the session"


async def test_internal_errors_do_not_leak_details_to_an_agent(daemon, tmp_path):
    daemon.register(config("alpha", tmp_path))
    container = daemon.containers["alpha"]
    container.server = await daemon._serve_container(container)

    # Missing required argument -> a KeyError inside the daemon.
    reply = await request(container.paths.socket, "cap.info", {})
    assert not reply.ok
    assert reply.code == "internal_error"
    assert "KeyError" not in (reply.message or "")


# ==========================================================================
# capability operations over the wire
# ==========================================================================


async def test_caps_are_listed_without_exposing_object_ids(daemon, tmp_path):
    daemon.register(config("alpha", tmp_path))
    container = daemon.containers["alpha"]
    container.server = await daemon._serve_container(container)

    reply = await request(container.paths.socket, "cap.list")
    assert reply.ok
    labels = {c["label"] for c in reply.result}
    assert labels == {"self", "operator"}
    for cap in reply.result:
        assert "oid" not in cap and "oid" not in cap["detail"]


async def test_messages_flow_between_containers(daemon, tmp_path):
    peers = {"caps": {"peers": [{"container": "beta", "rights": ["send"]}]}}
    daemon.register(config("alpha", tmp_path, **peers))
    daemon.register(config("beta", tmp_path))
    daemon.link_all_peers()

    for name in ("alpha", "beta"):
        c = daemon.containers[name]
        c.server = await daemon._serve_container(c)

    caps = (await request(daemon.containers["alpha"].paths.socket, "cap.list")).result
    peer_slot = [c["slot"] for c in caps if c["label"] == "peer:beta"][0]

    sent = await request(
        daemon.containers["alpha"].paths.socket,
        "msg.send", {"slot": peer_slot, "payload": "the build is green"},
    )
    assert sent.ok and sent.result["delivered_to"] == "beta"

    got = await request(
        daemon.containers["beta"].paths.socket, "msg.recv", {"timeout": 0}
    )
    assert got.ok
    assert got.result[0]["from"] == "alpha"
    assert got.result[0]["payload"] == "the build is green"


async def test_sending_without_a_capability_is_denied_and_audited(daemon, tmp_path):
    daemon.register(config("alpha", tmp_path))
    daemon.register(config("beta", tmp_path))
    for name in ("alpha", "beta"):
        c = daemon.containers[name]
        c.server = await daemon._serve_container(c)

    reply = await request(
        daemon.containers["alpha"].paths.socket, "msg.send",
        {"slot": 77, "payload": "hello?"},
    )
    assert not reply.ok
    assert reply.code == "no_such_cap"

    denied = daemon.audit.tail(denied_only=True)
    assert any(d["actor"] == "alpha" and d["op"] == "msg.send" for d in denied)


async def test_messages_are_mirrored_into_shared_inbox(daemon, tmp_path):
    """An agent that never polls the socket still trips over its mail."""
    peers = {"caps": {"peers": [{"container": "beta", "rights": ["send"]}]}}
    daemon.register(config("alpha", tmp_path, **peers))
    daemon.register(config("beta", tmp_path, runtime={"notify": "file"}))
    daemon.link_all_peers()
    for name in ("alpha", "beta"):
        c = daemon.containers[name]
        c.paths.ensure()
        c.server = await daemon._serve_container(c)

    caps = (await request(daemon.containers["alpha"].paths.socket, "cap.list")).result
    slot = [c["slot"] for c in caps if c["label"] == "peer:beta"][0]
    await request(
        daemon.containers["alpha"].paths.socket,
        "msg.send", {"slot": slot, "payload": "check your inbox"},
    )

    inbox = daemon.containers["beta"].paths.shared / "inbox"
    files = list(inbox.glob("*.json"))
    assert files, "no inbox file was written"
    assert "check your inbox" in files[0].read_text()


async def test_delegation_over_the_wire_notifies_the_recipient(daemon, tmp_path):
    peers = {"caps": {"peers": [
        {"container": "beta", "rights": ["send", "inspect", "delegate"]},
    ]}}
    daemon.register(config("alpha", tmp_path, **peers))
    daemon.register(config("beta", tmp_path))
    daemon.link_all_peers()
    for name in ("alpha", "beta"):
        c = daemon.containers[name]
        c.server = await daemon._serve_container(c)

    caps = (await request(daemon.containers["alpha"].paths.socket, "cap.list")).result
    slot = [c["slot"] for c in caps if c["label"] == "peer:beta"][0]

    reply = await request(
        daemon.containers["alpha"].paths.socket, "cap.delegate",
        {"target_slot": slot, "cap_slot": slot, "rights": ["send"]},
    )
    assert reply.ok

    beta_caps = (await request(daemon.containers["beta"].paths.socket, "cap.list")).result
    handed = [c for c in beta_caps if c["slot"] == reply.result["slot"]][0]
    assert handed["rights"] == ["send"]

    mail = (await request(
        daemon.containers["beta"].paths.socket, "msg.recv", {"timeout": 0}
    )).result
    assert any(m["kind"] == "capability" for m in mail)


# ==========================================================================
# dataspace mapping
# ==========================================================================


async def test_mapping_a_dataspace_materialises_it_and_revoking_removes_it(
    daemon, tmp_path
):
    source = tmp_path / "notes"
    source.mkdir()
    (source / "finding.md").write_text("the bug is in the parser\n")

    alpha_caps = {"caps": {
        "peers": [{"container": "beta", "rights": ["send"]}],
        "dataspaces": [{"path": str(source), "rights": ["read", "copy", "delegate"]}],
    }}
    daemon.register(config("alpha", tmp_path, **alpha_caps))
    daemon.register(config("beta", tmp_path))
    daemon.link_all_peers()
    for name in ("alpha", "beta"):
        c = daemon.containers[name]
        c.paths.ensure()
        c.server = await daemon._serve_container(c)

    caps = (await request(daemon.containers["alpha"].paths.socket, "cap.list")).result
    peer_slot = [c["slot"] for c in caps if c["label"] == "peer:beta"][0]
    ds_slot = [c["slot"] for c in caps if c["kind"] == "dataspace"][0]

    reply = await request(
        daemon.containers["alpha"].paths.socket, "ds.map",
        {"target_slot": peer_slot, "ds_slot": ds_slot, "dest": "notes", "mode": "copy"},
    )
    assert reply.ok

    landed = daemon.containers["beta"].paths.shared / "notes" / "finding.md"
    assert landed.exists(), "the dataspace never reached beta's /shared"
    assert "parser" in landed.read_text()

    # Revoking the mapping takes the files back too, not just the authority.
    granted_slot = reply.result["slot"]
    node = daemon.kernel.tasks["beta"].slots[granted_slot].node
    killed = daemon.kernel.mapdb.revoke(node)
    daemon.kernel._apply_revocations(killed)
    assert not landed.parent.exists(), "revocation left the data behind"


async def test_map_mode_requires_the_stronger_right(daemon, tmp_path):
    source = tmp_path / "notes"
    source.mkdir()
    alpha_caps = {"caps": {
        "peers": [{"container": "beta", "rights": ["send"]}],
        # copy but not map
        "dataspaces": [{"path": str(source), "rights": ["read", "copy"]}],
    }}
    daemon.register(config("alpha", tmp_path, **alpha_caps))
    daemon.register(config("beta", tmp_path))
    daemon.link_all_peers()
    for name in ("alpha", "beta"):
        c = daemon.containers[name]
        c.paths.ensure()
        c.server = await daemon._serve_container(c)

    caps = (await request(daemon.containers["alpha"].paths.socket, "cap.list")).result
    peer_slot = [c["slot"] for c in caps if c["label"] == "peer:beta"][0]
    ds_slot = [c["slot"] for c in caps if c["kind"] == "dataspace"][0]

    reply = await request(
        daemon.containers["alpha"].paths.socket, "ds.map",
        {"target_slot": peer_slot, "ds_slot": ds_slot, "dest": "n", "mode": "map"},
    )
    assert not reply.ok and reply.code == "insufficient_rights"


# ==========================================================================
# operator approvals
# ==========================================================================


async def test_ask_blocks_until_the_operator_answers(daemon, tmp_path):
    daemon.register(config("alpha", tmp_path))
    c = daemon.containers["alpha"]
    c.server = await daemon._serve_container(c)

    asking = asyncio.ensure_future(
        request(c.paths.socket, "ask", {"question": "may I install curl?"})
    )
    await asyncio.sleep(0.05)

    pending = daemon.pending_approvals()
    assert len(pending) == 1
    assert pending[0]["question"] == "may I install curl?"
    assert pending[0]["container"] == "alpha"

    assert daemon.resolve_approval(pending[0]["id"], "allow", "go ahead")
    reply = await asyncio.wait_for(asking, timeout=5)
    assert reply.ok and reply.result["decision"] == "allow"


async def test_ask_times_out_rather_than_hanging_forever(daemon, tmp_path):
    daemon.register(config("alpha", tmp_path))
    c = daemon.containers["alpha"]
    c.server = await daemon._serve_container(c)

    reply = await request(
        c.paths.socket, "ask", {"question": "anyone there?", "timeout": 0.1}
    )
    assert reply.ok and reply.result["decision"] == "timeout"


# ==========================================================================
# live containers
# ==========================================================================


@pytest.mark.sandbox
async def test_a_running_container_can_use_capctl(daemon, tmp_path, require_sandbox):
    """The whole stack: sandbox, socket, guest CLI, kernel, mailbox."""
    peers = {"caps": {"peers": [{"container": "beta", "rights": ["send"]}]}}
    daemon.register(config(
        "alpha", tmp_path,
        runtime={"command": ["/bin/bash", "-c", "capctl caps && capctl whoami"],
                 "tty": True},
        **peers,
    ))
    daemon.register(config("beta", tmp_path))
    daemon.link_all_peers()

    await daemon.start("beta")
    container = await daemon.start("alpha")
    code = await asyncio.wait_for(container.session.wait(), timeout=60)

    output = container.session.scrollback().decode(errors="replace")
    assert code == 0, output
    assert "peer:beta" in output, output
    assert '"container": "alpha"' in output, output


@pytest.mark.sandbox
async def test_two_live_agents_message_each_other(daemon, tmp_path, require_sandbox):
    peers = {"caps": {"peers": [{"container": "beta", "rights": ["send"]}]}}
    daemon.register(config(
        "beta", tmp_path,
        runtime={"command": ["/bin/bash", "-c", "capctl recv --wait --timeout 30"]},
    ))
    daemon.register(config(
        "alpha", tmp_path,
        # Addressed by label rather than slot number -- the way an agent would.
        runtime={"command": ["/bin/bash", "-c",
                             "sleep 0.5; capctl send peer:beta 'hello from alpha'"]},
        **peers,
    ))
    daemon.link_all_peers()

    beta = await daemon.start("beta")
    alpha = await daemon.start("alpha")

    await asyncio.wait_for(alpha.session.wait(), timeout=60)
    await asyncio.wait_for(beta.session.wait(), timeout=60)

    received = beta.session.scrollback().decode(errors="replace")
    assert "hello from alpha" in received, received


# ==========================================================================
# the PreToolUse hook -- bundling permission prompts
# ==========================================================================

HOOK_EVENT = (
    '{{"hook_event_name":"PreToolUse","tool_name":"{tool}","tool_input":{input}}}'
)


def hook_command(tool: str, tool_input: str) -> list[str]:
    """Feed one PreToolUse event to the hook, the way Claude Code would."""
    event = HOOK_EVENT.format(tool=tool, input=tool_input)
    return ["/bin/bash", "-c", f"echo '{event}' | /opt/capwrap/hook.py"]


@pytest.mark.sandbox
async def test_hook_routes_a_tool_prompt_to_the_operator(
    daemon, tmp_path, require_sandbox
):
    """A blocked agent's prompt shows up in the operator's one queue.

    This is the feature that stops five agents meaning five terminals to watch:
    the agent is stuck inside the hook until someone answers here.
    """
    daemon.register(config(
        "hooked", tmp_path,
        runtime={
            "approvals": "capwrap",
            "auto_allow": ["Read"],
            "command": hook_command("Bash", '{"command":"rm -rf /work"}'),
        },
    ))
    container = await daemon.start("hooked")

    for _ in range(200):
        if daemon.pending_approvals():
            break
        await asyncio.sleep(0.05)

    pending = daemon.pending_approvals()
    assert pending, "the hook never reached the operator"
    assert pending[0]["container"] == "hooked"
    assert "rm -rf /work" in pending[0]["question"]
    assert pending[0]["context"]["tool"] == "Bash"

    daemon.resolve_approval(pending[0]["id"], "deny", "that would delete your branch")
    await asyncio.wait_for(container.session.wait(), timeout=30)

    verdict = container.session.scrollback().decode(errors="replace")
    assert '"permissionDecision": "deny"' in verdict, verdict
    assert "delete your branch" in verdict


@pytest.mark.sandbox
async def test_hook_auto_allows_without_troubling_the_operator(
    daemon, tmp_path, require_sandbox
):
    """Nobody wants to approve every Read; the policy decides those locally."""
    daemon.register(config(
        "quiet", tmp_path,
        runtime={
            "approvals": "capwrap",
            "auto_allow": ["Read"],
            "command": hook_command("Read", '{"file_path":"/work/a.py"}'),
        },
    ))
    container = await daemon.start("quiet")
    await asyncio.wait_for(container.session.wait(), timeout=30)

    output = container.session.scrollback().decode(errors="replace")
    assert '"permissionDecision": "allow"' in output, output
    assert not daemon.pending_approvals(), "a Read should never reach the human"


@pytest.mark.sandbox
async def test_hook_policy_is_read_only_to_the_agent(daemon, tmp_path, require_sandbox):
    """An agent must not be able to widen its own permissions.

    It has a shell, so if the policy file were writable the entire approval
    mechanism would be advisory.
    """
    daemon.register(config(
        "sneaky", tmp_path,
        runtime={
            "approvals": "capwrap",
            "auto_deny": ["Bash(sudo *)"],
            "command": ["/bin/bash", "-c",
                        "echo '{\"allow\":[\"*\"]}' > $CAPWRAP_POLICY 2>&1; "
                        "cat $CAPWRAP_POLICY"],
        },
    ))
    container = await daemon.start("sneaky")
    await asyncio.wait_for(container.session.wait(), timeout=30)

    output = container.session.scrollback().decode(errors="replace")
    assert "Read-only file system" in output or "Permission denied" in output, output
    assert '"allow": []' in output or "sudo" in output, output


# ==========================================================================
# teardown -- nothing may outlive its container
# ==========================================================================


def live_sleepers() -> list[str]:
    """PIDs of `sleep 999x` processes, matched on exact argv.

    Scanned from /proc rather than with `pgrep -f`, which would also match the
    shell running the check -- the false positive that makes "did it actually
    die?" impossible to answer honestly.
    """
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = [a for a in (entry / "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            continue
        if len(argv) == 2 and argv[0].endswith(b"sleep") and argv[1].startswith(b"999"):
            found.append(entry.name)
    return found


@pytest.mark.sandbox
async def test_stopping_a_container_kills_everything_it_started(
    daemon, tmp_path, require_sandbox
):
    """Including processes that deliberately escape the process group.

    `setsid` puts a process in its own session, so killing the container's
    process group misses it. The PID namespace is what actually guarantees
    teardown: bwrap is pid 1 inside it, and the kernel SIGKILLs the rest of the
    namespace when pid 1 exits.
    """
    before = set(live_sleepers())
    daemon.register(config(
        "victim", tmp_path,
        runtime={"command": ["/bin/bash", "-c",
                             "sleep 9999 & "
                             "nohup sleep 9998 >/dev/null 2>&1 & "
                             "setsid sleep 9997 >/dev/null 2>&1 </dev/null & "
                             "sleep 9996"]},
    ))
    await daemon.start("victim")
    await asyncio.sleep(2)

    started = set(live_sleepers()) - before
    assert len(started) == 4, f"expected 4 background processes, saw {started}"

    await daemon.stop("victim")
    await asyncio.sleep(1.5)

    survivors = set(live_sleepers()) & started
    assert not survivors, f"processes outlived their container: {survivors}"


def test_a_config_cannot_opt_out_of_the_pid_namespace(tmp_path):
    """The guarantee above depends on it, so it is not configurable."""
    from capwrap.errors import ConfigError

    with pytest.raises(ConfigError, match="must include 'pid'"):
        load_config_data(
            {"name": "leaky", "sandbox": {"unshare": ["ipc", "uts"]}},
            base_dir=tmp_path,
        )


# ==========================================================================
# permission escalation through spawning
# ==========================================================================


def spawn_request(name: str, permissions: dict) -> dict:
    return {
        "factory_slot": 3,
        "config": {"name": name, "runtime": {"permissions": permissions}},
    }


async def parent_with_factory(daemon, tmp_path, *, permissions, envelope=None):
    runtime = {"permissions": permissions}
    if envelope is not None:
        runtime["permission_envelope"] = envelope
    daemon.register(config(
        "boss", tmp_path,
        runtime=runtime,
        caps={"factory": {"rights": ["create"], "quota": {"containers": 5}}},
    ))
    container = daemon.containers["boss"]
    container.server = await daemon._serve_container(container)
    # Spawning must not actually launch a sandbox in these tests.
    daemon.spawn_container = lambda cfg, parent: daemon.register(cfg, parent=parent).obj
    return container


async def test_a_narrower_child_spawns_without_asking_anyone(daemon, tmp_path):
    container = await parent_with_factory(
        daemon, tmp_path,
        permissions={"allow": ["Read", "Bash(git *)"], "deny": ["Bash(sudo *)"]},
    )
    reply = await request(container.paths.socket, "ctr.spawn", spawn_request(
        "child", {"allow": ["Read"], "deny": ["Bash(sudo *)"]},
    ))
    assert reply.ok, reply.message
    assert not daemon.pending_approvals(), "narrowing must never reach the operator"


async def test_a_child_inherits_the_parents_permissions_when_it_asks_for_none(
    daemon, tmp_path
):
    permissions = {"allow": ["Read"], "deny": ["Bash(sudo *)"]}
    container = await parent_with_factory(daemon, tmp_path, permissions=permissions)

    reply = await request(container.paths.socket, "ctr.spawn", {
        "factory_slot": 3, "config": {"name": "child"},
    })
    assert reply.ok, reply.message
    child = daemon.containers["child"]
    assert child.config.runtime.permissions.allow == ["Read"]
    assert child.config.runtime.permissions.deny == ["Bash(sudo *)"]


async def test_a_wider_child_is_blocked_until_the_operator_agrees(daemon, tmp_path):
    """The hole this whole mechanism exists to close.

    A container confined to Read tries to spawn a child that can run anything --
    which would let it act through the child. The factory capability alone says
    nothing about tool permissions, so without this check the spawn succeeds.
    """
    container = await parent_with_factory(
        daemon, tmp_path, permissions={"allow": ["Read"], "deny": ["Bash(sudo *)"]},
    )

    spawning = asyncio.ensure_future(request(
        container.paths.socket, "ctr.spawn",
        spawn_request("overreach", {"allow": ["Read", "Bash(*)"],
                                    "deny": ["Bash(sudo *)"]}),
    ))
    await asyncio.sleep(0.1)

    pending = daemon.pending_approvals()
    assert len(pending) == 1, "the escalation did not reach the operator"
    assert pending[0]["context"]["kind"] == "permission_escalation"
    assert any("Bash(*)" in r for r in pending[0]["context"]["reasons"])

    daemon.resolve_approval(pending[0]["id"], "deny", "no")
    reply = await asyncio.wait_for(spawning, timeout=5)

    assert not reply.ok
    assert "operator declined" in (reply.message or "")
    assert "overreach" not in daemon.containers


async def test_the_operator_can_approve_an_escalation(daemon, tmp_path):
    container = await parent_with_factory(
        daemon, tmp_path, permissions={"allow": ["Read"]},
    )
    spawning = asyncio.ensure_future(request(
        container.paths.socket, "ctr.spawn",
        spawn_request("wider", {"allow": ["Read", "Write"]}),
    ))
    await asyncio.sleep(0.1)

    pending = daemon.pending_approvals()
    daemon.resolve_approval(pending[0]["id"], "allow", "this one is fine")
    reply = await asyncio.wait_for(spawning, timeout=5)

    assert reply.ok, reply.message
    assert "wider" in daemon.containers


async def test_an_envelope_pre_authorises_a_range(daemon, tmp_path):
    """The less human-invasive route: decide once in the config, not per spawn."""
    container = await parent_with_factory(
        daemon, tmp_path,
        permissions={"allow": ["Read"], "deny": ["Bash(sudo *)"]},
        envelope={"allow": ["Read", "Bash(git *)"], "deny": ["Bash(sudo *)"]},
    )

    # Inside the envelope, though beyond the parent's own policy: no prompt.
    reply = await request(container.paths.socket, "ctr.spawn", spawn_request(
        "helper", {"allow": ["Read", "Bash(git status)"], "deny": ["Bash(sudo *)"]},
    ))
    assert reply.ok, reply.message
    assert not daemon.pending_approvals()

    # Beyond the envelope: still stops.
    spawning = asyncio.ensure_future(request(
        container.paths.socket, "ctr.spawn",
        spawn_request("greedy", {"allow": ["Bash(*)"], "deny": ["Bash(sudo *)"]}),
    ))
    await asyncio.sleep(0.1)
    assert daemon.pending_approvals()
    daemon.resolve_approval(daemon.pending_approvals()[0]["id"], "deny", "")
    await asyncio.wait_for(spawning, timeout=5)


async def test_escalation_attempts_are_audited(daemon, tmp_path):
    container = await parent_with_factory(
        daemon, tmp_path, permissions={"allow": ["Read"]},
    )
    spawning = asyncio.ensure_future(request(
        container.paths.socket, "ctr.spawn",
        spawn_request("nope", {"allow": ["Bash"]}),
    ))
    await asyncio.sleep(0.1)
    daemon.resolve_approval(daemon.pending_approvals()[0]["id"], "deny", "")
    await asyncio.wait_for(spawning, timeout=5)

    entries = daemon.audit.tail(limit=40)
    assert any(e["op"] == "policy.escalation" and not e["allowed"] for e in entries)


# ==========================================================================
# capability requests -- approval that actually grants
# ==========================================================================


async def test_ask_alone_grants_nothing(daemon, tmp_path):
    """`capctl ask` is a question, not a request.

    Approving it tells the agent "yes" and changes nothing, which is exactly the
    confusion `cap.request` exists to remove.
    """
    daemon.register(config("solo", tmp_path))
    c = daemon.containers["solo"]
    c.server = await daemon._serve_container(c)

    before = len(daemon.kernel.tasks["solo"])
    asking = asyncio.ensure_future(
        request(c.paths.socket, "ask", {"question": "may I have a factory?"})
    )
    await asyncio.sleep(0.05)
    daemon.resolve_approval(daemon.pending_approvals()[0]["id"], "allow", "sure")
    reply = await asyncio.wait_for(asking, timeout=5)

    assert reply.result["decision"] == "allow"
    assert len(daemon.kernel.tasks["solo"]) == before, \
        "ask must not change the capability table"


async def test_a_granted_request_lands_in_the_cap_table(daemon, tmp_path):
    daemon.register(config("solo", tmp_path))
    daemon.register(config("peer", tmp_path))
    c = daemon.containers["solo"]
    c.server = await daemon._serve_container(c)

    asking = asyncio.ensure_future(request(c.paths.socket, "cap.request", {
        "kind": "container", "target": "peer",
        "rights": ["send", "inspect"], "reason": "need to report results",
    }))
    await asyncio.sleep(0.05)

    pending = daemon.pending_approvals()[0]
    assert pending["context"]["kind"] == "capability_request"
    assert pending["context"]["request"]["target"] == "peer"
    assert pending["context"]["request"]["reason"] == "need to report results"

    daemon.resolve_approval(pending["id"], "allow")
    reply = await asyncio.wait_for(asking, timeout=5)

    assert reply.ok and reply.result["granted"] is True
    slot = reply.result["slot"]

    # The agent can use it immediately, without restarting or being told twice.
    caps = (await request(c.paths.socket, "cap.list")).result
    granted = [x for x in caps if x["slot"] == slot][0]
    assert granted["label"] == "peer:peer"
    assert sorted(granted["rights"]) == ["inspect", "send"]

    sent = await request(c.paths.socket, "msg.send",
                         {"slot": slot, "payload": "hello"})
    assert sent.ok


async def test_the_operator_can_grant_less_than_was_asked_for(daemon, tmp_path):
    """Asking for kill should not mean getting kill."""
    daemon.register(config("solo", tmp_path))
    daemon.register(config("peer", tmp_path))
    c = daemon.containers["solo"]
    c.server = await daemon._serve_container(c)

    asking = asyncio.ensure_future(request(c.paths.socket, "cap.request", {
        "kind": "container", "target": "peer",
        "rights": ["send", "inspect", "kill"],
    }))
    await asyncio.sleep(0.05)
    daemon.resolve_approval(
        daemon.pending_approvals()[0]["id"], "allow", "", rights=["send"],
    )
    reply = await asyncio.wait_for(asking, timeout=5)

    assert reply.result["rights"] == ["send"]
    with pytest.raises(Exception):
        daemon.kernel.ctr_kill("solo", reply.result["slot"])


async def test_a_denied_request_grants_nothing(daemon, tmp_path):
    daemon.register(config("solo", tmp_path))
    daemon.register(config("peer", tmp_path))
    c = daemon.containers["solo"]
    c.server = await daemon._serve_container(c)
    before = len(daemon.kernel.tasks["solo"])

    asking = asyncio.ensure_future(request(c.paths.socket, "cap.request", {
        "kind": "container", "target": "peer", "rights": ["send"],
    }))
    await asyncio.sleep(0.05)
    daemon.resolve_approval(daemon.pending_approvals()[0]["id"], "deny", "no")
    reply = await asyncio.wait_for(asking, timeout=5)

    assert reply.ok and reply.result["granted"] is False
    assert len(daemon.kernel.tasks["solo"]) == before


async def test_requesting_a_factory_makes_spawning_work(daemon, tmp_path):
    """The exact case that prompted this: an agent with no factory asks for one."""
    daemon.register(config("solo", tmp_path))
    c = daemon.containers["solo"]
    c.server = await daemon._serve_container(c)
    daemon.spawn_container = lambda cfg, parent: daemon.register(cfg, parent=parent).obj

    assert not any(x.kind == "factory" for x in daemon.kernel.cap_list("solo"))

    asking = asyncio.ensure_future(request(c.paths.socket, "cap.request", {
        "kind": "factory", "rights": ["create"], "quota": 2,
        "reason": "I need a helper to run the test suite",
    }))
    await asyncio.sleep(0.05)
    daemon.resolve_approval(daemon.pending_approvals()[0]["id"], "allow",
                            rights=["create"])
    reply = await asyncio.wait_for(asking, timeout=5)
    assert reply.result["granted"]

    factory_slot = reply.result["slot"]
    spawned = await request(c.paths.socket, "ctr.spawn", {
        "factory_slot": factory_slot, "config": {"name": "helper"},
    })
    assert spawned.ok, spawned.message
    assert "helper" in daemon.containers


async def test_a_requested_capability_is_still_revocable(daemon, tmp_path):
    """Nothing granted this way escapes the mapping database."""
    daemon.register(config("solo", tmp_path))
    daemon.register(config("peer", tmp_path))
    c = daemon.containers["solo"]
    c.server = await daemon._serve_container(c)

    asking = asyncio.ensure_future(request(c.paths.socket, "cap.request", {
        "kind": "container", "target": "peer", "rights": ["send"],
    }))
    await asyncio.sleep(0.05)
    daemon.resolve_approval(daemon.pending_approvals()[0]["id"], "allow")
    slot = (await asyncio.wait_for(asking, timeout=5)).result["slot"]

    node = daemon.kernel.tasks["solo"].slots[slot].node
    daemon.kernel._apply_revocations(daemon.kernel.mapdb.revoke(node))
    assert slot not in daemon.kernel.tasks["solo"].slots


async def test_an_unknown_request_kind_is_refused(daemon, tmp_path):
    daemon.register(config("solo", tmp_path))
    c = daemon.containers["solo"]
    c.server = await daemon._serve_container(c)

    reply = await request(c.paths.socket, "cap.request",
                          {"kind": "root", "target": "everything"})
    assert not reply.ok
    assert not daemon.pending_approvals(), "a bad kind must not reach the operator"
