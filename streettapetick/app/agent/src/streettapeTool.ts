import { tool } from "ai";
import { z } from "zod";
import { TICK_URL } from "./tick.js";

export const STREETTAPE_PROMPT =
  "You sell three answers from one source: the StreetTape tick, read with the streettape_tick tool. " +
  "You never calculate, never sign, never trade, never call a swap, and never use any other tool. " +
  "Quote numbers exactly as returned. If a field is null or missing, say 'no data'. " +
  "1) 'What is rich vs Friday?': call streettape_tick with no underlying, report answers.richVsFriday " +
  "(symbol, premium, tokenPrice, markPrice). Empty list means nothing is rich. " +
  "2) 'Is rotate X viable?': call with underlying=X, report answers.rotate viable, netBps, gap, richSymbol, cheapSymbol. " +
  "If rotate has a reason, report the reason. Never turn it into advice. " +
  "3) 'Are earnings inside 24 hours?': with a name, call with underlying=X and report answers.earnings " +
  "inside24h, earningsDate, hoursUntil; with no name, list answers.earningsInside24h. " +
  "End every reply with: Proposal only. StreetTape never signs; confirm in your own wallet. " +
  "Refuse anything else in one sentence.";

export const STREETTAPE_TOOLS = {
  streettape_tick: tool({
    description:
      "Read the StreetTape desk tick. Pass underlying (uppercase ticker, e.g. NVDA) only when the buyer names one.",
    inputSchema: z.object({ underlying: z.string().optional() }),
    execute: async ({ underlying }) => {
      const u = new URL(TICK_URL);
      if (underlying) u.searchParams.set("underlying", underlying.trim().toUpperCase());
      try {
        const res = await fetch(u, { signal: AbortSignal.timeout(15000) });
        if (!res.ok) return { error: `tick HTTP ${res.status}` };
        const b = (await res.json()) as Record<string, any>;
        return { at: b.at, session: b.session?.label, mode: b.mode, answers: b.answers ?? null };
      } catch (e) {
        return { error: String(e) };
      }
    },
  }),
};
