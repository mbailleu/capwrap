/**
 * capwrap gate for opencode v2 — route permission prompts to the operator's inbox.
 *
 * Dropped at `/home/agent/.config/opencode2/plugins/capwrap.ts` by capwrap's
 * fsprep (v2 reads its own config dir, not v1's `~/.config/opencode`). opencode
 * auto-loads every `.ts` file in that directory (Bun runtime, no build step),
 * so this file must stay dependency-free: `node:` builtins only, a plain
 * object export, no `@opencode-ai/plugin` import.
 *
 * opencode v2 runs the `permission.evaluate` hook for every tool call and
 * blocks until the handler resolves. Setting `event.effect` to "allow" or
 * "deny" overrides opencode's own decision; leaving it untouched lets opencode
 * fall back to the effect it computed itself.
 *
 * Failure is deliberately fail-open — the opposite of the pi shim. If the
 * daemon cannot be reached we leave `event.effect` untouched, so opencode
 * falls back to its own native prompt. That is hook.py's "not silent-allow"
 * rationale: a broken gate must not quietly disable every check. The
 * difference is that opencode *has* a native prompt to fall back to, so plain
 * fallthrough is the safe equivalent of hook.py's `ask` — pi has none, which
 * is why its shim blocks instead.
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
//: Long, because the whole design is that a human answers this. opencode's own
//: prompt has no timeout either.
const ASK_TIMEOUT = Number(process.env.CAPWRAP_ASK_TIMEOUT ?? "3600") || 3600;
const CONTAINER = process.env.CAPWRAP_CONTAINER ?? "?";

/** Auto-decisions the operator configured up front.
 *
 * Reduces the queue to things that actually need a human: nobody wants to
 * approve every single `Read`.
 */
function loadPolicy(): { allow: string[]; deny: string[]; fallback?: string } {
  try {
    const parsed = JSON.parse(fs.readFileSync(POLICY, "utf8")) as {
      allow?: unknown;
      deny?: unknown;
      fallback?: unknown;
    };
    return {
      allow: Array.isArray(parsed.allow) ? parsed.allow.map(String) : [],
      deny: Array.isArray(parsed.deny) ? parsed.deny.map(String) : [],
      fallback: typeof parsed.fallback === "string" ? parsed.fallback : undefined,
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
  // glob patterns (see agents._normalize_rule).  opencode's own tool names
  // are lowercase already, so the comparison is plain.
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

export default {
  id: "capwrap",
  setup: async (ctx: any) => {
    await ctx.permission.hook("evaluate", async (event: any) => {
      const tool = String(event.action ?? "?");
      const resources = Array.isArray(event.resources)
        ? event.resources.map(String)
        : [];
      const summary = resources.join(" ");

      // The policy file also carries `fallback`, decided host-side: "ask"
      // when the encoded permission block gives opencode's own prompt a
      // floor to fall back to, "deny" when it does not.  An unreadable
      // policy counts as no floor.
      let policy: { allow: string[]; deny: string[]; fallback?: string };
      try {
        policy = loadPolicy();
      } catch {
        policy = { allow: [], deny: [], fallback: "deny" };
      }

      try {
        if (matches(policy.deny, tool, summary)) {
          event.effect = "deny";
          event.message = `capwrap policy denies ${tool}`;
          return;
        }
        if (matches(policy.allow, tool, summary)) {
          // capwrap's allow means allow, the same as every other shim: set
          // the effect outright rather than deferring to opencode's own
          // decision, which the merged config may have set to "ask".
          event.effect = "allow";
          return;
        }

        const question = summary ? `${tool}: ${summary}` : `run ${tool}`;
        const result = await askDaemon(question, {
          tool,
          input: { resources, metadata: event.metadata },
          container: CONTAINER,
          session: event.sessionID,
        });

        const reason = result.reason ?? "";
        if (result.decision === "allow") {
          event.effect = "allow";
          event.message = reason || "approved in the capwrap console";
        } else if (result.decision === "deny") {
          event.effect = "deny";
          event.message = reason || "denied in the capwrap console";
        }
        // Anything else (timeout, no answer): leave event.effect untouched so
        // opencode falls back to its own computed decision.
      } catch (err) {
        const detail = err instanceof Error ? err.message : String(err);
        if (policy.fallback === "ask") {
          // There is an ask floor: leaving the effect untouched hands the
          // decision to opencode's own prompt -- the safe equivalent of
          // hook.py's "ask".
          event.message = `capwrap unreachable (${detail}); falling back to opencode's own prompt`;
        } else {
          // No ask floor: opencode's own decision would likely be "allow",
          // and an unreachable daemon must not silently disable governance.
          event.effect = "deny";
          event.message = `capwrap unreachable (${detail}); no ask floor to fall back to`;
        }
      }
    });
  },
};