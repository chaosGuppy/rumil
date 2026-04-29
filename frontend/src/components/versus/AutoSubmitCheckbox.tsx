"use client";

import { useRouter } from "next/navigation";
import { useTransition } from "react";

export function AutoSubmitCheckbox({
  name,
  value = "true",
  defaultChecked,
  label,
  id,
}: {
  name: string;
  value?: string;
  defaultChecked?: boolean;
  label: string;
  id?: string;
}) {
  const inputId = id ?? `auto-cb-${name}`;
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  return (
    <label htmlFor={inputId} className="versus-toggle" style={{ opacity: isPending ? 0.6 : undefined }}>
      <input
        id={inputId}
        type="checkbox"
        name={name}
        value={value}
        defaultChecked={defaultChecked}
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
      />
      {" "}
      {label}
    </label>
  );
}
