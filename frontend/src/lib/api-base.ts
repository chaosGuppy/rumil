import { createClient } from "@/lib/supabase/server";

export const API_BASE = process.env.API_BASE_URL || "http://localhost:8000";

export async function serverFetch(
  url: string,
  init?: RequestInit,
): Promise<Response> {
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
