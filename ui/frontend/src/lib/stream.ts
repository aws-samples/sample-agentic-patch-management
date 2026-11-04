import type { ResultCard } from "../hooks/useAgentStream";
import { apiFetch, SignInExpired } from "./http";
import { SseParser } from "./sse";

/**
 * POST an action that streams its outcome (approve, stop) outside the chat, and
 * collect the cards it returns. interrupted is true when the request may have
 * reached the runtime but the reply didn't finish, so the outcome isn't known.
 */
export async function streamAction(url: string, body: Record<string, unknown>):
    Promise<{ ok: boolean; cards: ResultCard[]; interrupted: boolean }> {
  const cards: ResultCard[] = [];
  let res: Response;
  try {
    res = await apiFetch(url, { method: "POST", body: JSON.stringify(body) });
  } catch (e) {
    // Refused at sign-in, it never reached the runtime; any other failure may have.
    return { ok: false, cards, interrupted: !(e instanceof SignInExpired) };
  }
  if (!res.ok || !res.body) return { ok: false, cards, interrupted: false };
  const parser = new SseParser();
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let done = false, cut = false, started = false;
  const take = (e: unknown) => {
    const ev = e as { type?: string; card?: ResultCard };
    if (ev.type === "card" && ev.card) cards.push(ev.card);
    if (ev.type === "done") done = true;
    // An error after the runtime started working is a reply cut off, not a refusal.
    else if (ev.type === "error") cut ||= started;
    else started = true;
  };
  try {
    for (;;) {
      const { done: end, value } = await reader.read();
      if (end) break;
      parser.push(decoder.decode(value, { stream: true })).forEach(take);
    }
    parser.end().forEach(take);
  } catch {
    cut = true;
  }
  return { ok: true, cards, interrupted: cut || !done };
}
