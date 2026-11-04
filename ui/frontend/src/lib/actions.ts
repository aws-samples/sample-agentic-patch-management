/**
 * Whether a suggested prompt only reads (status, findings, verification) or
 * could change something. Reads from card buttons and the command palette
 * send at once; anything else fills the chat box so the operator sees the
 * prompt and sends it themselves.
 */
const READ_START = /^(show|list|check|get|what|which|who|when|how|is|are|has|have|does|verify|view|explain|summari[sz]e|forecast|compare|find)\b/i;
// "patch" before compliance, status and the like is a noun, not a request to patch.
const PATCH_NOUN = "(?!\\s+(compliance|status|state|states|baselines?|polic(y|ies)|reports?|windows?|history|level|data|counts?)\\b)";
const CHANGE_WORDS = new RegExp(
  `\\b(patch${PATCH_NOUN}|install|upgrade|update|roll\\s?back|rollback|undo|reboot|restart|stop|cancel|abort|approve|delete|remove|create|schedule|run|execute|apply|fix|remediate)\\b`,
  "i",
);

export function isReadOnlyPrompt(prompt: string): boolean {
  const text = prompt.trim();
  return READ_START.test(text) && !CHANGE_WORDS.test(text);
}
