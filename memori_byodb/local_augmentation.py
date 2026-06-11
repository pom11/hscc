"""Memory augmentation backed by a local OpenAI-compatible LLM (e.g. the
cluster orchestrator) instead of a cloud API — keeps memory fully offline.

Endpoint + model are read from the environment so no host/topology is baked in:
  HSCC_MEMORI_AUGMENT_URL   chat-completions URL (default: localhost:8000)
  HSCC_MEMORI_AUGMENT_MODEL model name served at that URL
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
DEFAULT_API_URL = os.environ.get(
    "HSCC_MEMORI_AUGMENT_URL", "http://localhost:8000/v1/chat/completions")
DEFAULT_MODEL = os.environ.get("HSCC_MEMORI_AUGMENT_MODEL", "local-model")


@dataclass
class LocalAugmentationConfig:
    """Configuration for local LLM-based augmentation."""
    api_url: str = DEFAULT_API_URL
    model: str = DEFAULT_MODEL
    max_tokens: int = 2048
    temperature: float = 0.3
    timeout_seconds: int = 120


class LocalLLMAugmentation:
    """Uses our local orchestrator LLM for conversation fact extraction.
    
    Replaces the cloud-based AdvancedAugmentation with a call to a local
    OpenAI-compatible LLM endpoint (e.g. the cluster orchestrator).
    """

    def __init__(self, config: LocalAugmentationConfig | None = None) -> None:
        self.config = config or LocalAugmentationConfig()
        self._session: aiohttp.ClientSession | None = None

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

            # Schedule DB writes (same as cloud)
            from memori.memory.augmentation.augmentations.memori._augmentation import AdvancedAugmentation
            aug = AdvancedAugmentation()
            await aug._schedule_entity_writes(ctx, driver, memories)
            await aug._schedule_process_writes(ctx, driver, memories)
            await aug._schedule_conversation_writes(ctx, memories)

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
        """Parse LLM response into memories (entity facts)."""
        # Try to extract JSON from response (handle markdown code blocks)
        text = response.strip()
        if "```" in text:
            match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
            if match:
                text = match.group(1)

        try:
            facts = json.loads(text)
            if not isinstance(facts, list):
                return []

            memories = []
            for fact in facts:
                if not isinstance(fact, dict):
                    continue
                subject = str(fact.get("subject", ""))
                predicate = str(fact.get("predicate", ""))
                obj = str(fact.get("object", ""))
                confidence = float(fact.get("confidence", 0.8))

                if not subject or not obj:
                    continue

                # Construct content string
                content = f"{subject} {predicate} {obj}"

                memories.append({
                    "content": content,
                    "metadata": {
                        "subject": subject,
                        "predicate": predicate,
                        "object": obj,
                        "confidence": confidence,
                        "extraction_method": "local_llm",
                    }
                })

            return memories

        except json.JSONDecodeError:
            logger.warning(f"Failed to parse LLM response as JSON: {text[:200]}")
            # Fallback: treat entire response as one fact
            if response.strip():
                return [{
                    "content": f"Extraction: {response.strip()}",
                    "metadata": {"extraction_method": "local_llm", "fallback": True}
                }]
            return []
