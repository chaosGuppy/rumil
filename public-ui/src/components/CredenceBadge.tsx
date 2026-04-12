"use client";

interface CredenceBadgeProps {
  credence: number | null;
  robustness: number | null;
}

function ThinBar({
  value,
  max,
  color,
  width = 36,
}: {
  value: number;
  max: number;
  color: string;
  width?: number;
}) {
  const filled = (value / max) * width;
  return (
    <span
      style={{
        display: "inline-block",
        width: `${width}px`,
        height: "3px",
        background: "var(--credence-empty)",
        borderRadius: "1px",
        position: "relative",
        verticalAlign: "middle",
      }}
    >
      <span
        style={{
          position: "absolute",
          left: 0,
          top: 0,
          height: "3px",
          width: `${filled}px`,
          background: color,
          borderRadius: "1px",
        }}
      />
    </span>
  );
}

export function CredenceBadge({ credence, robustness }: CredenceBadgeProps) {
  if (credence === null && robustness === null) return null;

  return (
    <span
      className="credence-badge"
      title={formatTitle(credence, robustness)}
    >
      {credence !== null && (
        <span className="credence-item">
          <span className="credence-label">
            {credence}<span className="credence-max">/9</span>
          </span>
          <ThinBar value={credence} max={9} color="var(--credence-fill)" />
        </span>
      )}
      {robustness !== null && (
        <span className="credence-item">
          <span className="credence-label">
            {robustness}<span className="credence-max">/5</span>
          </span>
          <ThinBar
            value={robustness}
            max={5}
            color="var(--robustness-fill)"
            width={24}
          />
        </span>
      )}
    </span>
  );
}

function formatTitle(
  credence: number | null,
  robustness: number | null,
): string {
  const parts: string[] = [];
  if (credence !== null) parts.push(`Credence: ${credence}/9`);
  if (robustness !== null) parts.push(`Robustness: ${robustness}/5`);
  return parts.join(" · ");
}
