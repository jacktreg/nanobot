"""Routing provider that selects between a default (local) and strong (cloud) provider per query."""

import re
from typing import Any

from loguru import logger

from nanobot.providers.base import LLMProvider, LLMResponse

_TRIAGE_SYSTEM = (
    "Rate the complexity of the following user query on a scale of 1-5. "
    "1=simple greeting/chitchat, 2=simple factual question, "
    "3=moderate analysis/explanation, 4=complex reasoning/code generation, "
    "5=expert-level multi-step task. Respond with ONLY a single digit."
)

# Prefixes that trigger a manual override to the strong model.
_STRONG_PREFIXES = ("/think ", "/strong ")


class RoutingProvider(LLMProvider):
    """Wraps a default and a strong provider, routing per query."""

    def __init__(
        self,
        default_provider: LLMProvider,
        strong_provider: LLMProvider,
        strong_model: str,
        trigger: str = "auto",
        threshold: int = 3,
    ):
        # No api_key/api_base needed at this level.
        super().__init__(api_key=None, api_base=None)
        self.default_provider = default_provider
        self.strong_provider = strong_provider
        self.strong_model = strong_model
        self.trigger = trigger
        self.threshold = threshold

    # ------------------------------------------------------------------
    # Routing decision
    # ------------------------------------------------------------------

    def _check_manual_prefix(self, messages: list[dict[str, Any]]) -> tuple[bool, list[dict[str, Any]]]:
        """Check for /think or /strong prefix. Returns (matched, cleaned_messages)."""
        if not messages:
            return False, messages

        last = messages[-1]
        content = last.get("content", "")
        if not isinstance(content, str):
            return False, messages

        for prefix in _STRONG_PREFIXES:
            if content.lower().startswith(prefix):
                cleaned = dict(last)
                cleaned["content"] = content[len(prefix):]
                return True, messages[:-1] + [cleaned]

        return False, messages

    async def _triage(self, messages: list[dict[str, Any]]) -> int:
        """Ask the local model to rate query difficulty 1-5."""
        # Extract the last user message text for triage.
        user_text = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                c = msg.get("content", "")
                user_text = c if isinstance(c, str) else str(c)
                break

        if not user_text:
            return self.threshold  # No user text → default to strong

        triage_messages = [
            {"role": "system", "content": _TRIAGE_SYSTEM},
            {"role": "user", "content": user_text},
        ]

        try:
            resp = await self.default_provider.chat(
                messages=triage_messages,
                tools=None,
                max_tokens=4,
                temperature=0,
            )
            text = (resp.content or "").strip()
            match = re.search(r"[1-5]", text)
            if match:
                score = int(match.group())
                logger.info("Routing triage score: {} (threshold: {})", score, self.threshold)
                return score
            logger.warning("Triage returned unparseable response: {!r}, defaulting to threshold", text)
            return self.threshold
        except Exception as e:
            logger.warning("Triage call failed: {}, defaulting to threshold", e)
            return self.threshold

    async def _should_use_strong(
        self, messages: list[dict[str, Any]]
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Decide whether to route to the strong model.

        Returns (use_strong, possibly_cleaned_messages).
        """
        if self.trigger == "always-strong":
            logger.info("Routing: always-strong → cloud")
            return True, messages

        # Check manual prefix regardless of trigger mode.
        matched, cleaned = self._check_manual_prefix(messages)
        if matched:
            logger.info("Routing: manual override → cloud")
            return True, cleaned

        if self.trigger == "manual":
            return False, messages

        # Auto triage via local model.
        score = await self._triage(messages)
        use_strong = score >= self.threshold
        logger.info("Routing: auto triage → {}", "cloud" if use_strong else "local")
        return use_strong, messages

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
        use_strong, messages = await self._should_use_strong(messages)

        if use_strong:
            return await self.strong_provider.chat(
                messages,
                tools=tools,
                model=self.strong_model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
            )

        return await self.default_provider.chat(
            messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )

    def get_default_model(self) -> str:
        return self.default_provider.get_default_model()
