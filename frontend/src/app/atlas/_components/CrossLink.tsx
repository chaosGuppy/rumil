import Link from "next/link";
import type { ReactNode } from "react";

type Kind = "rumil" | "atlas" | "external";

type Props = {
  to: string;
  kind?: Kind;
  chip?: boolean;
  compact?: boolean;
  className?: string;
  children?: ReactNode;
};

/**
 * Link from atlas pages out to other rumil surfaces (or to atlas-internal
 * destinations). Outbound links get a distinct visual treatment — dashed
 * border + warm tone + ↗ glyph — driven by --a-link-out-* CSS vars in
 * atlas.css. Atlas-internal links render as a normal next/link so they
 * inherit existing chip / row / inline styling.
 *
 * Default kind detection: starts with "/atlas/" → atlas; starts with "/" →
 * rumil; otherwise external.
 */
function detectKind(to: string): Kind {
  if (to.startsWith("/atlas/") || to === "/atlas") return "atlas";
  if (to.startsWith("/")) return "rumil";
  return "external";
}

export function CrossLink({
  to,
  kind,
  chip = false,
  compact = false,
  className,
  children,
}: Props) {
  const resolved: Kind = kind ?? detectKind(to);
  const isOutbound = resolved !== "atlas";

  const classes: string[] = [];
  if (chip) classes.push("atlas-chip");
  if (isOutbound) classes.push("atlas-xlink");
  if (compact) classes.push("is-compact");
  if (className) classes.push(className);
  const cls = classes.join(" ") || undefined;

  // External (non-rumil) destinations render as a plain anchor with a new tab.
  if (resolved === "external") {
    return (
      <a href={to} className={cls} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    );
  }

  return (
    <Link href={to} className={cls}>
      {children}
    </Link>
  );
}
