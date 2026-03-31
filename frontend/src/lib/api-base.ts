export const API_BASE =
  process.env.API_BASE_URL || "http://localhost:8000";

export function serverFetch(
  url: string,
  init?: RequestInit,
): Promise<Response> {
  const pw = process.env.AUTH_PASSWORD;
  if (!pw) return fetch(url, init);
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Basic ${btoa(`user:${pw}`)}`);
  return fetch(url, { ...init, headers });
}
