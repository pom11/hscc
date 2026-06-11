"""Configuration for Memori BYODB provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MemoriConfig:
    entity_id: str = ""
    project_id: str | None = None
    process_id: str | None = None
    db_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {"entityId": self.entity_id}
        if self.project_id:
            d["projectId"] = self.project_id
        if self.process_id:
            d["processId"] = self.process_id
        if self.db_path:
            d["dbPath"] = self.db_path
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MemoriConfig":
        """Build from a config dict, accepting camelCase (as written by
        ``to_dict``) or snake_case keys. Unknown keys are ignored.

        ``MemoriConfig(**raw)`` was used directly, but the on-disk config writes
        camelCase (entityId/projectId/…) while the dataclass fields are
        snake_case — so the splat raised ``unexpected keyword argument
        'entityId'`` and the memory provider failed to init every turn.
        """
        raw = raw or {}

        def pick(*keys):
            for k in keys:
                if raw.get(k):
                    return raw[k]
            return None

        return cls(
            entity_id=pick("entity_id", "entityId") or "",
            project_id=pick("project_id", "projectId"),
            process_id=pick("process_id", "processId"),
            db_path=pick("db_path", "dbPath"),
        )
