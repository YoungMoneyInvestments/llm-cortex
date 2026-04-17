#!/usr/bin/env node
/**
 * AIR Hook Helper — Bridge between hook-handler.cjs and AIR CLI.
 *
 * Integration points in hook-handler.cjs:
 *   session-end    -> node air_hook.cjs compile
 *   session-restore -> node air_hook.cjs inject
 *   route          -> node air_hook.cjs lookup "user message"
 *
 * Or add to ~/.claude/settings.json hooks directly:
 *   "hooks": {
 *     "SessionEnd": [{"command": "python3 ~/Projects/llm-cortex/scripts/air_cli.py compile 48 && python3 ~/Projects/llm-cortex/scripts/air_cli.py inject && python3 ~/Projects/llm-cortex/scripts/air_cli.py decay"}],
 *     "SessionStart": [{"command": "python3 ~/Projects/llm-cortex/scripts/air_cli.py inject"}],
 *     "UserPromptSubmit": [{"command": "python3 ~/Projects/llm-cortex/scripts/air_cli.py lookup \"$PROMPT\""}]
 *   }
 */

const path = require('path');
const fs = require('fs');
const { execSync } = require('child_process');

const AIR_CLI = path.join(__dirname, 'air_cli.py');

function airExec(cmd, args = [], timeoutMs = 8000) {
  try {
    if (!fs.existsSync(AIR_CLI)) {
      console.log('[AIR] CLI not found at ' + AIR_CLI);
      return null;
    }
    const fullCmd = `python3 "${AIR_CLI}" ${cmd} ${args.map(a => `"${a}"`).join(' ')}`;
    const result = execSync(fullCmd, {
      timeout: timeoutMs,
      encoding: 'utf-8',
      cwd: path.dirname(AIR_CLI),
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    return result.trim();
  } catch (e) {
    console.log(`[AIR] ${cmd} failed: ${e.message.split('\n')[0]}`);
    return null;
  }
}

const [,, command, ...args] = process.argv;

if (command === 'compile') {
  const hours = args[0] || '48';
  const result = airExec('compile', [hours], 15000);
  if (result) console.log(`[AIR] ${result}`);

} else if (command === 'inject') {
  const result = airExec('inject');
  if (result) console.log(`[AIR] ${result}`);

} else if (command === 'lookup') {
  const message = args.join(' ');
  if (message) {
    const result = airExec('lookup', [message]);
    if (result) console.log(result);
  }

} else if (command === 'decay') {
  const result = airExec('decay');
  if (result) console.log(`[AIR] ${result}`);

} else {
  console.log('Usage: air_hook.cjs <compile|inject|lookup|decay> [args]');
}
