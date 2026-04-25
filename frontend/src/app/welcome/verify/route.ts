import { NextResponse, type NextRequest } from "next/server";
import { INVITE_COOKIE, signInviteToken } from "@/lib/invite";

export async function POST(request: NextRequest) {
  const expected = process.env.INVITE_PASSWORD;
  if (!expected) {
    return NextResponse.json(
      { ok: false, reason: "not_configured" },
      { status: 503 },
    );
  }

  let password = "";
  try {
    const body = await request.json();
    if (typeof body?.password === "string") password = body.password;
  } catch {
    // fall through
  }

  if (password !== expected) {
    return NextResponse.json({ ok: false }, { status: 401 });
  }

  const { value, maxAge } = await signInviteToken();
  const res = NextResponse.json({ ok: true });
  res.cookies.set(INVITE_COOKIE, value, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge,
  });
  return res;
}
