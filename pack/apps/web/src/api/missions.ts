import type { Mission } from "./types";

export class MissionNotFoundError extends Error {
  constructor(id: string) {
    super(`Mission ${id} is not in the fixture set.`);
    this.name = "MissionNotFoundError";
  }
}

function apiBase(): string {
  return (import.meta.env.VITE_API_BASE ?? "").replace(/\/$/, "");
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  return JSON.parse(text) as unknown;
}

export async function fetchMissions(): Promise<Mission[]> {
  const response = await fetch(`${apiBase()}/api/missions`);
  if (!response.ok) {
    throw new Error(`Missions request failed (${response.status}).`);
  }
  const body = (await readJson(response)) as { missions?: unknown };
  if (!body || !Array.isArray(body.missions)) {
    throw new Error("Missions response did not include the fixture list.");
  }
  return body.missions as Mission[];
}

export async function fetchMission(id: string): Promise<Mission> {
  const response = await fetch(`${apiBase()}/api/missions/${encodeURIComponent(id)}`);
  if (response.status === 404) throw new MissionNotFoundError(id);
  if (!response.ok) throw new Error(`Mission request failed (${response.status}).`);
  const body = (await readJson(response)) as Mission | null;
  if (!body || !Array.isArray(body.lifecycle)) {
    throw new Error("Mission response did not include lifecycle events.");
  }
  return body;
}
