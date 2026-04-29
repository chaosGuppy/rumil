import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";
import { isAdminPath } from "@/lib/admin-paths";

const PUBLIC_PREFIXES = ["/sign-in", "/auth/"];
const PUBLIC_PATHS = new Set(["/healthz"]);

const API_BASE = process.env.API_BASE_URL || "http://localhost:8000";

async function isAdminUser(accessToken: string | undefined): Promise<boolean> {
  if (!accessToken) return false;
  try {
    const res = await fetch(`${API_BASE}/api/auth/me`, {
      headers: { Authorization: `Bearer ${accessToken}` },
      cache: "no-store",
    });
    if (!res.ok) return false;
    const data = (await res.json()) as { is_admin?: boolean };
    return Boolean(data.is_admin);
  } catch {
    return false;
  }
}

export async function updateSession(request: NextRequest) {
  let response = NextResponse.next({ request });

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() {
          return request.cookies.getAll();
        },
        setAll(cookiesToSet) {
          cookiesToSet.forEach(({ name, value }) => {
            request.cookies.set(name, value);
          });
          response = NextResponse.next({ request });
          cookiesToSet.forEach(({ name, value, options }) => {
            response.cookies.set(name, value, options);
          });
        },
      },
    },
  );

  const {
    data: { user },
  } = await supabase.auth.getUser();

  const path = request.nextUrl.pathname;
  const isPublic =
    PUBLIC_PATHS.has(path) ||
    PUBLIC_PREFIXES.some((p) => path.startsWith(p));

  if (!user && !isPublic) {
    const url = request.nextUrl.clone();
    url.pathname = "/sign-in";
    if (path !== "/") url.searchParams.set("next", path);
    return NextResponse.redirect(url);
  }

  if (user && isAdminPath(path)) {
    const {
      data: { session },
    } = await supabase.auth.getSession();
    const ok = await isAdminUser(session?.access_token);
    if (!ok) {
      const url = request.nextUrl.clone();
      url.pathname = "/";
      url.search = "";
      return NextResponse.redirect(url);
    }
  }

  return response;
}
