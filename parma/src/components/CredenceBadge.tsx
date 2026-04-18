"use client";

import { useState, useRef, useEffect } from "react";

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

const CREDENCE_SCALE: Array<[number, string]> = [
  [1, "vanishingly unlikely (<1%)"],
  [2, "very unlikely (1-5%)"],
  [3, "unlikely (5-20%)"],
  [4, "leaning against (20-40%)"],
  [5, "genuinely uncertain (40-60%)"],
  [6, "leaning toward (60-80%)"],
  [7, "likely (80-95%)"],
  [8, "very likely (95-99%)"],
  [9, "near-certain (>99%)"],
];

const ROBUSTNESS_SCALE: Array<[number, string]> = [
  [1, "fragile — one datum away from flipping"],
  [2, "thin — rests on a narrow base of evidence"],
  [3, "moderate — stable against routine updates"],
  [4, "solid — would take substantial evidence to move"],
  [5, "locked in — effectively settled"],
];

function credenceGloss(n: number): string {
  const row = CREDENCE_SCALE.find(([k]) => k === n);
  return row ? row[1] : "out of range";
}

function robustnessGloss(n: number): string {
  const row = ROBUSTNESS_SCALE.find(([k]) => k === n);
  return row ? row[1] : "out of range";
}

function formatTitle(
  credence: number | null,
  robustness: number | null,
): string {
  const parts: string[] = [];
  if (credence !== null) {
    parts.push(`Credence ${credence}/9 — ${credenceGloss(credence)}`);
  }
  if (robustness !== null) {
    parts.push(`Robustness ${robustness}/5 — ${robustnessGloss(robustness)}`);
  }
  return parts.join(" · ");
}

function BadgeLegend({ onClose }: { onClose: () => void }) {
  const popoverRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (
        popoverRef.current &&
        !popoverRef.current.contains(e.target as Node)
      ) {
        onClose();
      }
    }
    function onEsc(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onEsc);
    };
  }, [onClose]);

  return (
    <div
      ref={popoverRef}
      className="credence-legend-popover"
      role="dialog"
      aria-label="Epistemic badge legend"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="credence-legend-section">
        <div className="credence-legend-heading">
          Credence <span className="credence-legend-scale">0–9</span>
        </div>
        <div className="credence-legend-sub">
          How likely the claim is true, as a probability bucket.
        </div>
        <ul className="credence-legend-list">
          {CREDENCE_SCALE.map(([n, gloss]) => (
            <li key={n}>
              <span className="credence-legend-num">{n}</span>
              <span className="credence-legend-text">{gloss}</span>
            </li>
          ))}
        </ul>
      </div>
      <div className="credence-legend-section">
        <div className="credence-legend-heading">
          Robustness <span className="credence-legend-scale">1–5</span>
        </div>
        <div className="credence-legend-sub">
          How resilient the credence is to new information.
        </div>
        <ul className="credence-legend-list">
          {ROBUSTNESS_SCALE.map(([n, gloss]) => (
            <li key={n}>
              <span className="credence-legend-num">{n}</span>
              <span className="credence-legend-text">{gloss}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

export function CredenceBadge({ credence, robustness }: CredenceBadgeProps) {
  const [legendOpen, setLegendOpen] = useState(false);

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
      <span className="credence-help-wrapper">
        <button
          type="button"
          className="credence-help-btn"
          aria-label="Show epistemic scale legend"
          aria-expanded={legendOpen}
          onClick={(e) => {
            e.stopPropagation();
            setLegendOpen((v) => !v);
          }}
        >
          ?
        </button>
        {legendOpen && <BadgeLegend onClose={() => setLegendOpen(false)} />}
      </span>
    </span>
  );
}
