import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import { invalidatePackProfile } from "../api/profile";
import { invalidateSession } from "../api/server";

const decisions = new Map<string, string>();
const journals = new Map<string, Record<string, unknown>>();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  decisions.clear();
  journals.clear();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const path = raw.startsWith("http") ? new URL(raw).pathname : raw.split("?")[0];
      const method = (init?.method ?? "GET").toUpperCase();

      if (path === "/api/auth/dev" || path === "/api/auth/telegram") {
        return json({ token: "test-token", user: { telegram_user_id: 900000001 } });
      }
      if (path === "/api/me") {
        const locked = [...decisions.values()];
        return json({
          display_name: "Dev Operator",
          telegram_user_id: 900000001,
          pack_xp: 0,
          wolf_rank: "SCOUT",
          discipline_streak: 0,
          decisions: Object.fromEntries(decisions),
          journal_ids: [...journals.keys()],
          replay_count: 0,
          no_trade_count: locked.filter((decision) => decision === "NO_TRADE").length,
          notification_prefs: {
            new_mission: true,
            lifecycle_resolution: true,
            quest_complete: false,
            streak: false,
            replay_nudge: false,
          },
          oath_accepted: false,
        });
      }
      if (path === "/api/quests") {
        return json({
          daily: [
            {
              code: "no-trade",
              title: "Pass on purpose",
              detail: "Seal NO TRADE. Passing is Pack strength.",
              xp: 40,
              period_key: "2026-09-21",
              progress: 0,
              target: 1,
              completed: false,
              href: "/missions",
              cadence: "daily",
            },
            {
              code: "read-lock",
              title: "Read a mission and seal a call",
              detail: "Any of the four decisions counts. A pass counts too.",
              xp: 40,
              period_key: "2026-09-21",
              progress: 0,
              target: 1,
              completed: false,
              href: "/missions",
              cadence: "daily",
            },
            {
              code: "replay",
              title: "Run one closed tape",
              detail: "Train on a closed setup. The outcome stays masked until you reveal.",
              xp: 40,
              period_key: "2026-09-21",
              progress: 0,
              target: 1,
              completed: false,
              href: "/replay",
              cadence: "daily",
            },
          ],
          weekly: [],
        });
      }
      if (path === "/api/achievements") {
        return json({ unlocked: [] });
      }
      const decisionMatch = path.match(/^\/api\/missions\/([^/]+)\/decision$/);
      if (decisionMatch) {
        const id = decodeURIComponent(decisionMatch[1]);
        if (method === "GET") return json({ decision: decisions.get(id) ?? null });
        const body = JSON.parse(String(init?.body ?? "{}")) as { decision?: string };
        const existing = decisions.get(id);
        if (existing && body.decision && existing !== body.decision) {
          return json({ decision: existing, created: false, xp_awarded: 0, conflict: true }, 409);
        }
        if (existing) return json({ decision: existing, created: false, xp_awarded: 0, conflict: false });
        if (body.decision) decisions.set(id, body.decision);
        return json({ decision: body.decision ?? null, created: true, xp_awarded: 0, conflict: false });
      }
      const journalMatch = path.match(/^\/api\/missions\/([^/]+)\/journal$/);
      if (journalMatch) {
        const id = decodeURIComponent(journalMatch[1]);
        if (method === "GET") return json({ journal: journals.get(id) ?? null });
        const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        const existing = journals.get(id);
        if (existing) return json({ journal: existing, created: false, xp_awarded: 0 });
        journals.set(id, body);
        return json({ journal: body, created: true, xp_awarded: 30 });
      }
      if (path.endsWith("/replay") && method === "POST") {
        return json({ score: 0, created: true, xp_awarded: 0 });
      }
      return json({ detail: "not mocked" }, 404);
    }),
  );
});

afterEach(() => {
  cleanup();
  invalidatePackProfile();
  invalidateSession();
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.unstubAllGlobals();
});
