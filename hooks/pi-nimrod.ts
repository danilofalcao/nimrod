// Nimrod worklog extension for the Pi coding agent (installed by `nimrod install`).
//
// Pi has no shell hooks, so this extension plays the role the hook script plays
// for Claude/Codex/OpenCode:
//
//   * on session start it refreshes the worklog and injects the Nimrod
//     awareness notice (not the brief) into the first model call (as a hidden
//     persisted custom message), so the model knows it can consult the tools;
//   * before every prompt it asks Nimrod for a gated auto-recall when the prompt
//     mentions another known project;
//   * on session shutdown it refreshes the worklog in the background.
//
// It never blocks or fails a session: every Nimrod call is best-effort and its
// output is swallowed on error.
import { execFile } from "node:child_process";

const NIMROD_BIN = process.env.NIMROD_BIN || "__NIMROD_BIN__";

function run(args, input, timeoutMs) {
  return new Promise((resolve) => {
    const child = execFile(
      NIMROD_BIN,
      args,
      { timeout: timeoutMs || 25000, maxBuffer: 8 * 1024 * 1024 },
      (error, stdout) => resolve(error ? "" : String(stdout || "")),
    );
    if (input !== undefined && child.stdin) {
      try {
        child.stdin.on("error", () => {});
        child.stdin.end(input);
      } catch {
        // ignore
      }
    }
  });
}

function projectOf(ctx) {
  return (ctx && (ctx.cwd || (ctx.sessionManager && ctx.sessionManager.getCwd()))) ||
    process.env.NIMROD_PROJECT ||
    process.cwd();
}

export default function NimrodPiExtension(pi) {
  let startContext = "";
  let startFetched = false;
  let injected = false;

  async function fetchStartContext(project) {
    const text = await run(
      ["hook", "session-start", "--agent", "pi", "--project", project],
      JSON.stringify({ cwd: project }),
      25000,
    );
    startContext = text.trim();
    startFetched = true;
  }

  pi.on("session_start", async (_event, ctx) => {
    injected = false;
    startFetched = false;
    startContext = "";
    await fetchStartContext(projectOf(ctx));
  });

  pi.on("before_agent_start", async (event, ctx) => {
    const project = projectOf(ctx);
    const parts = [];

    // Fallback in case session_start has not completed yet (or did not fire).
    if (!startFetched) await fetchStartContext(project);

    const prompt = (event && event.prompt) || "";

    if (!injected && startContext && prompt.trim().length >= 12) {
      injected = true;
      parts.push(startContext);
    }

    try {
      const sessionId =
        (ctx && ctx.sessionManager && ctx.sessionManager.getSessionId &&
          ctx.sessionManager.getSessionId()) || "";
      const raw = await run(
        ["hook", "prompt-submit", "--agent", "pi", "--project", project],
        JSON.stringify({
          prompt: (event && event.prompt) || "",
          cwd: project,
          session_id: sessionId,
        }),
        10000,
      );
      const trimmed = raw.trim();
      if (trimmed) {
        const parsed = JSON.parse(trimmed);
        const extra = parsed && parsed.hookSpecificOutput &&
          parsed.hookSpecificOutput.additionalContext;
        if (extra) parts.push(extra);
      }
    } catch {
      // ignore malformed / empty auto-recall output
    }

    if (parts.length === 0) return;
    return {
      message: {
        customType: "nimrod",
        content: parts.join("\n\n"),
        display: false,
      },
    };
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    const project = projectOf(ctx);
    try {
      const child = execFile(
        NIMROD_BIN,
        ["hook", "session-end", "--agent", "pi", "--project", project, "--semantic"],
        { detached: true, stdio: "ignore" },
      );
      if (child.unref) child.unref();
    } catch {
      // ignore
    }
  });
}
