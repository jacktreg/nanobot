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

    def _build_triage_prompt(self) -> str:
        tier_descriptions = []
        for t in self.tiers:
            tier_descriptions.append(
                f"- Score {t.min_score}-{t.max_score}: {t.name} (model: {t.model})"
            )
        tiers_text = "\n".join(tier_descriptions)
        return (
            f"Rate the complexity of the following user query on a scale of 1-{self.triage_scale}. "
            f"The tiers are:\n{tiers_text}\n"
            f"Respond with ONLY a single integer from 1 to {self.triage_scale}."
        )

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

        triage_messages = [
            {"role": "system", "content": self._build_triage_prompt()},
            {"role": "user", "content": user_text},
        ]

        try:
            resp = await self.triage_provider.chat(
                messages=triage_messages,
                tools=None,
                max_tokens=1024,
                reasoning_effort=None,
            )
            text = (resp.content or "").strip()
            # Extract any integer from 1 to triage_scale
            match = re.search(r"\d+", text)
            if match:
                score = int(match.group())
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
    ) -> tuple[ResolvedTier, list[dict[str, Any]]]:
        """Decide which tier to route to. Returns (tier, possibly_cleaned_messages)."""
        if self.trigger in ("always-highest", "always-strong"):
            logger.info("Routing: always-highest → '{}'", self._highest_tier.name)
            return self._highest_tier, messages

        # Check manual override regardless of trigger mode.
        override_tier, cleaned = self._check_manual_override(messages)
        if override_tier:
            return override_tier, cleaned

        if self.trigger == "manual":
            return self._lowest_tier, messages

        # Auto triage
        score = await self._triage(messages)
        tier = self._select_tier(score)
        logger.info("Routing: auto triage → tier '{}' (score={})", tier.name, score)
        return tier, messages

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
        tier, messages = await self._route(messages)
        effective_effort = tier.reasoning_effort or reasoning_effort
        logger.info("Routing dispatch → tier '{}' model={} reasoning_effort={}", tier.name, tier.model, effective_effort or "none")

        return await tier.provider.chat(
            messages,
            tools=tools,
            model=tier.model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=effective_effort,
        )

    def get_default_model(self) -> str:
        return self._lowest_tier.model
