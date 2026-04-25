// Invite gate: a server-signed HMAC cookie that proves the visitor entered
// the shared invite password. The Supabase session is layered on top once
// the visitor proceeds to /sign-in.

export const INVITE_COOKIE = "rumil_invite";
const INVITE_TTL_SECONDS = 60 * 60 * 24 * 30;

function getSecret(): string {
  const s = process.env.INVITE_SECRET;
  if (!s) throw new Error("INVITE_SECRET must be set");
  return s;
}

function toHex(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let out = "";
  for (let i = 0; i < bytes.length; i++) out += bytes[i].toString(16).padStart(2, "0");
  return out;
}

async function hmac(data: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(getSecret()),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(data));
  return toHex(sig);
}

export async function signInviteToken(): Promise<{ value: string; maxAge: number }> {
  const exp = Math.floor(Date.now() / 1000) + INVITE_TTL_SECONDS;
  const payload = `v1.${exp}`;
  const sig = await hmac(payload);
  return { value: `${payload}.${sig}`, maxAge: INVITE_TTL_SECONDS };
}

export async function verifyInviteToken(value: string | undefined): Promise<boolean> {
  if (!value) return false;
  const parts = value.split(".");
  if (parts.length !== 3) return false;
  const [v, expStr, sig] = parts;
  if (v !== "v1") return false;
  const exp = Number(expStr);
  if (!Number.isFinite(exp) || exp * 1000 < Date.now()) return false;
  const expected = await hmac(`v1.${expStr}`);
  if (expected.length !== sig.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) diff |= expected.charCodeAt(i) ^ sig.charCodeAt(i);
  return diff === 0;
}
