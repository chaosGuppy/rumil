export function fmtCost(v: number | null | undefined): string {
  if (v == null) return "—";
  if (v >= 1) return `$${v.toFixed(2)}`;
  if (v >= 0.01) return `$${v.toFixed(3)}`;
  if (v > 0) return `$${v.toFixed(4)}`;
  return "$0";
}

export function fmtDuration(s: number | null | undefined): string {
  if (s == null) return "—";
  if (s < 60) return `${s.toFixed(0)}s`;
  if (s < 3600) return `${(s / 60).toFixed(1)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

export function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString("en-US", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const ms = Date.now() - new Date(iso).getTime();
    if (ms < 60_000) return `${Math.max(0, Math.floor(ms / 1000))}s ago`;
    if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
    if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
    if (ms < 30 * 86_400_000) return `${Math.floor(ms / 86_400_000)}d ago`;
    const d = new Date(iso);
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  } catch {
    return iso;
  }
}

export function fmtDelta(
  a: number | null | undefined,
  b: number | null | undefined,
  fmt: (n: number) => string = (n) => `${n}`,
): { label: string; cls: "is-positive" | "is-negative" | "is-flat" } {
  const av = a ?? 0;
  const bv = b ?? 0;
  const d = bv - av;
  if (d === 0) return { label: "·", cls: "is-flat" };
  const sign = d > 0 ? "+" : "−";
  return {
    label: `${sign}${fmt(Math.abs(d))}`,
    cls: d > 0 ? "is-positive" : "is-negative",
  };
}

export function isUuidLike(s: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(s);
}
