"use client";

export function AutoSubmitSelect({
  name,
  defaultValue,
  options,
  className,
  style,
  id,
}: {
  name: string;
  defaultValue?: string;
  options: { value: string; label: string }[];
  className?: string;
  style?: React.CSSProperties;
  id?: string;
}) {
  return (
    <select
      id={id}
      name={name}
      defaultValue={defaultValue}
      className={className}
      style={style}
      onChange={(e) => e.currentTarget.form?.requestSubmit()}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}
