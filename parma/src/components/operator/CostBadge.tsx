export function CostBadge({ cost }: { cost: number }) {
  // Values are USD per rumil/pricing.py. Always prefix with `$` so the unit
  // is unambiguous — at a glance the reader shouldn't have to guess whether
  // a bare "0.012" means dollars, cents, or something else.
  const formatted =
    cost < 0.001
      ? "$<0.001"
      : cost < 1
        ? `$${cost.toFixed(3)}`
        : `$${cost.toFixed(2)}`;

  const tier = cost < 0.01 ? "low" : cost < 0.10 ? "mid" : "high";

  return <span className={`op-cost op-cost-${tier}`} title={`${cost.toFixed(6)} USD`}>{formatted}</span>;
}
