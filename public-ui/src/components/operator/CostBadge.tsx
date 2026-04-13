export function CostBadge({ cost }: { cost: number }) {
  const formatted =
    cost < 0.001
      ? "< 0.1\u00a2"
      : cost < 0.01
        ? `${(cost * 100).toFixed(1)}\u00a2`
        : cost < 1
          ? `$${cost.toFixed(3)}`
          : `$${cost.toFixed(2)}`;

  const tier = cost < 0.01 ? "low" : cost < 0.10 ? "mid" : "high";

  return <span className={`op-cost op-cost-${tier}`}>{formatted}</span>;
}
