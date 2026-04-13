# /// script
# dependencies = ["anthropic"]
# ///
"""Import rumil research graph into the worldview tree format.

Reads exported rumil data (pages + links), uses Claude to organize the flat
graph into a well-structured worldview tree with thematic branches, concise
content, proper node types, and importance levels. Also imports sources.

Usage:
    uv run public-ui/import_from_rumil.py /tmp/rumil_export.json --workspace forethought
"""

import json
import sqlite3
import sys
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import anthropic

DB_PATH = Path(__file__).parent / "worldview.db"
VALID_NODE_TYPES = {"claim", "hypothesis", "evidence", "uncertainty", "context", "question"}


def load_export(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def prepare_pages_for_llm(data: dict) -> tuple[str, dict[str, list[str]]]:
    """Build a compact representation of all pages + links for the LLM.

    Returns the text prompt and a cites index mapping page_id -> source short IDs.
    """
    pages_by_id = {p["id"]: p for p in data["pages"]}

    # Build link indices
    cites_index: dict[str, list[str]] = defaultdict(list)
    deps: dict[str, list[str]] = defaultdict(list)  # A depends on B
    considerations: dict[str, list[str]] = defaultdict(list)  # question -> claims
    child_qs: dict[str, list[str]] = defaultdict(list)
    answers: dict[str, list[str]] = defaultdict(list)  # question -> judgements

    for link in data["links"]:
        lt = link["link_type"]
        f, t = link["from_page_id"], link["to_page_id"]
        if lt == "cites":
            cites_index[f].append(t[:8])
        elif lt == "depends_on":
            deps[f].append(t[:8])
        elif lt == "consideration":
            considerations[t].append(f[:8])
        elif lt == "child_question":
            child_qs[f].append(t[:8])
        elif lt == "answers":
            answers[t].append(f[:8])

    lines = []
    for page in data["pages"]:
        if page["page_type"] == "source":
            continue
        pid = page["id"][:8]
        ptype = page["page_type"]
        headline = page["headline"]
        content = (page.get("content") or page.get("abstract") or "")[:500]
        cred = page.get("credence")
        rob = page.get("robustness")
        scores = ""
        if cred is not None:
            scores += f" C{cred}"
        if rob is not None:
            scores += f"/R{rob}"

        source_refs = cites_index.get(page["id"], [])
        dep_refs = deps.get(page["id"], [])
        consid_refs = considerations.get(page["id"], [])
        child_q_refs = child_qs.get(page["id"], [])
        answer_refs = answers.get(page["id"], [])

        entry = f"[{pid}] {ptype}{scores}: {headline}\n"
        if content:
            entry += f"  Content: {content}\n"
        if source_refs:
            entry += f"  Cites sources: {', '.join(source_refs)}\n"
        if dep_refs:
            entry += f"  Depends on: {', '.join(dep_refs)}\n"
        if consid_refs:
            entry += f"  Considerations from: {', '.join(consid_refs)}\n"
        if child_q_refs:
            entry += f"  Sub-questions: {', '.join(child_q_refs)}\n"
        if answer_refs:
            entry += f"  Answered by: {', '.join(answer_refs)}\n"

        lines.append(entry)

    return "\n".join(lines), cites_index


def organize_tree_with_llm(pages_text: str) -> list[dict]:
    """Use Claude to organize pages into a well-structured worldview tree."""
    client = anthropic.Anthropic()

    prompt = (
        "You are organizing research findings into a worldview tree — a hierarchical "
        "structure that a reader can browse to understand a complex topic.\n\n"
        "## Input\n\n"
        "Below are research pages from a graph-structured research system. Each has an "
        "8-char ID, a type (claim/question/judgement/summary), credence (1-9), "
        "robustness (1-5), and content. Links show how pages relate.\n\n"
        "## Your Task\n\n"
        "Produce a JSON tree with these properties:\n\n"
        "1. **4-6 top-level thematic branches** under the root. Each branch groups "
        "related findings. Use the existing judgements as natural branch organizers.\n\n"
        "2. **Node types**: claim, hypothesis, evidence, uncertainty, context, question\n"
        "   - claim: factual assertion (most research findings)\n"
        "   - hypothesis: theoretical model or speculative mechanism\n"
        "   - evidence: specific empirical observation or data point\n"
        "   - uncertainty: open question, unresolved tension\n"
        "   - context: meta-commentary, summary, scope-setting\n"
        "   - question: a research question (use for genuine open questions)\n\n"
        "3. **Importance levels (L0-L4)**:\n"
        "   - L0: 3-5 core findings you'd include in a 5-minute briefing\n"
        "   - L1: important supporting claims (maybe 15-20 nodes)\n"
        "   - L2: supplementary evidence, edge cases (bulk of nodes)\n"
        "   - L3: tangential context, detailed data points\n"
        "   - L4: peripheral material\n"
        "   Target distribution: ~5% L0, ~25% L1, ~45% L2, ~20% L3, ~5% L4\n\n"
        "4. **Concise content**: Each node gets a `content` field of 1-3 sentences. "
        "This is NOT the full research text — it's a crisp summary suitable for "
        "browsing. Think executive briefing, not research paper.\n\n"
        "5. **Source references**: Preserve `source_ids` arrays from the original pages "
        "(the 8-char source IDs from 'Cites sources' lines).\n\n"
        "6. **Tree depth**: Aim for 3-4 levels. Don't go deeper than 5.\n\n"
        "7. **Every original page must appear** in the tree (except sources). Use the "
        "original 8-char IDs. You may create NEW organizing nodes (use fresh 8-char "
        "hex IDs like 'aa000001') — mark them with `\"synthetic\": true`.\n\n"
        "## Output Format\n\n"
        "Return a JSON array of top-level nodes. Each node:\n"
        "```json\n"
        "{\n"
        '  "id": "8-char-id",\n'
        '  "node_type": "claim|hypothesis|evidence|uncertainty|context|question",\n'
        '  "headline": "Short headline",\n'
        '  "content": "1-3 sentence summary",\n'
        '  "credence": null or 1-9,\n'
        '  "robustness": null or 1-5,\n'
        '  "importance": 0-4,\n'
        '  "source_ids": ["src-id1"],\n'
        '  "children": [...]\n'
        "}\n"
        "```\n\n"
        "## Research Pages\n\n"
        f"{pages_text}\n\n"
        "Return ONLY the JSON array. No commentary, no markdown fences."
    )

    print("Calling Claude to organize tree (this may take a moment)...")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
    )

    block = response.content[0]
    assert isinstance(block, anthropic.types.TextBlock)
    text = block.text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0].strip()

    return json.loads(text)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def validate_node(node: dict) -> dict:
    """Ensure node has valid types and values."""
    if node.get("node_type") not in VALID_NODE_TYPES:
        node["node_type"] = "claim"
    node["importance"] = max(0, min(4, node.get("importance", 2)))
    cred = node.get("credence")
    if cred is not None:
        node["credence"] = max(1, min(9, int(cred)))
    rob = node.get("robustness")
    if rob is not None:
        node["robustness"] = max(1, min(5, int(rob)))
    for child in node.get("children", []):
        validate_node(child)
    return node


def count_nodes(node: dict) -> int:
    return 1 + sum(count_nodes(c) for c in node.get("children", []))


def count_by_importance(node: dict, counts: dict | None = None) -> dict:
    if counts is None:
        counts = defaultdict(int)
    counts[node.get("importance", 2)] += 1
    for child in node.get("children", []):
        count_by_importance(child, counts)
    return counts


def print_tree(node: dict, depth: int = 0) -> None:
    indent = "  " * depth
    cred = f" C{node['credence']}" if node.get("credence") else ""
    rob = f"/R{node['robustness']}" if node.get("robustness") else ""
    syn = " *" if node.get("synthetic") else ""
    kids = len(node.get("children", []))
    kid_note = f" ({kids})" if kids else ""
    print(
        f"{indent}[{node.get('node_type', '?')}] L{node.get('importance', '?')}"
        f"{cred}{rob}{syn}: {node.get('headline', '?')[:65]}{kid_note}"
    )
    for child in node.get("children", []):
        print_tree(child, depth + 1)


def insert_node(
    conn: sqlite3.Connection,
    ws_id: str,
    node: dict,
    parent_id: str | None,
    position: int,
) -> int:
    """Recursively insert a node and its children. Returns count inserted."""
    node_id = new_id()
    conn.execute(
        "INSERT INTO nodes (id, workspace_id, parent_id, node_type, headline, content, "
        "credence, robustness, importance, position, source_ids, created_at, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            node_id,
            ws_id,
            parent_id,
            node.get("node_type", "claim"),
            node.get("headline", ""),
            node.get("content", ""),
            node.get("credence"),
            node.get("robustness"),
            node.get("importance", 2),
            position,
            json.dumps(node.get("source_ids", [])),
            now_iso(),
            "rumil-import",
        ),
    )
    count = 1
    for i, child in enumerate(node.get("children", [])):
        count += insert_node(conn, ws_id, child, node_id, i)
    return count


def insert_sources(conn: sqlite3.Connection, ws_id: str, data: dict) -> int:
    """Insert source pages into the sources table."""
    count = 0
    for page in data["pages"]:
        if page["page_type"] != "source":
            continue
        extra = page.get("extra") or {}
        # Use the first 16 chars of the original ID so short IDs (first 8) match
        source_id = page["id"][:16]
        conn.execute(
            "INSERT OR REPLACE INTO sources (id, workspace_id, title, url, abstract, "
            "content, extra, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_id,
                ws_id,
                page["headline"][:200],
                extra.get("url", ""),
                (page.get("abstract") or page["headline"])[:1000],
                page.get("content") or "",
                json.dumps(extra),
                page.get("created_at") or now_iso(),
            ),
        )
        count += 1
    return count


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: uv run public-ui/import_from_rumil.py <export.json> [--workspace NAME]")
        sys.exit(1)

    export_path = sys.argv[1]
    workspace_name = "imported"
    if "--workspace" in sys.argv:
        idx = sys.argv.index("--workspace")
        workspace_name = sys.argv[idx + 1]

    print(f"Loading export from {export_path}...")
    data = load_export(export_path)

    non_source = [p for p in data["pages"] if p["page_type"] != "source"]
    sources = [p for p in data["pages"] if p["page_type"] == "source"]
    print(f"  {len(non_source)} content pages, {len(sources)} sources, {len(data['links'])} links")

    # Prepare page data for LLM
    print("Preparing data for LLM...")
    pages_text, cites_index = prepare_pages_for_llm(data)

    # Get LLM to organize into a tree
    tree_nodes = organize_tree_with_llm(pages_text)
    print(f"Got {len(tree_nodes)} top-level branches")

    # Validate all nodes
    for node in tree_nodes:
        validate_node(node)

    # Build root node
    root_question = next(
        (p for p in data["pages"] if p["page_type"] == "question" and not any(
            l["link_type"] == "child_question" and l["to_page_id"] == p["id"]
            for l in data["links"]
        )),
        None,
    )
    root = {
        "node_type": "context",
        "headline": root_question["headline"] if root_question else "Worldview",
        "content": root_question.get("content", "") if root_question else "",
        "credence": None,
        "robustness": None,
        "importance": 0,
        "source_ids": [],
        "children": tree_nodes,
    }

    # Print tree for review
    total = count_nodes(root)
    print(f"\n--- Tree: {total} nodes ---")
    print_tree(root)

    imp_counts = count_by_importance(root)
    print("\nImportance distribution:")
    for level in sorted(imp_counts):
        print(f"  L{level}: {imp_counts[level]}")

    # Write to SQLite
    conn = get_db()

    existing = conn.execute(
        "SELECT id FROM workspaces WHERE name = ?", (workspace_name,)
    ).fetchone()
    if existing:
        ws_id = existing["id"]
        conn.execute("DELETE FROM nodes WHERE workspace_id = ?", (ws_id,))
        conn.execute("DELETE FROM sources WHERE workspace_id = ?", (ws_id,))
        print(f"\nCleared existing workspace '{workspace_name}'")
    else:
        ws_id = new_id()
        conn.execute(
            "INSERT INTO workspaces (id, name, created_at) VALUES (?, ?, ?)",
            (ws_id, workspace_name, now_iso()),
        )
        print(f"\nCreated workspace '{workspace_name}'")

    node_count = insert_node(conn, ws_id, root, None, 0)
    source_count = insert_sources(conn, ws_id, data)

    conn.commit()
    conn.close()

    print(f"Inserted {node_count} nodes and {source_count} sources into '{workspace_name}'")
    print("Done!")


if __name__ == "__main__":
    main()
