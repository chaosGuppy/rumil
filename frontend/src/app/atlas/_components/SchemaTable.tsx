import type { JsonSchemaField } from "@/api";

function renderType(f: JsonSchemaField): string {
  if (f.type === "array") {
    if (f.items_ref) return `array<${f.items_ref}>`;
    if (f.items_type) return `array<${f.items_type}>`;
    return "array";
  }
  return f.type ?? "any";
}

export function SchemaTable({ fields }: { fields: JsonSchemaField[] }) {
  if (!fields || fields.length === 0) {
    return (
      <div className="atlas-empty">
        <strong>no fields</strong>
        this payload has no declared fields.
      </div>
    );
  }
  return (
    <div className="atlas-schema">
      {fields.map((f) => (
        <div
          key={f.name}
          className={`atlas-schema-field ${f.required ? "is-required" : "is-optional"}`}
        >
          <div className="atlas-schema-name">
            <span>{f.name}</span>
            <span
              className={`atlas-schema-name-flag ${f.required ? "is-required" : ""}`}
            >
              {f.required ? "required" : "optional"}
            </span>
          </div>
          <div className="atlas-schema-type">
            <span>{renderType(f)}</span>
            {f.default !== null && f.default !== undefined && (
              <span className="atlas-schema-type-default">
                default: {JSON.stringify(f.default)}
              </span>
            )}
            {f.minimum !== null && f.minimum !== undefined && (
              <span className="atlas-schema-type-default">
                min: {f.minimum}
              </span>
            )}
            {f.maximum !== null && f.maximum !== undefined && (
              <span className="atlas-schema-type-default">
                max: {f.maximum}
              </span>
            )}
          </div>
          <div>
            <div className="atlas-schema-desc">
              {f.description || (
                <span style={{ color: "var(--a-muted)", fontStyle: "italic" }}>
                  no description
                </span>
              )}
            </div>
            {f.enum && f.enum.length > 0 && (
              <div className="atlas-schema-enum">
                enum: {f.enum.map((v) => JSON.stringify(v)).join(" · ")}
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
