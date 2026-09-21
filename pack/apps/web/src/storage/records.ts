export const PACK_EVENT = "pack-records";

export function emitRecords(): void {
  window.dispatchEvent(new Event(PACK_EVENT));
}

export function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

export function writeJson(key: string, value: unknown): void {
  window.localStorage.setItem(key, JSON.stringify(value));
  emitRecords();
}
