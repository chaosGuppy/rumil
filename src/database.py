"""
SQLite database layer for the research workspace.
"""
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from models import (
    Call, CallStatus, CallType, ConsiderationDirection,
    LinkType, Page, PageLayer, PageLink, PageType, Workspace,
)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path) -> None:
    conn = _connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pages (
            id TEXT PRIMARY KEY,
            page_type TEXT NOT NULL,
            layer TEXT NOT NULL,
            workspace TEXT NOT NULL,
            content TEXT NOT NULL,
            summary TEXT NOT NULL,
            epistemic_status REAL DEFAULT 0.5,
            epistemic_type TEXT DEFAULT '',
            provenance_model TEXT DEFAULT '',
            provenance_call_type TEXT DEFAULT '',
            provenance_call_id TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            superseded_by TEXT,
            is_superseded INTEGER DEFAULT 0,
            extra TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS page_links (
            id TEXT PRIMARY KEY,
            from_page_id TEXT NOT NULL,
            to_page_id TEXT NOT NULL,
            link_type TEXT NOT NULL,
            direction TEXT,
            strength REAL DEFAULT 0.5,
            reasoning TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (from_page_id) REFERENCES pages(id),
            FOREIGN KEY (to_page_id) REFERENCES pages(id)
        );

        CREATE TABLE IF NOT EXISTS calls (
            id TEXT PRIMARY KEY,
            call_type TEXT NOT NULL,
            workspace TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            parent_call_id TEXT,
            scope_page_id TEXT,
            budget_allocated INTEGER,
            budget_used INTEGER DEFAULT 0,
            context_page_ids TEXT DEFAULT '[]',
            result_summary TEXT DEFAULT '',
            review_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (scope_page_id) REFERENCES pages(id)
        );

        CREATE TABLE IF NOT EXISTS budget (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            total INTEGER NOT NULL,
            used INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS page_ratings (
            id TEXT PRIMARY KEY,
            page_id TEXT NOT NULL,
            call_id TEXT NOT NULL,
            score INTEGER NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (page_id) REFERENCES pages(id),
            FOREIGN KEY (call_id) REFERENCES calls(id)
        );

        CREATE TABLE IF NOT EXISTS page_flags (
            id TEXT PRIMARY KEY,
            flag_type TEXT NOT NULL,
            call_id TEXT,
            page_id TEXT,
            page_id_a TEXT,
            page_id_b TEXT,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()

    # Migrate existing databases that predate the review_json column
    try:
        conn.execute("ALTER TABLE calls ADD COLUMN review_json TEXT DEFAULT '{}'")
        conn.commit()
    except Exception:
        pass  # Column already exists

    # Migrate epistemic_status and strength from 0-1 to 0-5 scale.
    # Only applies when all existing values are in the old 0-1 range.
    row = conn.execute("SELECT MAX(epistemic_status) FROM pages").fetchone()
    if row and row[0] is not None and row[0] <= 1.001:
        conn.execute("UPDATE pages SET epistemic_status = MIN(epistemic_status * 5, 5.0)")
        conn.commit()

    row = conn.execute("SELECT MAX(strength) FROM page_links").fetchone()
    if row and row[0] is not None and row[0] <= 1.001:
        conn.execute("UPDATE page_links SET strength = MIN(strength * 5, 5.0)")
        conn.commit()

    conn.close()


def _row_to_page(row: sqlite3.Row) -> Page:
    return Page(
        id=row["id"],
        page_type=PageType(row["page_type"]),
        layer=PageLayer(row["layer"]),
        workspace=Workspace(row["workspace"]),
        content=row["content"],
        summary=row["summary"],
        epistemic_status=row["epistemic_status"],
        epistemic_type=row["epistemic_type"] or "",
        provenance_model=row["provenance_model"] or "",
        provenance_call_type=row["provenance_call_type"] or "",
        provenance_call_id=row["provenance_call_id"] or "",
        created_at=datetime.fromisoformat(row["created_at"]),
        superseded_by=row["superseded_by"],
        is_superseded=bool(row["is_superseded"]),
        extra=row["extra"] or "{}",
    )


def _row_to_link(row: sqlite3.Row) -> PageLink:
    return PageLink(
        id=row["id"],
        from_page_id=row["from_page_id"],
        to_page_id=row["to_page_id"],
        link_type=LinkType(row["link_type"]),
        direction=ConsiderationDirection(row["direction"]) if row["direction"] else None,
        strength=row["strength"],
        reasoning=row["reasoning"] or "",
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_call(row: sqlite3.Row) -> Call:
    row_dict = dict(row)
    return Call(
        id=row_dict["id"],
        call_type=CallType(row_dict["call_type"]),
        workspace=Workspace(row_dict["workspace"]),
        status=CallStatus(row_dict["status"]),
        parent_call_id=row_dict["parent_call_id"],
        scope_page_id=row_dict["scope_page_id"],
        budget_allocated=row_dict["budget_allocated"],
        budget_used=row_dict["budget_used"],
        context_page_ids=row_dict.get("context_page_ids") or "[]",
        result_summary=row_dict.get("result_summary") or "",
        review_json=row_dict.get("review_json") or "{}",
        created_at=datetime.fromisoformat(row_dict["created_at"]),
        completed_at=datetime.fromisoformat(row_dict["completed_at"]) if row_dict["completed_at"] else None,
    )


class DB:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def _conn(self) -> sqlite3.Connection:
        return _connect(self.db_path)

    # --- Pages ---

    def save_page(self, page: Page) -> None:
        conn = self._conn()
        conn.execute("""
            INSERT OR REPLACE INTO pages
            (id, page_type, layer, workspace, content, summary,
             epistemic_status, epistemic_type,
             provenance_model, provenance_call_type, provenance_call_id,
             created_at, superseded_by, is_superseded, extra)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            page.id, page.page_type.value, page.layer.value, page.workspace.value,
            page.content, page.summary,
            page.epistemic_status, page.epistemic_type,
            page.provenance_model, page.provenance_call_type, page.provenance_call_id,
            page.created_at.isoformat(),
            page.superseded_by, int(page.is_superseded),
            page.extra,
        ))
        conn.commit()
        conn.close()

    def get_page(self, page_id: str) -> Optional[Page]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        conn.close()
        return _row_to_page(row) if row else None

    def resolve_page_id(self, page_id: str) -> Optional[str]:
        """Resolve a page ID to a full UUID. Handles both full UUIDs and 8-char short IDs.
        Returns the full UUID if found, or None."""
        if not page_id:
            return None
        # Try exact match first
        conn = self._conn()
        row = conn.execute("SELECT id FROM pages WHERE id = ?", (page_id,)).fetchone()
        if row:
            conn.close()
            return row["id"]
        # Try prefix match for short IDs
        if len(page_id) <= 8:
            rows = conn.execute(
                "SELECT id FROM pages WHERE id LIKE ?", (page_id + "%",)
            ).fetchall()
            conn.close()
            if len(rows) == 1:
                return rows[0]["id"]
            if len(rows) > 1:
                print(f"  [db] Ambiguous short ID '{page_id}' matches {len(rows)} pages — skipping.")
            return None
        conn.close()
        return None

    def get_pages(
        self,
        workspace: Optional[Workspace] = None,
        page_type: Optional[PageType] = None,
        active_only: bool = True,
    ) -> list[Page]:
        conn = self._conn()
        query = "SELECT * FROM pages WHERE 1=1"
        params: list = []
        if workspace:
            query += " AND workspace = ?"
            params.append(workspace.value)
        if page_type:
            query += " AND page_type = ?"
            params.append(page_type.value)
        if active_only:
            query += " AND is_superseded = 0"
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [_row_to_page(r) for r in rows]

    def supersede_page(self, old_id: str, new_id: str) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE pages SET is_superseded = 1, superseded_by = ? WHERE id = ?",
            (new_id, old_id),
        )
        conn.commit()
        conn.close()

    # --- Links ---

    def save_link(self, link: PageLink) -> None:
        conn = self._conn()
        conn.execute("""
            INSERT OR REPLACE INTO page_links
            (id, from_page_id, to_page_id, link_type, direction, strength, reasoning, created_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            link.id, link.from_page_id, link.to_page_id,
            link.link_type.value,
            link.direction.value if link.direction else None,
            link.strength, link.reasoning,
            link.created_at.isoformat(),
        ))
        conn.commit()
        conn.close()

    def get_links_to(self, page_id: str) -> list[PageLink]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM page_links WHERE to_page_id = ?", (page_id,)
        ).fetchall()
        conn.close()
        return [_row_to_link(r) for r in rows]

    def get_links_from(self, page_id: str) -> list[PageLink]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM page_links WHERE from_page_id = ?", (page_id,)
        ).fetchall()
        conn.close()
        return [_row_to_link(r) for r in rows]

    def get_considerations_for_question(self, question_id: str) -> list[tuple[Page, PageLink]]:
        """Return (claim_page, link) pairs for all considerations on a question."""
        links = self.get_links_to(question_id)
        consideration_links = [l for l in links if l.link_type == LinkType.CONSIDERATION]
        result = []
        for link in consideration_links:
            page = self.get_page(link.from_page_id)
            if page and page.is_active():
                result.append((page, link))
        return result

    def get_child_questions(self, parent_id: str) -> list[Page]:
        """Return sub-questions of a question."""
        links = self.get_links_from(parent_id)
        child_links = [l for l in links if l.link_type == LinkType.CHILD_QUESTION]
        result = []
        for link in child_links:
            page = self.get_page(link.to_page_id)
            if page and page.is_active():
                result.append(page)
        return result

    def get_judgements_for_question(self, question_id: str) -> list[Page]:
        links = self.get_links_to(question_id)
        judgement_links = [l for l in links if l.link_type == LinkType.RELATED]
        result = []
        for link in judgement_links:
            page = self.get_page(link.from_page_id)
            if page and page.is_active() and page.page_type == PageType.JUDGEMENT:
                result.append(page)
        return result

    # --- Calls ---

    def save_call(self, call: Call) -> None:
        conn = self._conn()
        conn.execute("""
            INSERT OR REPLACE INTO calls
            (id, call_type, workspace, status, parent_call_id, scope_page_id,
             budget_allocated, budget_used, context_page_ids, result_summary,
             review_json, created_at, completed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            call.id, call.call_type.value, call.workspace.value,
            call.status.value, call.parent_call_id, call.scope_page_id,
            call.budget_allocated, call.budget_used,
            call.context_page_ids, call.result_summary,
            call.review_json,
            call.created_at.isoformat(),
            call.completed_at.isoformat() if call.completed_at else None,
        ))
        conn.commit()
        conn.close()

    def get_call(self, call_id: str) -> Optional[Call]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        conn.close()
        return _row_to_call(row) if row else None

    def update_call_status(self, call_id: str, status: CallStatus,
                           result_summary: str = "") -> None:
        conn = self._conn()
        completed_at = datetime.utcnow().isoformat() if status == CallStatus.COMPLETE else None
        conn.execute("""
            UPDATE calls SET status = ?, result_summary = ?, completed_at = ?
            WHERE id = ?
        """, (status.value, result_summary, completed_at, call_id))
        conn.commit()
        conn.close()

    def increment_call_budget_used(self, call_id: str, amount: int = 1) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE calls SET budget_used = budget_used + ? WHERE id = ?",
            (amount, call_id),
        )
        conn.commit()
        conn.close()

    # --- Global budget ---

    def init_budget(self, total: int) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO budget (id, total, used) VALUES (1, ?, 0)",
            (total,),
        )
        conn.commit()
        conn.close()

    def get_budget(self) -> tuple[int, int]:
        """Returns (total, used)."""
        conn = self._conn()
        row = conn.execute("SELECT total, used FROM budget WHERE id = 1").fetchone()
        conn.close()
        if row:
            return row["total"], row["used"]
        return 0, 0

    def consume_budget(self, amount: int = 1) -> bool:
        """Deduct from global budget. Returns False if insufficient budget."""
        conn = self._conn()
        row = conn.execute("SELECT total, used FROM budget WHERE id = 1").fetchone()
        if not row or (row["used"] + amount) > row["total"]:
            conn.close()
            return False
        conn.execute(
            "UPDATE budget SET used = used + ? WHERE id = 1", (amount,)
        )
        conn.commit()
        conn.close()
        return True

    def add_budget(self, amount: int) -> None:
        """Add more calls to the existing budget (for continue runs)."""
        conn = self._conn()
        conn.execute("UPDATE budget SET total = total + ? WHERE id = 1", (amount,))
        conn.commit()
        conn.close()

    def budget_remaining(self) -> int:
        total, used = self.get_budget()
        return max(0, total - used)

    def get_last_scout_info(self, question_id: str) -> Optional[tuple[str, Optional[int]]]:
        """Return (completed_at_iso, remaining_fruit) for the most recent scout call
        on this question, or None if never scouted."""
        conn = self._conn()
        row = conn.execute("""
            SELECT completed_at, review_json FROM calls
            WHERE call_type = 'scout' AND scope_page_id = ? AND status = 'complete'
            ORDER BY completed_at DESC LIMIT 1
        """, (question_id,)).fetchone()
        conn.close()
        if not row or not row["completed_at"]:
            return None
        try:
            fruit = json.loads(row["review_json"]).get("remaining_fruit")
        except Exception:
            fruit = None
        return row["completed_at"], fruit

    def get_ingest_history(self) -> dict[str, list[str]]:
        """Return {source_id: [question_id, ...]} based on considerations created by
        ingest calls. Reflects which sources have been extracted against which questions."""
        conn = self._conn()
        rows = conn.execute("""
            SELECT DISTINCT c.scope_page_id AS source_id, pl.to_page_id AS question_id
            FROM calls c
            JOIN pages p ON p.provenance_call_id = c.id
            JOIN page_links pl ON pl.from_page_id = p.id AND pl.link_type = 'consideration'
            WHERE c.call_type = 'ingest' AND c.status = 'complete'
        """).fetchall()
        conn.close()
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(row["source_id"], []).append(row["question_id"])
        return result

    def save_page_rating(self, page_id: str, call_id: str, score: int, note: str = "") -> None:
        conn = self._conn()
        conn.execute("""
            INSERT INTO page_ratings (id, page_id, call_id, score, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (str(uuid.uuid4()), page_id, call_id, score, note, datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()

    def save_page_flag(
        self,
        flag_type: str,
        call_id: Optional[str] = None,
        note: str = "",
        page_id: Optional[str] = None,
        page_id_a: Optional[str] = None,
        page_id_b: Optional[str] = None,
    ) -> None:
        conn = self._conn()
        conn.execute("""
            INSERT INTO page_flags (id, flag_type, call_id, page_id, page_id_a, page_id_b, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (str(uuid.uuid4()), flag_type, call_id, page_id, page_id_a, page_id_b,
              note, datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()

    def get_root_questions(self, workspace: Workspace = Workspace.RESEARCH) -> list[Page]:
        """Return questions that have no parent (top-level questions)."""
        conn = self._conn()
        # Root questions: question pages not appearing as a child in any link
        rows = conn.execute("""
            SELECT p.* FROM pages p
            WHERE p.page_type = 'question'
              AND p.workspace = ?
              AND p.is_superseded = 0
              AND p.id NOT IN (
                  SELECT to_page_id FROM page_links WHERE link_type = 'child_question'
              )
            ORDER BY p.created_at DESC
        """, (workspace.value,)).fetchall()
        conn.close()
        return [_row_to_page(r) for r in rows]

    def count_pages_for_question(self, question_id: str) -> dict:
        """Count pages linked to or created in context of a question."""
        conn = self._conn()
        considerations = conn.execute(
            "SELECT COUNT(*) FROM page_links WHERE to_page_id = ? AND link_type = 'consideration'",
            (question_id,)
        ).fetchone()[0]
        judgements = conn.execute("""
            SELECT COUNT(*) FROM page_links pl
            JOIN pages p ON pl.from_page_id = p.id
            WHERE pl.to_page_id = ? AND p.page_type = 'judgement' AND p.is_superseded = 0
        """, (question_id,)).fetchone()[0]
        conn.close()
        return {"considerations": considerations, "judgements": judgements}
