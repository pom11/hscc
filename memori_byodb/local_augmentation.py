"""Memory augmentation backed by a local OpenAI-compatible LLM (e.g. the
cluster orchestrator) instead of a cloud API — keeps memory fully offline.

Endpoint + model are read from the environment so no host/topology is baked in:
  HSCC_MEMORI_AUGMENT_URL   chat-completions URL (default: the local proxy on
                            :4000, which load-balances across every serving
                            unit — NOT a single node's :8000, which pins
                            augmentation to one GPU and breaks outright when
                            autodown frees that node)
  HSCC_MEMORI_AUGMENT_MODEL model name served at that URL (default:
                            worker-model, the role alias every serving unit
                            advertises)
  HSCC_MEMORI_EMBED_MODEL   local fastembed model the facts are embedded with
                            (default: all-MiniLM-L6-v2, the memori SDK's own
                            default, which recall embeds the query with)
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# Generic, env-overridable defaults — point at whatever local LLM you serve.
# The proxy (:4000), not a node's own vLLM port (:8000): the proxy fronts every
# serving unit, so augmentation balances and survives any single node going
# away. `worker-model` over `local-model` because the role alias is actually
# advertised by the fleet — `local-model` was served by nothing, so the default
# only ever worked when the env overrode it.
DEFAULT_API_URL = os.environ.get(
    "HSCC_MEMORI_AUGMENT_URL", "http://localhost:4000/v1/chat/completions")
DEFAULT_MODEL = os.environ.get("HSCC_MEMORI_AUGMENT_MODEL", "worker-model")
# Must match the model memori.recall embeds the query with (the SDK's
# Config.embeddings.model): a fact embedded with another model has another
# dimension and cannot be compared.
DEFAULT_EMBED_MODEL = os.environ.get("HSCC_MEMORI_EMBED_MODEL", "all-MiniLM-L6-v2")


def sdk_embed(texts: list[str], model: str) -> list[list[float]]:
    """Embed with the memori SDK's local fastembed model (no network)."""
    from memori.embeddings import embed_texts

    return embed_texts(texts, model=model)


@dataclass
class LocalAugmentationConfig:
    """Configuration for local LLM-based augmentation."""
    api_url: str = DEFAULT_API_URL
    model: str = DEFAULT_MODEL
    max_tokens: int = 2048
    temperature: float = 0.3
    timeout_seconds: int = 120
    embed_model: str = DEFAULT_EMBED_MODEL


class LocalLLMAugmentation:
    """Uses our local orchestrator LLM for conversation fact extraction.
    
    Replaces the cloud-based AdvancedAugmentation with a call to a local
    OpenAI-compatible LLM endpoint (e.g. the cluster orchestrator).
    """

    def __init__(self, config: LocalAugmentationConfig | None = None) -> None:
        self.config = config or LocalAugmentationConfig()
        self._session: aiohttp.ClientSession | None = None
        self._embed = sdk_embed

    async def __aenter__(self) -> "LocalLLMAugmentation":
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def process(self, ctx, driver) -> None:
        """Run augmentation using our local orchestrator LLM.
        
        Modified from SDK's AdvancedAugmentation.process() to call local LLM.
        This method modifies ctx in-place (same pattern as SDK).
        """
        if not self._session:
            # Create the aiohttp session lazily, inside the running event loop —
            # it cannot be created at __init__ (sync, no loop), which previously
            # forced a silent fallback to the cloud augmentation.
            self._session = aiohttp.ClientSession()
        if not ctx.payload.conversation_messages:
            return

        # Build prompt for fact extraction
        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(ctx)

        # Call local LLM
        response = await self._call_local_llm(system_prompt, user_prompt)
        if not response:
            return

        # Parse response into memories
        memories = self._parse_response(response)
        
        # Store in context (same as cloud augmentation)
        if memories:
            ctx.data["memories"] = memories

            # Persist captured facts + conversation directly via the driver.
            #
            # The SDK's AdvancedAugmentation._schedule_* helpers are a poor fit
            # here: _schedule_entity_writes takes a `Memories` object (not the
            # list of plain dicts `_parse_response` returns) and, more
            # importantly, none of the helpers ever create the conversation or
            # message rows — and the payload's `conversation_id` is a session
            # *string*, not the numeric conversation row id those helpers
            # expect. So we bypass them and write the rows ourselves. This is
            # what makes capture actually land rows in memori_conversation /
            # memori_conversation_message / memori_entity_fact.
            fact_texts = [m["content"] for m in memories]

            # 1. Ensure the entity exists (idempotent by external_id).
            entity_id = driver.entity.create(ctx.payload.entity_id)

            # 2. Create/reuse the conversation for this session and write its
            #    messages. conversation.create returns the numeric row id.
            conversation_id = None
            session_id = ctx.payload.conversation_id
            if session_id:
                conversation_id = driver.conversation.create(session_id, 30)
                if conversation_id:
                    for msg in ctx.payload.conversation_messages or []:
                        m_role = getattr(msg, "role", None)
                        m_content = getattr(msg, "content", None)
                        if m_role and m_content:
                            driver.conversation.message.create(
                                conversation_id,
                                m_role,
                                getattr(msg, "type", "text") or "text",
                                m_content,
                            )

            # 3. Embed the facts, then write them linked to the conversation via
            #    mentions. A fact stored without an embedding is not just
            #    unfindable: memori.recall refuses the whole query
            #    (missing_embedding_data), so an embedding failure writes no facts.
            fact_embeddings = None
            if fact_texts:
                try:
                    fact_embeddings = self._embed(fact_texts, self.config.embed_model)
                except Exception as exc:
                    logger.error(f"Fact embedding failed; {len(fact_texts)} facts not stored: {exc}")
                if fact_embeddings is not None and (
                        len(fact_embeddings) != len(fact_texts) or not all(fact_embeddings)):
                    logger.error("Fact embedding returned an empty or short result; facts not stored")
                    fact_embeddings = None
            if entity_id and fact_texts and conversation_id and fact_embeddings:
                driver.entity_fact.create(
                    entity_id, fact_texts, fact_embeddings, conversation_id
                )

            logger.info(f"Local augmentation extracted {len(memories)} facts")

    def _build_system_prompt(self) -> str:
        return """You are a knowledge extraction engine. Given a conversation between a user and assistant, extract all persistent facts worth remembering.

RULES:
1. Extract ONLY factual information, not opinions or transient chat
2. Focus on: decisions made, preferences stated, project details, technical facts, constraints, prior outcomes, user info
3. Each fact must be: self-contained, verifiable, useful for future recall
4. Group related facts about the same subject
5. DO NOT extract: greetings, casual chat, tool outputs, code snippets (unless they encode a fact)

Respond with ONLY a JSON array of facts. Each fact has:
- "subject": what the fact is about (person, project, tool, concept)
- "predicate": the relationship or action
- "object": what is true about the subject
- "confidence": 0.0-1.0 (how certain we are)

Example output:
[
  {"subject": "user", "predicate": "prefers", "object": "concise responses with no fluff", "confidence": 0.95},
  {"subject": "project", "predicate": "runs on", "object": "DGX Spark GPU cluster", "confidence": 1.0}
]

CRITICAL: Return ONLY the JSON array, nothing else. No markdown, no explanation."""

    def _build_user_prompt(self, ctx) -> str:
        # Format conversation
        payload = ctx.payload
        msgs = []
        for msg in payload.conversation_messages:
            role = getattr(msg, "role", "unknown")
            content = getattr(msg, "content", "")
            if content:
                msgs.append(f"[{role}]: {content}")

        system_content = getattr(payload, "system_prompt", "") or ""

        prompt = "Extract facts from this conversation.\n\n"
        if system_content:
            prompt += f"System context: {system_content[:500]}\n\n"
        prompt += "Conversation:\n" + "\n".join(msgs[-20:])  # Last 20 messages
        prompt += "\n\nRespond with JSON only."
        return prompt

    async def _call_local_llm(self, system_prompt: str, user_prompt: str) -> str | None:
        """Call our orchestrator's Qwen3.6 LLM."""
        try:
            async with self._session.post(
                self.config.api_url,
                json={
                    "model": self.config.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": self.config.max_tokens,
                    "temperature": self.config.temperature,
                    "top_p": 0.95,
                    "extra_body": {
                        "reasoning": {"enable": False}
                    }
                },
                timeout=aiohttp.ClientTimeout(total=self.config.timeout_seconds)
            ) as resp:
                if resp.status != 200:
                    logger.error(f"LLM call failed: {resp.status}")
                    return None
                data = await resp.json()

                # Qwen3-Coder returns content in reasoning field when content is null
                choice = data.get("choices", [{}])[0]
                message = choice.get("message", {})
                content = message.get("content") or ""
                reasoning = message.get("reasoning") or ""

                # Use reasoning when content is empty (Qwen3-Coder behavior)
                return content or reasoning

        except Exception as exc:
            logger.error(f"Local LLM augmentation failed: {exc}", exc_info=True)
            return None

    def _parse_response(self, response: str) -> list[dict[str, Any]]:
        """Parse LLM response into memories (entity facts).

        The fact list is found wherever the model put it: bare, inside a
        ```json fence (closed or not), or after reasoning text. A reply with no
        parseable list stores NOTHING. It used to store the whole reply as one
        "Extraction: ..." fact, which filled memory with the model's reasoning
        and unparsed JSON (376 of 3068 live facts on 2026-11-21).
        """
        facts = self._extract_fact_list(response)
        if facts is None:
            logger.warning(f"No fact list in LLM response; nothing stored: {response.strip()[:200]}")
            return []

        memories = []
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            subject = str(fact.get("subject", ""))
            predicate = str(fact.get("predicate", ""))
            obj = str(fact.get("object", ""))
            try:
                confidence = float(fact.get("confidence", 0.8))
            except (TypeError, ValueError):
                confidence = 0.8

            if not subject or not obj:
                continue

            memories.append({
                "content": f"{subject} {predicate} {obj}",
                "metadata": {
                    "subject": subject,
                    "predicate": predicate,
                    "object": obj,
                    "confidence": confidence,
                    "extraction_method": "local_llm",
                }
            })
        return memories

    @staticmethod
    def _extract_fact_list(response: str) -> list | None:
        """The JSON array in ``response``, or None when there is none."""
        text = response.strip()
        candidates = [text]
        fence = re.search(r"```(?:json)?\s*(.*?)(?:```|$)", text, re.DOTALL)
        if fence:
            candidates.append(fence.group(1).strip())
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end > start:
            candidates.append(text[start:end + 1])
        for candidate in candidates:
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, list):
                return value
        return None
