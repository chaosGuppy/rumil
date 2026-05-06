"use client";
import {
  Bar,
  BarChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { HistogramBin } from "@/api";

export function Histogram({
  bins,
  color,
  height = 110,
}: {
  bins: HistogramBin[];
  color?: string;
  height?: number;
}) {
  if (!bins || bins.length === 0) {
    return (
      <div
        style={{
          height,
          color: "var(--a-muted)",
          fontFamily: "var(--a-mono)",
          fontSize: "0.7rem",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        no data
      </div>
    );
  }
  const fill = color ?? "var(--a-accent)";
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart
        data={bins}
        margin={{ top: 4, right: 4, bottom: 0, left: 0 }}
      >
        <XAxis
          dataKey="label"
          tick={{
            fontFamily: "var(--a-mono)",
            fontSize: 9,
            fill: "var(--a-muted)",
          }}
          tickLine={false}
          axisLine={{ stroke: "var(--a-line-strong)" }}
          interval={0}
        />
        <YAxis
          tick={{
            fontFamily: "var(--a-mono)",
            fontSize: 9,
            fill: "var(--a-muted)",
          }}
          tickLine={false}
          axisLine={false}
          width={22}
          allowDecimals={false}
        />
        <Tooltip
          cursor={{ fill: "var(--a-accent-soft)" }}
          contentStyle={{
            background: "var(--a-bg-paper)",
            border: "1px solid var(--a-line-strong)",
            borderRadius: 2,
            fontFamily: "var(--a-mono)",
            fontSize: "0.7rem",
            padding: "0.3rem 0.5rem",
            color: "var(--a-fg)",
          }}
          labelFormatter={((label: unknown) => `bin · ${label}`) as never}
          formatter={((v: unknown) => [`${v}`, "count"]) as never}
        />
        <Bar dataKey="count" fill={fill} isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  );
}
