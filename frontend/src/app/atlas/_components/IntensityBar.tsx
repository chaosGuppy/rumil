/**
 * Two stacked use-intensity bars for index rows.
 *
 * Top bar: ``recent`` (the user's "what's hot right now" signal).
 * Bottom bar (thinner, dimmer): ``lifetime`` (so historically-heavy
 * but currently-cold types still rank fairly).
 *
 * Either prop can be omitted: omitting ``lifetime`` is fine for
 * surfaces where lifetime isn't tractable (e.g. moves), and
 * omitting ``recent`` is fine where recent isn't natural (e.g. page
 * type counts). When both are zero/absent, renders a muted "no
 * recent activity" stub instead.
 */
export function IntensityBar({
  recent,
  recentMax,
  recentLabel = "recent",
  lifetime,
  lifetimeMax,
  lifetimeLabel = "lifetime",
}: {
  recent?: number | null;
  recentMax?: number;
  recentLabel?: string;
  lifetime?: number | null;
  lifetimeMax?: number;
  lifetimeLabel?: string;
}) {
  const r = recent ?? 0;
  const l = lifetime ?? 0;
  const rMax = Math.max(1, recentMax ?? 0);
  const lMax = Math.max(1, lifetimeMax ?? 0);
  const rPct = r > 0 ? Math.max(2, Math.round((100 * r) / rMax)) : 0;
  const lPct = l > 0 ? Math.max(2, Math.round((100 * l) / lMax)) : 0;

  if (recent == null && lifetime == null) {
    return (
      <span
        style={{
          color: "var(--a-muted)",
          fontFamily: "var(--a-mono)",
          fontSize: "0.74rem",
        }}
      >
        no recorded activity
      </span>
    );
  }
  if (r === 0 && l === 0) {
    return (
      <span
        style={{
          color: "var(--a-muted)",
          fontFamily: "var(--a-mono)",
          fontSize: "0.74rem",
        }}
      >
        no recorded activity
      </span>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
      {recent != null && (
        <div
          style={{
            position: "relative",
            height: "0.45rem",
            width: "100%",
            maxWidth: "16rem",
            background: "var(--a-line)",
            borderRadius: "2px",
            overflow: "hidden",
          }}
          title={`${r} ${recentLabel}`}
        >
          <div
            style={{
              position: "absolute",
              inset: 0,
              width: `${rPct}%`,
              background: "var(--a-accent)",
              opacity: 0.7,
            }}
          />
        </div>
      )}
      {lifetime != null && (
        <div
          style={{
            position: "relative",
            height: "0.3rem",
            width: "100%",
            maxWidth: "16rem",
            background: "var(--a-line)",
            borderRadius: "2px",
            overflow: "hidden",
          }}
          title={`${l} ${lifetimeLabel}`}
        >
          <div
            style={{
              position: "absolute",
              inset: 0,
              width: `${lPct}%`,
              background: "var(--a-rule)",
              opacity: 0.5,
            }}
          />
        </div>
      )}
    </div>
  );
}
