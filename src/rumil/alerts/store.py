"""AlertConfigStore: CRUD on the ``alert_configs`` table.

Non-mutation-tracked — alert configs are operator preferences, not
workspace state. No staged filtering.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from rumil.db.row_helpers import _rows
from rumil.models import AlertConfig, AlertKind

if TYPE_CHECKING:
    from rumil.database import DB


def _row_to_config(row: dict[str, Any]) -> AlertConfig:
    return AlertConfig(
        id=row["id"],
        project_id=row.get("project_id"),
        run_id=row.get("run_id"),
        kind=AlertKind(row["kind"]),
        params=row.get("params") or {},
        enabled=bool(row.get("enabled", True)),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class AlertConfigStore:
    def __init__(self, db: "DB") -> None:
        self._db = db

    @property
    def client(self) -> Any:
        return self._db.client

    async def create(
        self,
        *,
        kind: AlertKind,
        params: dict | None = None,
        project_id: str | None = None,
        run_id: str | None = None,
        enabled: bool = True,
    ) -> AlertConfig:
        row = {
            "kind": kind.value,
            "params": params or {},
            "project_id": project_id,
            "run_id": run_id,
            "enabled": enabled,
        }
        rows = _rows(await self._db._execute(self.client.table("alert_configs").insert(row)))
        return _row_to_config(rows[0])

    async def delete(self, config_id: str) -> None:
        await self._db._execute(self.client.table("alert_configs").delete().eq("id", config_id))

    async def list_for_run(
        self,
        run_id: str,
        project_id: str | None = None,
    ) -> list[AlertConfig]:
        """Return all enabled configs matching this run (run-specific and
        project-wide). Consumers are responsible for deciding which one
        wins per-kind (see ``resolve_config_for_kind``).
        """
        or_parts = [f"run_id.eq.{run_id}"]
        if project_id:
            or_parts.append(f"project_id.eq.{project_id}")
        rows = _rows(
            await self._db._execute(
                self.client.table("alert_configs")
                .select("*")
                .eq("enabled", True)
                .or_(",".join(or_parts))
            )
        )
        return [_row_to_config(r) for r in rows]

    async def list_all(self) -> list[AlertConfig]:
        rows = _rows(await self._db._execute(self.client.table("alert_configs").select("*")))
        return [_row_to_config(r) for r in rows]
