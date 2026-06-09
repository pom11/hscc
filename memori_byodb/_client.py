"""Memori BYODB client — local SQLite + local embeddings."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

try:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
except ImportError:
    raise RuntimeError("sqlalchemy is required for Memori BYODB. Run: pip install sqlalchemy")


logger = logging.getLogger(__name__)


class MemoriBYODBClient:
    """Thin wrapper around local Memori SDK for Hermes BYODB mode."""

    def __init__(
        self,
        *,
        entity_id: str,
        process_id: str | None = None,
        project_id: str,
        db_path: str | None = None,
    ) -> None:
        self.entity_id = entity_id
        self.process_id = process_id or "hermes_agent"
        self.project_id = project_id
        self.db_path = db_path or str(Path.home() / ".hermes" / "memori_byodb.db")

        # Initialize Memori BYODB
        try:
            from memori import Memori
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                f"Memori SDK dependency missing: {exc.name}. Run: pip install memori"
            ) from exc

        # Create SQLite engine for BYODB
        engine = create_engine(f"sqlite:///{self.db_path}")
        SessionLocal = sessionmaker(bind=engine)

        self.memori = Memori(conn=SessionLocal)
        self.memori.attribution(entity_id, self.process_id)

        # Provision the database schema
        try:
            self.memori.config.storage.build()
            engine = create_engine(f"sqlite:///{self.db_path}")
            self.memori.config.augmentation.start(engine)
        except Exception as exc:
            logger.warning("Memori BYODB initialization warning: %s", exc)

    def capture_turn(
        self,
        *,
        user_content: str,
        assistant_content: str,
        session_id: str,
        platform: str,
        trace: dict[str, Any] | None = None,
    ) -> None:
        from memori.memory.augmentation.input import AugmentationInput
        from memori.memory.augmentation._message import ConversationMessage

        # Build conversation messages
        messages = [
            ConversationMessage(role="user", content=user_content, type="text", trace=None),
            ConversationMessage(
                role="assistant", content=assistant_content, type="text", trace=trace
            ),
        ]

        # Enqueue augmentation
        self.memori.config.augmentation.enqueue(
            AugmentationInput(
                conversation_id=session_id,
                entity_id=self.entity_id,
                process_id=self.process_id,
                conversation_messages=messages,
            )
        )
        # Wait for augmentation to complete
        self.memori.config.augmentation.wait()

    def agent_recall(self, params: dict[str, Any]) -> dict[str, Any]:
        query = params.get("query", "")
        project_id = params.get("projectId") or params.get("project_id") or self.project_id
        limit = params.get("limit", 10)

        # Use local recall
        try:
            results = self.memori.recall(query, limit=limit)
            return {
                "project_id": project_id,
                "query": query,
                "memories": results if isinstance(results, list) else [],
                "count": len(results) if isinstance(results, list) else 0,
            }
        except Exception as exc:
            return {"error": str(exc), "query": query, "memories": []}

    def agent_recall_summary(self, params: dict[str, Any]) -> dict[str, Any]:
        date_start = params.get("dateStart") or params.get("date_start")
        date_end = params.get("dateEnd") or params.get("date_end")
        project_id = params.get("projectId") or params.get("project_id") or self.project_id

        # For summary, just recall everything in the range with a broader query
        query = f"summary from {date_start or 'all time'} to {date_end or 'today'}"
        try:
            results = self.memori.recall(query, limit=20)
            return {
                "project_id": project_id,
                "summary": {
                    "date_start": date_start,
                    "date_end": date_end,
                    "total_memories": len(results) if isinstance(results, list) else 0,
                    "memories": results if isinstance(results, list) else [],
                },
            }
        except Exception as exc:
            return {"error": str(exc)}

    def shutdown(self) -> None:
        try:
            self.memori.close()
        except Exception:
            pass
