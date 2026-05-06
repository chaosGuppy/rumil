import { API_BASE, serverFetch } from "@/lib/api-base";

export async function atlasFetch<T>(
  path: string,
  fallback: T,
): Promise<T> {
  try {
    const res = await serverFetch(`${API_BASE}${path}`, { cache: "no-store" });
    if (!res.ok) return fallback;
    return (await res.json()) as T;
  } catch {
    return fallback;
  }
}
