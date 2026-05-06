"use client";
import { useEffect, useRef, useState } from "react";
import type { LiveRunSnapshot } from "@/api";
import { OverlayStages } from "../../../../../_components/OverlayStages";
import { fmtRelative } from "../../../../../_lib/format";

const POLL_MS = 2000;

export function LiveRunner({
  initial,
  apiUrl,
}: {
  initial: LiveRunSnapshot;
  apiUrl: string;
}) {
  const [snap, setSnap] = useState<LiveRunSnapshot>(initial);
  const stoppedRef = useRef<boolean>(!initial.is_in_flight);

  useEffect(() => {
    let cancelled = false;
    let timeout: ReturnType<typeof setTimeout> | null = null;

    async function tick() {
      if (cancelled || stoppedRef.current) return;
      try {
        const res = await fetch(apiUrl, { cache: "no-store" });
        if (res.ok) {
          const next = (await res.json()) as LiveRunSnapshot;
          if (!cancelled) setSnap(next);
          if (!next.is_in_flight) {
            stoppedRef.current = true;
            return;
          }
        }
      } catch {
        // swallow — try again next tick
      }
      if (!cancelled && !stoppedRef.current) {
        timeout = setTimeout(tick, POLL_MS);
      }
    }

    if (!stoppedRef.current) {
      timeout = setTimeout(tick, POLL_MS);
    }
    return () => {
      cancelled = true;
      if (timeout) clearTimeout(timeout);
    };
  }, [apiUrl]);

  const overlay = snap.overlay;
  const live = !!snap.is_in_flight;

  return (
    <>
      <div className={`atlas-live-banner ${live ? "" : "is-stopped"}`}>
        <span className={`atlas-live-pulse ${live ? "" : "is-stopped"}`} />
        <span style={{ color: "var(--a-rule)", letterSpacing: "0.04em" }}>
          {live ? "live · polling" : "settled · polling stopped"}
        </span>
        <div className="atlas-live-meta">
          <span>
            pending · <strong>{snap.n_pending_calls ?? 0}</strong>
          </span>
          <span>
            running · <strong>{snap.n_running_calls ?? 0}</strong>
          </span>
          <span>
            last event · <strong>{fmtRelative(snap.last_event_ts)}</strong>
          </span>
          <span>
            snapshot · <strong>{fmtRelative(snap.snapshot_ts)}</strong>
          </span>
        </div>
      </div>

      {overlay ? (
        <OverlayStages overlay={overlay} currentStageId={snap.current_stage_id} />
      ) : (
        <div className="atlas-empty">
          <strong>no overlay yet</strong>
          waiting for the first stage to land.
        </div>
      )}
    </>
  );
}
