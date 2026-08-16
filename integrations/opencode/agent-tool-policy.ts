/**
 * Route opencode's bash tool calls through agent-tool-policy.
 *
 * opencode has no subprocess hook of its own, so this shim gives the same
 * PreToolUse decision the Claude and Codex hooks make. It speaks the hook's
 * payload shape on stdin, then applies the answer with the two levers opencode
 * offers: throwing blocks the call, and assigning `output.args` rewrites it.
 *
 * The hook is told `--host opencode`, which selects an adapter that never
 * returns an ask state, because there is nothing here to ask with.
 */
import type { Plugin } from "@opencode-ai/plugin"

const HOOK = `${process.env.HOME}/.claude/hooks/bash-policy-hook`
const TIMEOUT_MS = 5000

type HookDecision = {
  hookSpecificOutput?: {
    permissionDecision?: "allow" | "deny" | "ask"
    permissionDecisionReason?: string
    additionalContext?: string
    updatedInput?: { command?: string }
  }
}

async function decide(command: string, cwd: string): Promise<HookDecision | null> {
  const child = Bun.spawn([HOOK, "--host", "opencode"], {
    stdin: new TextEncoder().encode(
      JSON.stringify({ tool_name: "Bash", tool_input: { command }, cwd }),
    ),
    stdout: "pipe",
    stderr: "ignore",
  })

  const timer = setTimeout(() => child.kill(), TIMEOUT_MS)
  try {
    const stdout = await new Response(child.stdout).text()
    await child.exited
    return stdout.trim() ? (JSON.parse(stdout) as HookDecision) : null
  } catch {
    // A missing or broken hook must not take the session down with it.
    return null
  } finally {
    clearTimeout(timer)
  }
}

export const AgentToolPolicy: Plugin = async ({ directory }) => ({
  "tool.execute.before": async (input, output) => {
    if (input.tool !== "bash") return
    const command = output.args?.command
    if (typeof command !== "string" || !command) return

    const decision = await decide(command, directory)
    const result = decision?.hookSpecificOutput
    if (!result) return

    if (result.permissionDecision === "deny" || result.permissionDecision === "ask") {
      throw new Error(result.permissionDecisionReason ?? "Blocked by agent-tool-policy")
    }

    const rewritten = result.updatedInput?.command
    if (typeof rewritten === "string" && rewritten) {
      output.args.command = rewritten
    }
  },
})
