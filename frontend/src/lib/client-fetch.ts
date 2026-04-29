"use client";

import { createClient } from "@/lib/supabase/client";

export async function clientFetch(
  url: string,
  init?: RequestInit,
): Promise<Response> {
  const supabase = createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();
  const headers = new Headers(init?.headers);
  if (session?.access_token) {
    headers.set("Authorization", `Bearer ${session.access_token}`);
  }
  return fetch(url, { ...init, headers });
}
