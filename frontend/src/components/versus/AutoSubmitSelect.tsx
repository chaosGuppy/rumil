"use client";

import { useRouter } from "next/navigation";
import { useTransition } from "react";

type Option = { value: string; label: string };

/**
 * On change, builds the target URL from the parent form's action + all
 * its current field values and soft-navigates via Next's router. This
 * keeps the previously-painted page visible while the new server
 * components stream in — no blank flash on essay/filter switch.
 *
 * Falls back to a real form submit if the parent form can't be located
 * (defensive — shouldn't happen in practice).
 */
export function AutoSubmitSelect({
  name,
  defaultValue,
  options,
  groups,
  className,
  style,
  id,
}: {
  name: string;
  defaultValue?: string;
  options?: Option[];
  groups?: { label: string; options: Option[] }[];
  className?: string;
  style?: React.CSSProperties;
  id?: string;
}) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  return (
    <select
      id={id}
      name={name}
      defaultValue={defaultValue}
      className={className}
      style={{ ...style, opacity: isPending ? 0.6 : undefined }}
      disabled={isPending}
      onChange={(e) => {
        const form = e.currentTarget.form;
        if (!form) {
          e.currentTarget.form?.requestSubmit();
          return;
        }
        const fd = new FormData(form);
        const params = new URLSearchParams();
        for (const [k, v] of fd.entries()) {
          if (typeof v === "string" && v !== "") params.set(k, v);
        }
        const action = form.getAttribute("action") || form.action || window.location.pathname;
        const qs = params.toString();
        const url = qs ? `${action}?${qs}` : action;
        startTransition(() => {
          router.push(url);
        });
      }}
    >
      {groups
        ? groups.map((g) => (
            <optgroup key={g.label} label={g.label}>
              {g.options.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </optgroup>
          ))
        : (options ?? []).map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
    </select>
  );
}
