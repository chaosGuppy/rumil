"use client";

import { useState, useRef, useEffect, useLayoutEffect } from "react";
import { createPortal } from "react-dom";

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

const POPOVER_WIDTH = 280;
const POPOVER_GAP = 6;
const VIEWPORT_MARGIN = 8;

function BadgeLegend({
  triggerEl,
  onClose,
}: {
  triggerEl: HTMLElement;
  onClose: () => void;
}) {
  const popoverRef = useRef<HTMLDivElement>(null);
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(
    null,
  );

  useLayoutEffect(() => {
    function update() {
      const rect = triggerEl.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const popoverH = popoverRef.current?.offsetHeight ?? 0;
      let left = rect.right - POPOVER_WIDTH;
      if (left < VIEWPORT_MARGIN) left = VIEWPORT_MARGIN;
      if (left + POPOVER_WIDTH > vw - VIEWPORT_MARGIN) {
        left = vw - VIEWPORT_MARGIN - POPOVER_WIDTH;
      }
      let top = rect.bottom + POPOVER_GAP;
      if (popoverH > 0 && top + popoverH > vh - VIEWPORT_MARGIN) {
        const aboveTop = rect.top - POPOVER_GAP - popoverH;
        if (aboveTop >= VIEWPORT_MARGIN) top = aboveTop;
      }
      setCoords({ top, left });
    }
    update();
    const ro = new ResizeObserver(update);
    if (popoverRef.current) ro.observe(popoverRef.current);
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [triggerEl]);

  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      const target = e.target as Node;
      if (
        popoverRef.current &&
        !popoverRef.current.contains(target) &&
        !triggerEl.contains(target)
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
  }, [onClose, triggerEl]);

  return createPortal(
    <div
      ref={popoverRef}
      className="credence-legend-popover"
      role="dialog"
      aria-label="Epistemic badge legend"
      onClick={(e) => e.stopPropagation()}
      style={{
        top: coords?.top ?? 0,
        left: coords?.left ?? 0,
        visibility: coords ? "visible" : "hidden",
      }}
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
    </div>,
    document.body,
  );
}

export function CredenceBadge({ credence, robustness }: CredenceBadgeProps) {
  const [legendOpen, setLegendOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);

  if (credence === null && robustness === null) return null;

  return (
    <span
      className="credence-badge"
      title={formatTitle(credence, robustness)}
    >
      {credence !== null && (
        <span
          className="credence-item"
          title={`Credence ${credence}/9 — ${credenceGloss(credence)}`}
        >
          <span className="credence-label">
            {credence}<span className="credence-max">/9</span>
          </span>
          <ThinBar value={credence} max={9} color="var(--credence-fill)" />
        </span>
      )}
      {robustness !== null && (
        <span
          className="credence-item"
          title={`Robustness ${robustness}/5 — ${robustnessGloss(robustness)}`}
        >
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
          ref={buttonRef}
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
        {legendOpen && buttonRef.current && (
          <BadgeLegend
            triggerEl={buttonRef.current}
            onClose={() => setLegendOpen(false)}
          />
        )}
      </span>
    </span>
  );
}
