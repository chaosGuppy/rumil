"use client";

import { useState } from "react";
import type { AbRunTraceOut, AbRunArmOut } from "@/api/types.gen";
import { CallNode, HashTargetProvider, callTraceToTreeNode } from "../../traces/[runId]/call-node";
import "./ab-trace.css";

function ConfigDiff({ arms }: { arms: AbRunArmOut[] }) {
  if (arms.length < 2) return null;
  const configA = arms[0].config ?? {};
  const configB = arms[1].config ?? {};

  const allKeys = Array.from(
    new Set([...Object.keys(configA), ...Object.keys(configB)])
  ).sort();

  if (allKeys.length === 0) return null;

  const hasDiff = allKeys.some(
    (k) => JSON.stringify(configA[k]) !== JSON.stringify(configB[k])
  );

  return (
    <div className="ab-config-section">
      <div className="ab-config-label">configuration</div>
      <div className="ab-config-table">
        <div className="ab-config-row ab-config-header-row">
          <span className="ab-config-key">param</span>
          <span className="ab-config-val">arm a</span>
          <span className="ab-config-val">arm b</span>
        </div>
        {allKeys.map((k) => {
          const a = String(configA[k] ?? "—");
          const b = String(configB[k] ?? "—");
          const differs = a !== b;
          return (
            <div
              key={k}
              className={`ab-config-row ${differs ? "ab-config-diff" : ""}`}
            >
              <span className="ab-config-key">{k}</span>
              <span className={`ab-config-val ${differs ? "ab-config-val-diff" : ""}`}>
                {a}
              </span>
              <span className={`ab-config-val ${differs ? "ab-config-val-diff" : ""}`}>
                {b}
              </span>
            </div>
          );
        })}
      </div>
      {!hasDiff && (
        <div className="ab-config-same">identical configuration</div>
      )}
    </div>
  );
}

function CostComparison({ arms }: { arms: AbRunArmOut[] }) {
  if (arms.length < 2) return null;
  const costA = arms[0].trace.cost_usd;
  const costB = arms[1].trace.cost_usd;
  if (costA == null && costB == null) return null;

  return (
    <div className="ab-cost-comparison">
      <span className="ab-cost-item">
        <span className="ab-cost-label">A</span>
        <span className="ab-cost-value">
          {costA != null ? `$${costA.toFixed(4)}` : "—"}
        </span>
      </span>
      <span className="ab-cost-separator">vs</span>
      <span className="ab-cost-item">
        <span className="ab-cost-label">B</span>
        <span className="ab-cost-value">
          {costB != null ? `$${costB.toFixed(4)}` : "—"}
        </span>
      </span>
    </div>
  );
}

export function ABTraceViewer({ trace }: { trace: AbRunTraceOut }) {
  const [syncScroll, setSyncScroll] = useState(false);
  const arms = trace.arms;

  return (
    <div className="ab-viewer">
      <ConfigDiff arms={arms} />
      <CostComparison arms={arms} />

      <div className="ab-controls">
        <label className="ab-sync-toggle">
          <input
            type="checkbox"
            checked={syncScroll}
            onChange={(e) => setSyncScroll(e.target.checked)}
          />
          <span className="ab-sync-label">sync scroll</span>
        </label>
      </div>

      <div
        className="ab-panels"
        onScroll={
          syncScroll
            ? (e) => {
                const target = e.target as HTMLElement;
                const panels = target.parentElement?.querySelectorAll(".ab-panel");
                panels?.forEach((p) => {
                  if (p !== target) {
                    (p as HTMLElement).scrollTop = target.scrollTop;
                  }
                });
              }
            : undefined
        }
      >
        {arms.map((arm, i) => {
          const label = i === 0 ? "A" : "B";
          return (
            <div key={arm.run_id} className="ab-panel">
              <div className="ab-panel-header">
                <span className="ab-arm-label">Arm {label}</span>
                <span className="ab-arm-id">{arm.run_id.slice(0, 8)}</span>
                {arm.trace.cost_usd != null && (
                  <span className="ab-arm-cost">
                    ${arm.trace.cost_usd.toFixed(4)}
                  </span>
                )}
              </div>
              <div
                className="ab-panel-content"
                onScroll={
                  syncScroll
                    ? (e) => {
                        const target = e.currentTarget;
                        const sibling = target
                          .closest(".ab-panels")
                          ?.querySelectorAll(".ab-panel-content");
                        sibling?.forEach((s) => {
                          if (s !== target) {
                            (s as HTMLElement).scrollTop = target.scrollTop;
                          }
                        });
                      }
                    : undefined
                }
              >
                <HashTargetProvider>
                  <div className="trace-root">
                    {arm.trace.root_calls.map((ct) => (
                      <CallNode key={ct.call.id} tree={callTraceToTreeNode(ct)} depth={0} />
                    ))}
                    {arm.trace.root_calls.length === 0 && (
                      <p className="trace-empty">
                        No calls recorded for this arm yet.
                      </p>
                    )}
                  </div>
                </HashTargetProvider>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
