"""N-tier routing provider that selects among multiple providers per query."""

import re
from dataclasses import dataclass
from typing import Any

from loguru import logger

from nanobot.providers.base import LLMProvider, LLMResponse


@dataclass
class ResolvedTier:
    """A fully resolved routing tier with its provider instance."""

    name: str
    provider: LLMProvider
    model: str
    min_score: int
    max_score: int
    reasoning_effort: str | None = None


# Prefixes that trigger a manual override to the highest tier.
_HIGHEST_PREFIXES = ("/think ", "/strong ")


class RoutingProvider(LLMProvider):
    """Wraps N provider tiers, routing per query based on triage score."""

    def __init__(
        self,
        triage_provider: LLMProvider,
        tiers: list[ResolvedTier],
        trigger: str = "auto",
        triage_scale: int = 10,
    ):
        super().__init__(api_key=None, api_base=None)
        # tiers must be sorted by min_score ascending
        self.tiers = sorted(tiers, key=lambda t: t.min_score)
        self.triage_provider = triage_provider
        self.trigger = trigger
        self.triage_scale = triage_scale

    @property
    def _lowest_tier(self) -> ResolvedTier:
        return self.tiers[0]

    @property
    def _highest_tier(self) -> ResolvedTier:
        return self.tiers[-1]

    def _build_triage_prompt(self, context_summary: str | None = None) -> str:
        tier_descriptions = []
        for t in self.tiers:
            tier_descriptions.append(
                f"- Score {t.min_score}-{t.max_score}: {t.name}"
            )
        tiers_text = "\n".join(tier_descriptions)
        prompt = (
            f"You are a query complexity scorer. Rate the user's query from 1 to {self.triage_scale}.\n"
            f"1 = simple greeting or trivial question\n"
            f"{self.triage_scale} = complex analysis, multi-step reasoning, or expert knowledge\n"
            f"{', '.join([str(x) for x in range(2, self.triage_scale)])} = require low to moderate reasoning, analysis, or knowledge\n"
            f"Tiers:\n{tiers_text}\n\n"
        )
        if context_summary:
            prompt += (
                f"Recent conversation context:\n{context_summary}\n\n"
                f"Consider this context when scoring — a short follow-up to a complex discussion "
                f"should score similarly to the original discussion.\n\n"
            )
        prompt += "Reply with ONLY a single number, nothing else."
        return prompt

    # ------------------------------------------------------------------
    # Tier selection
    # ------------------------------------------------------------------

    def _select_tier(self, score: int) -> ResolvedTier:
        """Find the tier whose range contains the score, fallback to lowest."""
        for tier in self.tiers:
            if tier.min_score <= score <= tier.max_score:
                return tier
        return self._lowest_tier

    def _find_tier_by_name(self, name: str) -> ResolvedTier | None:
        """Find a tier by name (case-insensitive)."""
        name_lower = name.lower()
        for tier in self.tiers:
            if tier.name.lower() == name_lower:
                return tier
        return None

    # ------------------------------------------------------------------
    # Manual overrides
    # ------------------------------------------------------------------

    def _check_manual_override(
        self, messages: list[dict[str, Any]]
    ) -> tuple[ResolvedTier | None, list[dict[str, Any]]]:
        """Check for /think, /strong, or /tier <name> prefix.

        Returns (tier_override_or_None, cleaned_messages).
        """
        if not messages:
            return None, messages

        last = messages[-1]
        content = last.get("content", "")
        if not isinstance(content, str):
            return None, messages

        content_lower = content.lower()

        # /think or /strong → highest tier
        for prefix in _HIGHEST_PREFIXES:
            if content_lower.startswith(prefix):
                cleaned = dict(last)
                cleaned["content"] = content[len(prefix):]
                logger.info("Routing: manual override → highest tier '{}'", self._highest_tier.name)
                return self._highest_tier, messages[:-1] + [cleaned]

        # /tier <name> → named tier
        match = re.match(r"/tier\s+(\S+)\s*", content, re.IGNORECASE)
        if match:
            tier_name = match.group(1)
            tier = self._find_tier_by_name(tier_name)
            if tier:
                remainder = content[match.end():]
                cleaned = dict(last)
                cleaned["content"] = remainder
                logger.info("Routing: manual override → tier '{}'", tier.name)
                return tier, messages[:-1] + [cleaned]
            else:
                logger.warning("Routing: unknown tier '{}', ignoring override", tier_name)

        return None, messages

    # ------------------------------------------------------------------
    # Conversation context for triage
    # ------------------------------------------------------------------

    def _extract_recent_context(
        self, messages: list[dict[str, Any]]
    ) -> str | None:
        """Extract a compact summary of the most recent exchange for triage context.

        Walks backward through messages (skipping the current user message and
        tool-role messages) to find the last assistant reply and the user message
        before it. Returns a truncated summary string, or None if no history.
        """
        assistant_text: str | None = None
        prior_user_text: str | None = None

        # Skip the last message (the current user query).
        history = messages[:-1] if messages else []

        for msg in reversed(history):
            role = msg.get("role", "")
            content = msg.get("content", "")

            # Skip tool messages and non-string content.
            if role == "tool" or not isinstance(content, str):
                continue

            if role == "assistant" and assistant_text is None:
                assistant_text = content.strip()
            elif role == "user" and assistant_text is not None:
                prior_user_text = content.strip()
                break

        if assistant_text is None:
            return None

        # Strip [Runtime Context ...] prefix from prior user message.
        if prior_user_text:
            if prior_user_text.startswith("[Runtime Context"):
                # Format: tag + newline + metadata, separated from content by \n\n
                parts = prior_user_text.split("\n\n", 1)
                prior_user_text = parts[1].strip() if len(parts) > 1 else ""
            prior_user_text = prior_user_text[:150]

        assistant_text = assistant_text[:300]

        parts = []
        if prior_user_text:
            parts.append(f"Previous user message: {prior_user_text}")
        parts.append(f"Assistant reply: {assistant_text}")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Triage
    # ------------------------------------------------------------------

    async def _triage(self, messages: list[dict[str, Any]]) -> int:
        """Ask the triage provider to rate query difficulty."""
        user_text = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                c = msg.get("content", "")
                user_text = c if isinstance(c, str) else str(c)
                break

        if not user_text:
            return 1

        context_summary = self._extract_recent_context(messages)
        triage_messages = [
            {"role": "system", "content": self._build_triage_prompt(context_summary)},
            {"role": "user", "content": user_text},
        ]

        try:
            resp = await self.triage_provider.chat(
                messages=triage_messages,
                tools=None,
                max_tokens=32,
                temperature=0.0,
                reasoning_effort=None,
            )
            # Check content first, then fall back to reasoning_content
            text = (resp.content or "").strip()
            if not text and resp.reasoning_content:
                text = resp.reasoning_content.strip()
                logger.debug("Triage: content was empty, extracted from reasoning_content")
            logger.debug("Triage raw response: content={!r}, reasoning_content={!r}", resp.content, resp.reasoning_content)
            # Extract the last integer — reasoning models put the answer at the end
            nums = re.findall(r"\d+", text)
            if nums:
                score = int(nums[-1])
                score = max(1, min(score, self.triage_scale))
                logger.info("Routing triage score: {}/{}", score, self.triage_scale)
                return score
            logger.warning("Triage returned unparseable response: {!r}, defaulting to lowest", text)
            return 1
        except Exception as e:
            logger.warning("Triage call failed: {}, defaulting to lowest", e)
            return 1

    # ------------------------------------------------------------------
    # Route decision
    # ------------------------------------------------------------------

    async def _route(
        self, messages: list[dict[str, Any]]
    ) -> tuple[ResolvedTier, list[dict[str, Any]], int | None]:
        """Decide which tier to route to. Returns (tier, possibly_cleaned_messages, score_or_None)."""
        if self.trigger in ("always-highest", "always-strong"):
            logger.info("Routing: always-highest → '{}'", self._highest_tier.name)
            return self._highest_tier, messages, None

        # Check manual override regardless of trigger mode.
        override_tier, cleaned = self._check_manual_override(messages)
        if override_tier:
            return override_tier, cleaned, None

        if self.trigger == "manual":
            return self._lowest_tier, messages, None

        # Auto triage
        score = await self._triage(messages)
        tier = self._select_tier(score)
        logger.info("Routing: auto triage → tier '{}' (score={})", tier.name, score)
        return tier, messages, score

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        tier, messages, score = await self._route(messages)
        effective_effort = tier.reasoning_effort or reasoning_effort
        logger.info("Routing dispatch → tier '{}' model={} api_base={} reasoning_effort={}", tier.name, tier.model, tier.provider.api_base or "default", effective_effort or "none")

        response = await tier.provider.chat(
            messages,
            tools=tools,
            model=tier.model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=effective_effort,
        )
        response.metadata["_routing"] = {
            "tier": tier.name,
            "score": score,
            "scale": self.triage_scale,
        }
        return response

    def get_default_model(self) -> str:
        return self._lowest_tier.model
