"use client";

type Option = { value: string; label: string };

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
  return (
    <select
      id={id}
      name={name}
      defaultValue={defaultValue}
      className={className}
      style={style}
      onChange={(e) => e.currentTarget.form?.requestSubmit()}
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
