import { NextResponse, type NextRequest } from "next/server";
import { createClient } from "@/lib/supabase/server";

// Standalone Next.js builds bind to HOSTNAME=0.0.0.0:3000, so
// `request.nextUrl.origin` resolves to the pod's internal address rather
// than the public origin the browser came from. Behind a load balancer
// (GKE Gateway) we need to read the original host from `x-forwarded-host`
// before emitting the redirect, otherwise the browser ends up at
// `https://0.0.0.0:3000/`. Locally the header is absent and we fall
// back to `nextUrl.origin`.
function publicOrigin(request: NextRequest): string {
  const forwardedHost = request.headers.get("x-forwarded-host");
  if (forwardedHost) {
    const proto = request.headers.get("x-forwarded-proto") ?? "https";
    return `${proto}://${forwardedHost}`;
  }
  return request.nextUrl.origin;
}

export async function GET(request: NextRequest) {
  const url = request.nextUrl;
  const code = url.searchParams.get("code");
  const next = url.searchParams.get("next") ?? "/";
  const origin = publicOrigin(request);

  if (code) {
    const supabase = await createClient();
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (!error) {
      return NextResponse.redirect(new URL(next, origin));
    }
  }

  const fallback = new URL("/sign-in", origin);
  fallback.searchParams.set("error", "oauth_failed");
  return NextResponse.redirect(fallback);
}
