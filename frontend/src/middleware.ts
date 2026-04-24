import { NextRequest, NextResponse } from "next/server";
import { createServerClient } from "@supabase/ssr";
import { INVITE_COOKIE, verifyInviteToken } from "@/lib/invite";
import { updateSession } from "@/lib/supabase/middleware";

const API_BASE = process.env.API_BASE_URL || "http://localhost:8000";
const AUTH_DISABLED =
  process.env.AUTH_ENABLED === "0" || process.env.AUTH_ENABLED === "false";

async function getAccessToken(request: NextRequest): Promise<string | null> {
  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() {
          return request.cookies.getAll();
        },
        setAll() {
          // read-only
        },
      },
    },
  );
  const {
    data: { session },
  } = await supabase.auth.getSession();
  return session?.access_token ?? null;
}

function isWelcomePath(path: string): boolean {
  return path === "/welcome" || path.startsWith("/welcome/");
}

function isAuthFlowPath(path: string): boolean {
  return path === "/sign-in" || path.startsWith("/auth/");
}

export async function middleware(request: NextRequest) {
  const path = request.nextUrl.pathname;

  // Local-dev escape hatch: AUTH_ENABLED=0 drops the invite gate and the
  // session check, and forwards /api/* to the backend without a JWT. The
  // backend must agree (set AUTH_ENABLED=0 for the API too) or calls 401.
  if (AUTH_DISABLED) {
    if (path.startsWith("/api/")) {
      const target = new URL(
        `${request.nextUrl.pathname}${request.nextUrl.search}`,
        API_BASE,
      );
      return NextResponse.rewrite(target);
    }
    return NextResponse.next();
  }

  const inviteCookie = request.cookies.get(INVITE_COOKIE)?.value;

  // /api/* is proxied to the FastAPI backend. Require both gates: invite cookie
  // (so random traffic can't hit the backend) + Supabase session (so the
  // backend sees a valid JWT). Clients get 403 vs 401 to distinguish.
  if (path.startsWith("/api/")) {
    if (!(await verifyInviteToken(inviteCookie))) {
      return NextResponse.json({ error: "not_invited" }, { status: 403 });
    }
    const token = await getAccessToken(request);
    const headers = new Headers(request.headers);
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const target = new URL(
      `${request.nextUrl.pathname}${request.nextUrl.search}`,
      API_BASE,
    );
    return NextResponse.rewrite(target, { request: { headers } });
  }

  // The welcome gate is always reachable.
  if (isWelcomePath(path)) return NextResponse.next();

  // Everything else requires the invite cookie first.
  if (!(await verifyInviteToken(inviteCookie))) {
    const url = request.nextUrl.clone();
    url.pathname = "/welcome";
    if (path !== "/") url.searchParams.set("next", path);
    return NextResponse.redirect(url);
  }

  // Sign-in + OAuth callback don't need a Supabase session yet.
  if (isAuthFlowPath(path)) return NextResponse.next();

  // App pages: enforce Supabase session; `updateSession` also refreshes tokens.
  return updateSession(request);
}

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|healthz|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)",
  ],
};
