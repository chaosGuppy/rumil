import type { JudgeLabel } from "@/api/types.gen";

export function JudgeHeader({
  judge,
  label,
  includeTask = true,
}: {
  judge: string;
  label: JudgeLabel;
  includeTask?: boolean;
}) {
  return (
    <th title={judge} className="judge-th">
      {label.variant && <div className="judge-th-variant">{label.variant}</div>}
      <div className="judge-th-model">{label.model}</div>
      {includeTask && label.task && <div className="judge-th-task">{label.task}</div>}
      {includeTask && label.phash && <div className="judge-th-hash">{label.phash}</div>}
    </th>
  );
}

export function shortName(model: string): string {
  return model
    .split("/")
    .slice(-1)[0]
    .replace("-preview", "")
    .replace(/-202\d{5}/, "");
}
