// Nimrod context plugin for OpenCode (installed by `nimrod install`).
//
// Injects the Nimrod awareness notice into the system prompt once per session,
// before the first model call (the brief itself is never injected -- the model
// fetches it through the MCP tools when it needs context). Capture of the
// session itself happens through the same hook script the other agents use.
import { execFile } from "node:child_process";

const NIMROD_BIN = process.env.NIMROD_BIN || "__NIMROD_BIN__";
const injected = new Set();

function run(args, timeoutMs) {
  return new Promise((resolve) => {
    execFile(NIMROD_BIN, args, { timeout: timeoutMs, maxBuffer: 4 * 1024 * 1024 },
      (error, stdout) => resolve(error ? "" : String(stdout || "")));
  });
}

export const NimrodContextPlugin = async () => ({
  "experimental.chat.system.transform": async (input, output) => {
    const sessionID = input?.sessionID;
    if (sessionID && injected.has(sessionID)) return;
    if (sessionID) injected.add(sessionID);

    const project = process.env.NIMROD_PROJECT || process.cwd();
    const text = await run(
      ["hook", "session-start", "--agent", "opencode", "--project", project],
      20000,
    );
    const trimmed = text.trim();
    if (trimmed && Array.isArray(output?.system)) {
      output.system.push(trimmed);
    }
  },
});

export default {
  id: "nimrod.context",
  server: NimrodContextPlugin,
};
