import { telegramBridge } from "../telegram/TelegramBridge";

const TOKEN_KEY = "cci-pack.session.v1";
let pending: Promise<string | null> | null = null;

export function invalidateSession(): void {
  pending = null;
  sessionStorage.removeItem(TOKEN_KEY);
}

export async function ensureSession(): Promise<string | null> {
  const existing = sessionStorage.getItem(TOKEN_KEY);
  if (existing) return existing;
  if (!pending) {
    pending = openSession().finally(() => {
      pending = null;
    });
  }
  return pending;
}

async function openSession(): Promise<string | null> {
  const initData = telegramBridge.initData();
  const response = await fetch(initData ? "/api/auth/telegram" : "/api/auth/dev", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(initData ? { init_data: initData } : {}),
  });
  if (!response.ok) return null;
  const body = (await response.json()) as { token?: string };
  if (!body.token) return null;
  sessionStorage.setItem(TOKEN_KEY, body.token);
  return body.token;
}

export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const token = await ensureSession();
  const headers = new Headers(init?.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init?.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  return fetch(path, { ...init, headers });
}
