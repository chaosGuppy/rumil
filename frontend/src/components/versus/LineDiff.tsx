/**
 * Tiny LCS-based line diff between two strings, rendered as a unified
 * diff (added/removed lines + context). No external dep — Myers/LCS
 * is small enough to inline. Good enough for prompt-shaped inputs in
 * the hundreds-of-lines range.
 *
 * Not exported as a hook because it's pure; the caller passes the two
 * strings and we just render <pre>-equivalent lines.
 */

type DiffOp = { kind: "eq" | "add" | "del"; line: string };

/** LCS table over the two line lists, then walk back to a sequence of
 *  eq/add/del ops. O(n*m) memory; we cap at ~5k lines per side to
 *  avoid pathological cases. */
function diffLines(a: string, b: string): DiffOp[] {
  const al = a.split("\n");
  const bl = b.split("\n");
  if (al.length > 5000 || bl.length > 5000) {
    return [
      { kind: "del", line: `(diff skipped — input too large: ${al.length} vs ${bl.length} lines)` },
    ];
  }
  const n = al.length;
  const m = bl.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      if (al[i] === bl[j]) dp[i][j] = dp[i + 1][j + 1] + 1;
      else dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const ops: DiffOp[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (al[i] === bl[j]) {
      ops.push({ kind: "eq", line: al[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      ops.push({ kind: "del", line: al[i] });
      i++;
    } else {
      ops.push({ kind: "add", line: bl[j] });
      j++;
    }
  }
  while (i < n) ops.push({ kind: "del", line: al[i++] });
  while (j < m) ops.push({ kind: "add", line: bl[j++] });
  return ops;
}

/** Collapse runs of unchanged lines longer than `context * 2` into
 *  a "... N unchanged lines ..." marker, keeping `context` lines on
 *  each side of every change. */
function collapseUnchanged(ops: DiffOp[], context = 2): (DiffOp | { kind: "skip"; n: number })[] {
  type Out = DiffOp | { kind: "skip"; n: number };
  const out: Out[] = [];
  let runStart = -1;
  for (let i = 0; i < ops.length; i++) {
    if (ops[i].kind === "eq") {
      if (runStart === -1) runStart = i;
    } else if (runStart !== -1) {
      flushRun(runStart, i);
      runStart = -1;
    }
    if (ops[i].kind !== "eq" && runStart === -1) out.push(ops[i]);
  }
  if (runStart !== -1) flushRun(runStart, ops.length);
  return out;

  function flushRun(start: number, end: number) {
    const len = end - start;
    if (len <= context * 2) {
      for (let k = start; k < end; k++) out.push(ops[k]);
      return;
    }
    if (start > 0) {
      for (let k = start; k < start + context; k++) out.push(ops[k]);
      out.push({ kind: "skip", n: len - context * 2 });
      for (let k = end - context; k < end; k++) out.push(ops[k]);
    } else {
      out.push({ kind: "skip", n: len - context });
      for (let k = end - context; k < end; k++) out.push(ops[k]);
    }
  }
}

export function LineDiff({
  a,
  b,
  labelA,
  labelB,
}: {
  a: string;
  b: string;
  labelA?: string;
  labelB?: string;
}) {
  const ops = collapseUnchanged(diffLines(a, b));
  return (
    <div className="line-diff">
      {(labelA || labelB) && (
        <div className="line-diff-head">
          <span className="line-diff-label del">- {labelA ?? "A"}</span>
          <span className="line-diff-label add">+ {labelB ?? "B"}</span>
        </div>
      )}
      <pre className="line-diff-body">
        {ops.map((op, idx) => {
          if (op.kind === "skip") {
            return (
              <span key={idx} className="line-diff-line skip">
                {`  …${op.n} unchanged lines\n`}
              </span>
            );
          }
          const sigil = op.kind === "add" ? "+" : op.kind === "del" ? "-" : " ";
          return (
            <span key={idx} className={`line-diff-line ${op.kind}`}>
              {`${sigil} ${op.line}\n`}
            </span>
          );
        })}
      </pre>
    </div>
  );
}
