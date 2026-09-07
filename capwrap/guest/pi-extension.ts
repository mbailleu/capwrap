/**
 * capwrap gate for pi — the only thing standing between a tool call and execution.
 *
 * Dropped at `/home/agent/.pi/agent/extensions/capwrap-gate.ts` by capwrap's
 * fsprep. pi auto-loads every `.ts` file in that directory (Node via jiti, no
 * build step), so this file must stay dependency-free: `node:` builtins only,
 * a plain function export, no `@earendil-works/pi-coding-agent` import.
 *
 * pi has no native permission system — its docs say "no permission popups —
 * build your own with extensions". So unlike the opencode shim there is no
 * built-in prompt to fall back to, and this gate is the whole approval
 * mechanism. Failure is therefore FAIL-CLOSED: if the daemon cannot be reached
 * we block the tool call. A silent allow would disable all approvals, which is
 * exactly the failure hook.py's "not silent-allow" rationale exists to prevent
 * — but where hook.py and the opencode shim can hand the decision back to a
 * native prompt, pi has none, so blocking is the only safe default.
 *
 * pi does not time out `tool_call` handlers, so this shim races its own hard
 * timeout (`CAPWRAP_ASK_TIMEOUT`, default 3600s) and blocks on expiry.
 *
 * `ctx.hasUI` is irrelevant here: the decision comes from the capwrap daemon,
 * not from pi's UI, so the gate behaves identically in interactive mode and
 * `--mode rpc`.
 *
 * Protocol (mirrors hook.py exactly): one JSON line over the daemon's unix
 * socket, newline-delimited, blocking until the operator answers.
 *
 *     → {"id":1,"op":"ask","args":{"question":...,"context":{...},"block":true,"timeout":3600}}
 *     ← {"ok":true,"result":{"decision":"allow"|"deny","reason":"..."}}
 *
 * Auto-decisions come from the policy file at `/run/capwrap-policy.json`,
 * bound read-only so an agent with a shell cannot widen its own permissions.
 */

import fs from "node:fs";
import net from "node:net";

const SOCKET = process.env.CAPWRAP_SOCKET ?? "/run/capwrap.sock";
const POLICY = process.env.CAPWRAP_POLICY ?? "/run/capwrap-policy.json";
//: Long, because the whole design is that a human answers this.
const ASK_TIMEOUT = Number(process.env.CAPWRAP_ASK_TIMEOUT ?? "3600") || 3600;
const CONTAINER = process.env.CAPWRAP_CONTAINER ?? "?";

/** Auto-decisions the operator configured up front.
 *
 * Reduces the queue to things that actually need a human: nobody wants to
 * approve every single `Read`.
 */
function loadPolicy(): { allow: string[]; deny: string[] } {
  try {
    const parsed = JSON.parse(fs.readFileSync(POLICY, "utf8")) as {
      allow?: unknown;
      deny?: unknown;
    };
    return {
      allow: Array.isArray(parsed.allow) ? parsed.allow.map(String) : [],
      deny: Array.isArray(parsed.deny) ? parsed.deny.map(String) : [],
    };
  } catch {
    return { allow: [], deny: [] };
  }
}

/** Translate a fnmatch-style glob into a RegExp: `*`, `?`, `[...]`.
 *
 * Mirrors Python's `fnmatch.translate`, which is what hook.py's policy
 * matching uses: `*` matches anything (including `/`), `?` matches one
 * character, `[abc]`/`[!abc]` match one character in / not in a set, ranges
 * like `[a-z]` work, and a leading `^` or `]` inside a class is literal (a
 * backslash is literal too — fnmatch does not escape with it). Everything else
 * is literal.
 */
function globToRegExp(pattern: string): RegExp {
  let out = "";
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i];
    if (c === "*") {
      out += ".*";
    } else if (c === "?") {
      out += ".";
    } else if (c === "[") {
      let j = i + 1;
      if (pattern[j] === "!") j++;
      if (pattern[j] === "]") j++;
      let k = j;
      while (k < pattern.length && pattern[k] !== "]") k++;
      if (k >= pattern.length) {
        out += "\\[";
        continue;
      }
      let stuff = pattern.slice(i + 1, k);
      stuff = stuff.replace(/\\/g, "\\\\");
      if (stuff.startsWith("!")) {
        stuff = "^" + stuff.slice(1);
      } else if (stuff.startsWith("^")) {
        stuff = "\\" + stuff;
      }
      // Python's re treats a ']' right after '[' or '[^' as a literal member;
      // JavaScript does not, so escape it to keep the class from closing early.
      if (stuff.startsWith("]")) {
        stuff = "\\" + stuff;
      } else if (stuff.startsWith("^]")) {
        stuff = "^\\" + stuff.slice(1);
      }
      out += "[" + stuff + "]";
      i = k;
    } else {
      out += c.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }
  }
  return new RegExp("^" + out + "$");
}

/** A rule matches a bare tool name, or `Tool(pattern)` against its argument.
 *
 * `Bash(git *)` is the shape people actually want: allow git, keep asking
 * about everything else the same tool could do.
 */
function matches(rules: string[] | undefined, tool: string, summary: string): boolean {
  // Rules arrive pre-normalised from the policy file: lowercase tool names,
  // glob patterns (see agents._normalize_rule).  The tool name is lowercased
  // once at the entry point below, so the comparison here is plain.
  if (!rules) return false;
  for (const rule of rules) {
    if (rule === tool || rule === "*") return true;
    if (rule.includes("(") && rule.endsWith(")")) {
      const open = rule.indexOf("(");
      const name = rule.slice(0, open);
      const pattern = rule.slice(open + 1, -1);
      if (name === tool && globToRegExp(pattern).test(summary)) return true;
    }
  }
  return false;
}

/** A one-line summary, because that is what the operator reads first.
 *
 * The fallback shows argument *values*, not just their names: a queue entry
 * reading "Skill: skill" tells you nothing, while "Skill: skill=capwrap" tells
 * you what is about to happen. Ported from hook.py's `describe()`; the bash
 * check is case-insensitive because pi spells the tool `bash`, not `Bash`.
 */
function describe(tool: string, input: Record<string, unknown>): string {
  if (tool.toLowerCase() === "bash") {
    return short(String(input.command ?? "").trim());
  }
  for (const key of ["file_path", "path", "url", "pattern", "notebook_path",
    "command", "name", "query", "prompt"]) {
    if (key in input) return short(input[key]);
  }
  const parts = Object.keys(input)
    .sort()
    .map((k) => `${k}=${short(input[k])}`);
  return parts.slice(0, 3).join(", ") || "(no arguments)";
}

function short(value: unknown, limit = 120): string {
  let text: string;
  if (typeof value === "string") text = value;
  else if (value === null) text = "null";
  else if (value === undefined) text = "";
  else if (typeof value === "object") text = JSON.stringify(value);
  else text = String(value);
  text = text.split(/\s+/).join(" ");
  return text.length <= limit ? text : text.slice(0, limit - 1) + "…";
}

/** Block on the capwrap daemon until the operator answers. */
function askDaemon(
  question: string,
  context: Record<string, unknown>,
): Promise<{ decision?: string; reason?: string }> {
  return new Promise((resolve, reject) => {
    const sock = net.createConnection(SOCKET);
    let buffer = "";
    let settled = false;

    const cleanup = () => {
      sock.off("data", onData);
      sock.off("error", onError);
      sock.off("close", onClose);
      sock.off("timeout", onTimeout);
      sock.destroy();
    };
    const fail = (err: Error) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(err);
    };
    const onData = (chunk: Buffer) => {
      buffer += chunk.toString("utf8");
      const nl = buffer.indexOf("\n");
      if (nl === -1 || settled) return;
      settled = true;
      cleanup();
      const line = buffer.slice(0, nl);
      try {
        const reply = JSON.parse(line) as {
          ok?: boolean;
          result?: { decision?: string; reason?: string };
          error?: { message?: string };
        };
        if (!reply.ok) {
          reject(new Error(reply.error?.message ?? "request failed"));
        } else {
          resolve(reply.result ?? {});
        }
      } catch (err) {
        reject(err instanceof Error ? err : new Error(String(err)));
      }
    };
    const onError = (err: Error) => fail(err);
    const onClose = () => fail(new Error("the daemon closed the connection"));
    const onTimeout = () =>
      fail(new Error(`timed out waiting for the capwrap daemon (${ASK_TIMEOUT}s)`));

    sock.on("data", onData);
    sock.on("error", onError);
    sock.on("close", onClose);
    sock.on("timeout", onTimeout);
    sock.setTimeout(Math.max(1, Math.round(ASK_TIMEOUT * 1000)));
    try {
      sock.write(
        JSON.stringify({
          id: 1,
          op: "ask",
          args: { question, context, block: true, timeout: ASK_TIMEOUT },
        }) + "\n",
      );
    } catch (err) {
      fail(err instanceof Error ? err : new Error(String(err)));
    }
  });
}

/** Reject after `ms`, so a hung daemon cannot block a tool call forever.
 *
 * pi does not time out `tool_call` handlers itself, so the gate must. The
 * timer is unref'd so a pending timeout cannot keep the process alive once the
 * daemon's answer has already decided the race.
 */
function timeout(ms: number): Promise<never> {
  return new Promise((_, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`timed out after ${ms}ms waiting for the capwrap daemon`));
    }, ms);
    if (typeof (timer as any)?.unref === "function") (timer as any).unref();
  });
}

export default function (pi: any) {
  pi.on("tool_call", async (event: any, ctx: any) => {
    // ctx is unused: the decision comes from the daemon, not from pi's UI, so
    // `ctx.hasUI` changes nothing about how this gate behaves.
    const tool = String(event.toolName ?? "?").toLowerCase();
    const input =
      event.input && typeof event.input === "object"
        ? (event.input as Record<string, unknown>)
        : {};
    const summary = describe(tool, input);

    try {
      const policy = loadPolicy();
      if (matches(policy.deny, tool, summary)) {
        return { block: true, reason: `capwrap policy denies ${tool}` };
      }
      if (matches(policy.allow, tool, summary)) {
        return undefined;
      }

      const question = summary ? `${tool}: ${summary}` : `run ${tool}`;
      const ms = Math.max(1, Math.round(ASK_TIMEOUT * 1000));
      const result = await Promise.race([
        askDaemon(question, {
          tool,
          input,
          container: CONTAINER,
        }),
        timeout(ms),
      ]);

      const reason = result.reason ?? "";
      if (result.decision === "allow") return undefined;
      if (result.decision === "deny") {
        return { block: true, reason: reason || "denied in the capwrap console" };
      }
      return { block: true, reason: reason || "no answer from the operator" };
    } catch (err) {
      // Fail closed: pi has no native prompt to fall back to, so a silent
      // allow here would disable every approval in the system.
      const msg = err instanceof Error ? err.message : String(err);
      return {
        block: true,
        reason: `capwrap unreachable (${msg}); blocking because pi has no native prompt to fall back to`,
      };
    }
  });
}