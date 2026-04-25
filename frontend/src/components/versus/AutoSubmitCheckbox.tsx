"use client";

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
  return (
    <label htmlFor={inputId} className="versus-toggle">
      <input
        id={inputId}
        type="checkbox"
        name={name}
        value={value}
        defaultChecked={defaultChecked}
        onChange={(e) => e.currentTarget.form?.requestSubmit()}
      />
      {" "}
      {label}
    </label>
  );
}
