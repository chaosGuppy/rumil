"use client";
import {
  Area,
  AreaChart,
  ResponsiveContainer,
  Tooltip,
} from "recharts";

type Point = { i: number; v: number; label?: string };

export function Sparkline({
  values,
  labels,
  color,
}: {
  values: number[];
  labels?: string[];
  color?: string;
}) {
  if (!values || values.length === 0) {
    return (
      <div
        style={{
          height: 36,
          color: "var(--a-muted)",
          fontFamily: "var(--a-mono)",
          fontSize: "0.7rem",
          display: "flex",
          alignItems: "center",
        }}
      >
        no data
      </div>
    );
  }
  const data: Point[] = values.map((v, i) => ({
    i,
    v,
    label: labels?.[i],
  }));
  const stroke = color ?? "var(--a-accent)";
  const id = `spark-${Math.random().toString(36).slice(2, 8)}`;
  return (
    <ResponsiveContainer width="100%" height={36}>
      <AreaChart data={data} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
        <defs>
          <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity={0.45} />
            <stop offset="100%" stopColor={stroke} stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <Tooltip
          cursor={{ stroke: "var(--a-line-strong)", strokeWidth: 1 }}
          contentStyle={{
            background: "var(--a-bg-paper)",
            border: "1px solid var(--a-line-strong)",
            borderRadius: 2,
            fontFamily: "var(--a-mono)",
            fontSize: "0.72rem",
            padding: "0.3rem 0.5rem",
            color: "var(--a-fg)",
          }}
          labelFormatter={() => ""}
          formatter={((v: unknown, _n: unknown, item: { payload?: Point }) => {
            const p = item?.payload;
            const label = p?.label ? ` · ${p.label}` : "";
            return [`${v}${label}`, ""] as [string, string];
          }) as never}
        />
        <Area
          type="monotone"
          dataKey="v"
          stroke={stroke}
          strokeWidth={1.4}
          fill={`url(#${id})`}
          isAnimationActive={false}
          dot={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
