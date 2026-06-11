"""Memori BYODB client — local SQLite + local embeddings + LOCAL LLM augmentation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
except ImportError:
    raise RuntimeError("sqlalchemy is required for Memori BYODB. Run: pip install sqlalchemy")


logger = logging.getLogger(__name__)


class LocalAugmentationWrapper:
    """Wraps our custom LocalLLMAugmentation to match SDK's augmentation interface."""
    
    def __init__(self, local_aug: Any) -> None:
        self.local_aug = local_aug
        self.enabled = True


class MemoriBYODBClient:
    """Thin wrapper around the local Memori SDK for Hermes BYODB mode.

    Uses a local OpenAI-compatible LLM (e.g. the cluster orchestrator) for fact
    extraction instead of the Memori cloud API.
    """

    SYNC_JOIN_TIMEOUT_SECS: int = 30

    def __init__(
        self,
        *,
        entity_id: str,
        process_id: str | None = None,
        project_id: str,
        db_path: str | None = None,
        api_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.entity_id = entity_id
        self.process_id = process_id or "hermes_agent"
        self.project_id = project_id
        # DB path: explicit arg > HSCC_MEMORI_DB_PATH env > local ~/.hermes default.
        # No host/mount baked in — point it at a NAS/shared path via the env var
        # if you want the memory DB shared across machines.
        self.db_path = db_path or os.environ.get("HSCC_MEMORI_DB_PATH") or str(
            Path.home() / ".hermes" / "memori_byodb.db")
        self._sync_thread: threading.Thread | None = None
        self._last_user_content: str = ""
        self._last_assistant_content: str = ""
        self._last_session_id: str = ""

        # Local augmentation LLM: explicit arg > env > localhost default.
        self._api_url = api_url or os.environ.get(
            "HSCC_MEMORI_AUGMENT_URL", "http://localhost:8000/v1/chat/completions")
        self._model = model or os.environ.get(
            "HSCC_MEMORI_AUGMENT_MODEL", "local-model")

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

        # Register SQLite driver in augmentation registry (needs separate registration)
        try:
            from memori.storage.drivers.sqlite._driver import Driver as SQLiteDriver
            from memori.storage._registry import Registry as StorageRegistry
            StorageRegistry._drivers["sqlite"] = SQLiteDriver
        except Exception:
            pass

        # Use LOCAL augmentation (our orchestrator Qwen3.6) instead of cloud API
        self._local_augment = None
        try:
            from .local_augmentation import LocalLLMAugmentation, LocalAugmentationConfig
            config = LocalAugmentationConfig(
                api_url=self._api_url,
                model=self._model,
            )
            self._local_augment = LocalLLMAugmentation(config)
            # Session is created lazily inside the async augmentation call (which
            # has a running event loop). Creating it here in sync __init__ raises
            # "no running event loop" and silently falls back to cloud.

            # Replace cloud augmentation with local one AFTER Memori is created
            from memori.memory.augmentation.augmentations.memori._augmentation import AdvancedAugmentation
            self.memori.config.augmentation.augmentations.clear()
            self.memori.config.augmentation.augmentations.append(
                LocalAugmentationWrapper(self._local_augment)
            )
            
            # Build DB schema and start augmentation
            self.memori.config.storage.build()
            self.memori.config.augmentation.start(SessionLocal)
            logger.info("Local augmentation initialized with Qwen3.6 on %s", self._api_url)
        except Exception as exc:
            logger.warning("Local augmentation initialization failed, falling back to cloud: %s", exc)
            try:
                self.memori.config.storage.build()
                self.memori.config.augmentation.start(SessionLocal)
            except Exception as exc2:
                logger.warning("Memori BYODB initialization warning: %s", exc2)

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

        # Store for hook
        self._last_user_content = user_content
        self._last_assistant_content = assistant_content
        self._last_session_id = session_id

        # Build conversation messages
        messages = [
            ConversationMessage(role="user", content=user_content),
            ConversationMessage(role="assistant", content=assistant_content),
        ]

        # Enqueue augmentation (goes to local LLM now)
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

    def agent_feedback(self, content: str) -> dict[str, Any]:
        """Send feedback to Memori (BYODB mode: no-op since feedback goes to cloud)."""
        # In BYODB mode, feedback has no local effect.
        # The SDK's agent_feedback() sends to cloud API only.
        return {
            "status": "skipped",
            "reason": "Memori BYODB mode does not support cloud feedback. "
                      "Use the cloud SDK (hermes-memori) for feedback.",
        }

    def agent_compaction(self, params: dict[str, Any]) -> dict[str, Any]:
        """Compaction merges similar memories. BYODB mode: no-op (compaction is cloud-only)."""
        return {
            "status": "skipped",
            "reason": "Compaction is a cloud SDK feature. BYODB mode does not support it.",
        }

    def shutdown(self) -> None:
        try:
            self.memori.close()
        except Exception:
            pass
        try:
            if self._local_augment and self._local_augment._session:
                asyncio.run(self._local_augment._session.close())
        except Exception:
            pass
