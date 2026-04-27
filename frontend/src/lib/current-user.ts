import { cache } from "react";
import type { AuthUserOut } from "@/api";
import { API_BASE, serverFetch } from "@/lib/api-base";

const AUTH_DISABLED =
  process.env.AUTH_ENABLED === "0" || process.env.AUTH_ENABLED === "false";

const ANONYMOUS_ADMIN: AuthUserOut = {
  user_id: "",
  email: "",
  is_admin: true,
};

const ANONYMOUS_USER: AuthUserOut = {
  user_id: "",
  email: "",
  is_admin: false,
};

export const getCurrentUser = cache(async (): Promise<AuthUserOut> => {
  if (AUTH_DISABLED) return ANONYMOUS_ADMIN;
  try {
    const res = await serverFetch(`${API_BASE}/api/auth/me`, {
      cache: "no-store",
    });
    if (!res.ok) return ANONYMOUS_USER;
    return (await res.json()) as AuthUserOut;
  } catch {
    return ANONYMOUS_USER;
  }
});

export async function isAdmin(): Promise<boolean> {
  const user = await getCurrentUser();
  return user.is_admin;
}
