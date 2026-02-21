"""Headless browser tool using Camoufox for JS-rendered pages."""

import json
from pathlib import Path
from typing import Any

from camoufox.async_api import AsyncCamoufox
from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.web import _validate_url, _strip_tags, _normalize


class WebBrowseTool(Tool):
    """Browse web pages with a headless Camoufox browser (JS-capable)."""

    name = "web_browse"
    description = (
        "Control a headless browser to interact with JS-rendered web pages. "
        "Use this for SPAs, JS-heavy dashboards, and dynamically-loaded content. "
        "Prefer web_fetch for plain HTML pages.\n\n"
        "Actions:\n"
        "- navigate: Load a URL in the browser\n"
        "- get_content: Extract rendered page content\n"
        "- screenshot: Save a full-page screenshot\n"
        "- execute_js: Run a JavaScript expression\n"
        "- close: Shut down the browser"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["navigate", "get_content", "screenshot", "execute_js", "close"],
                "description": "The browser action to perform",
            },
            "url": {
                "type": "string",
                "description": "URL to navigate to (navigate action)",
            },
            "waitUntil": {
                "type": "string",
                "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                "description": "When to consider navigation done (default: load)",
            },
            "timeout": {
                "type": "integer",
                "description": "Navigation timeout in milliseconds (default: 30000)",
                "minimum": 1000,
                "maximum": 120000,
            },
            "extractMode": {
                "type": "string",
                "enum": ["text", "markdown"],
                "description": "Content extraction mode (default: text)",
            },
            "maxChars": {
                "type": "integer",
                "description": "Max characters to return (default: config value)",
                "minimum": 100,
            },
            "filename": {
                "type": "string",
                "description": "Screenshot filename (default: auto-generated)",
            },
            "script": {
                "type": "string",
                "description": "JavaScript expression to evaluate (execute_js action)",
            },
        },
        "required": ["action"],
    }

    def __init__(
        self,
        workspace: Path,
        max_chars: int = 50000,
        screenshot_dir: str = "screenshots",
    ):
        self.workspace = workspace
        self.max_chars = max_chars
        self.screenshot_dir = screenshot_dir
        self._camoufox = None
        self._page = None

    async def _ensure_browser(self) -> None:
        """Lazily launch Camoufox on first use."""
        if self._page is not None:
            return

        self._camoufox = AsyncCamoufox(headless=True)
        browser = await self._camoufox.__aenter__()
        self._page = await browser.new_page()
        logger.debug("WebBrowseTool: Camoufox launched")

    async def close(self) -> None:
        """Shut down the Camoufox browser."""
        if self._camoufox:
            try:
                await self._camoufox.__aexit__(None, None, None)
            except Exception:
                pass
            self._camoufox = None
        self._page = None
        logger.debug("WebBrowseTool: browser closed")

    async def execute(self, action: str, **kwargs: Any) -> str:
        try:
            if action == "navigate":
                return await self._navigate(**kwargs)
            elif action == "get_content":
                return await self._get_content(**kwargs)
            elif action == "screenshot":
                return await self._screenshot(**kwargs)
            elif action == "execute_js":
                return await self._execute_js(**kwargs)
            elif action == "close":
                await self.close()
                return json.dumps({"ok": True, "message": "Browser closed"})
            else:
                return json.dumps({"error": f"Unknown action: {action}"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _navigate(
        self,
        url: str | None = None,
        waitUntil: str = "load",
        timeout: int = 30000,
        **_: Any,
    ) -> str:
        if not url:
            return json.dumps({"error": "url is required for navigate action"})

        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url})

        await self._ensure_browser()
        response = await self._page.goto(url, wait_until=waitUntil, timeout=timeout)

        status = response.status if response else None
        return json.dumps({
            "ok": True,
            "url": url,
            "finalUrl": self._page.url,
            "status": status,
            "title": await self._page.title(),
        })

    async def _get_content(
        self,
        extractMode: str = "text",
        maxChars: int | None = None,
        **_: Any,
    ) -> str:
        if self._page is None:
            return json.dumps({"error": "No page loaded. Use navigate first."})

        max_chars = maxChars or self.max_chars
        title = await self._page.title()
        page_url = self._page.url
        html_content = await self._page.content()

        from readability import Document

        doc = Document(html_content)
        if extractMode == "markdown":
            text = self._to_markdown(doc.summary())
        else:
            text = _normalize(_strip_tags(doc.summary()))

        if doc.title():
            text = f"# {doc.title()}\n\n{text}"

        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]

        return json.dumps({
            "url": page_url,
            "title": title,
            "extractor": "readability",
            "truncated": truncated,
            "length": len(text),
            "text": text,
        }, ensure_ascii=False)

    async def _screenshot(
        self,
        filename: str | None = None,
        **_: Any,
    ) -> str:
        if self._page is None:
            return json.dumps({"error": "No page loaded. Use navigate first."})

        import time

        screenshot_dir = self.workspace / self.screenshot_dir
        screenshot_dir.mkdir(parents=True, exist_ok=True)

        if not filename:
            filename = f"screenshot_{int(time.time())}.png"
        if not filename.endswith(".png"):
            filename += ".png"

        path = screenshot_dir / filename
        await self._page.screenshot(path=str(path), full_page=True)

        return json.dumps({
            "ok": True,
            "path": str(path),
            "url": self._page.url,
        })

    async def _execute_js(
        self,
        script: str | None = None,
        **_: Any,
    ) -> str:
        if not script:
            return json.dumps({"error": "script is required for execute_js action"})
        if self._page is None:
            return json.dumps({"error": "No page loaded. Use navigate first."})

        result = await self._page.evaluate(script)
        return json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str)

    @staticmethod
    def _to_markdown(html_str: str) -> str:
        """Convert HTML to markdown (same approach as WebFetchTool)."""
        import re
        text = re.sub(
            r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
            lambda m: f'[{_strip_tags(m[2])}]({m[1]})',
            html_str, flags=re.I,
        )
        text = re.sub(
            r'<h([1-6])[^>]*>([\s\S]*?)</h\1>',
            lambda m: f'\n{"#" * int(m[1])} {_strip_tags(m[2])}\n',
            text, flags=re.I,
        )
        text = re.sub(r'<li[^>]*>([\s\S]*?)</li>', lambda m: f'\n- {_strip_tags(m[1])}', text, flags=re.I)
        text = re.sub(r'</(p|div|section|article)>', '\n\n', text, flags=re.I)
        text = re.sub(r'<(br|hr)\s*/?>', '\n', text, flags=re.I)
        return _normalize(_strip_tags(text))
