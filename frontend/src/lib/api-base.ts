import { createClient } from "@/lib/supabase/server";

export const API_BASE = process.env.API_BASE_URL || "http://localhost:8000";

const AUTH_DISABLED =
  process.env.AUTH_ENABLED === "0" || process.env.AUTH_ENABLED === "false";

export async function serverFetch(
  url: string,
  init?: RequestInit,
): Promise<Response> {
  if (AUTH_DISABLED) return fetch(url, init);

  const supabase = await createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();

  const headers = new Headers(init?.headers);
  if (session?.access_token) {
    headers.set("Authorization", `Bearer ${session.access_token}`);
  }
  return fetch(url, { ...init, headers });
}
