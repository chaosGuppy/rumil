"use client";

import { useEffect, useState } from "react";
import type { AuthUserOut } from "@/api";
import { CLIENT_API_BASE as API_BASE } from "@/api-config";

const ANONYMOUS: AuthUserOut = { user_id: "", email: "", is_admin: false };

export function useCurrentUser(): AuthUserOut | null {
  const [user, setUser] = useState<AuthUserOut | null>(null);
  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/api/auth/me`, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : ANONYMOUS))
      .then((data: AuthUserOut) => {
        if (!cancelled) setUser(data);
      })
      .catch(() => {
        if (!cancelled) setUser(ANONYMOUS);
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return user;
}
