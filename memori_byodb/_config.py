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
