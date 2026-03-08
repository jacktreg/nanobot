"""Tests for the WebBrowseTool (headless browser)."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.tools.browse import WebBrowseTool
from nanobot.config.schema import WebBrowseToolConfig, WebToolsConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool(tmp_path: Path, page=None, max_chars: int = 500) -> WebBrowseTool:
    """Create WebBrowseTool with pre-injected mock page (skips real browser launch)."""
    tool = WebBrowseTool(workspace=tmp_path, max_chars=max_chars)
    if page is not None:
        tool._page = page
        tool._camoufox = MagicMock()
    return tool


def _mock_page(
    url: str = "https://example.com/final",
    title: str = "Test Page",
    status: int = 200,
    html: str = "<html><body>Hello</body></html>",
) -> AsyncMock:
    """Build an AsyncMock that mimics a Playwright page."""
    mock_response = AsyncMock()
    mock_response.status = status

    page = AsyncMock()
    page.goto = AsyncMock(return_value=mock_response)
    page.title = AsyncMock(return_value=title)
    page.url = url
    page.content = AsyncMock(return_value=html)
    page.screenshot = AsyncMock()
    page.evaluate = AsyncMock(return_value=42)
    return page


# ===========================================================================
# Group 1: Parameter Validation (sync)
# ===========================================================================

class TestParameterValidation:
    def test_browse_validate_missing_action(self):
        tool = WebBrowseTool(workspace=Path("/tmp"))
        errors = tool.validate_params({})
        assert any("action" in e for e in errors)

    def test_browse_validate_invalid_action(self):
        tool = WebBrowseTool(workspace=Path("/tmp"))
        errors = tool.validate_params({"action": "fly"})
        assert any("must be one of" in e for e in errors)

    def test_browse_validate_navigate_valid(self):
        tool = WebBrowseTool(workspace=Path("/tmp"))
        errors = tool.validate_params({"action": "navigate", "url": "https://x.com"})
        assert errors == []

    def test_browse_validate_timeout_range(self):
        tool = WebBrowseTool(workspace=Path("/tmp"))
        errors = tool.validate_params({"action": "navigate", "timeout": 500})
        assert any(">= 1000" in e for e in errors)


# ===========================================================================
# Group 2: Action Dispatch (async, mock Playwright)
# ===========================================================================

class TestNavigate:
    async def test_navigate_success(self, tmp_path):
        page = _mock_page()
        tool = _make_tool(tmp_path, page=page)

        with patch.object(tool, "_ensure_browser", new_callable=AsyncMock):
            result = json.loads(await tool.execute(action="navigate", url="https://example.com"))

        assert result["ok"] is True
        assert result["status"] == 200
        assert result["finalUrl"] == "https://example.com/final"
        assert result["title"] == "Test Page"

    async def test_navigate_missing_url(self, tmp_path):
        tool = _make_tool(tmp_path)
        result = json.loads(await tool.execute(action="navigate"))
        assert "url is required" in result["error"]

    async def test_navigate_invalid_url(self, tmp_path):
        tool = _make_tool(tmp_path)
        result = json.loads(await tool.execute(action="navigate", url="ftp://x"))
        assert "URL validation failed" in result["error"]


class TestGetContent:
    async def test_get_content_no_page(self, tmp_path):
        tool = _make_tool(tmp_path)  # no page injected
        result = json.loads(await tool.execute(action="get_content"))
        assert "No page loaded" in result["error"]

    async def test_get_content_extracts_text(self, tmp_path):
        page = _mock_page(html="<html><body><p>Hello world</p></body></html>")
        tool = _make_tool(tmp_path, page=page)

        mock_doc = MagicMock()
        mock_doc.summary.return_value = "<p>Hello world</p>"
        mock_doc.title.return_value = "Test Page"

        with patch("readability.Document", return_value=mock_doc):
            result = json.loads(await tool.execute(action="get_content"))

        assert "text" in result
        assert "title" in result
        assert "truncated" in result
        assert "length" in result

    async def test_get_content_truncation(self, tmp_path):
        page = _mock_page(html="<html><body><p>" + "A" * 200 + "</p></body></html>")
        tool = _make_tool(tmp_path, page=page, max_chars=50)

        mock_doc = MagicMock()
        mock_doc.summary.return_value = "<p>" + "A" * 200 + "</p>"
        mock_doc.title.return_value = "T"

        with patch("readability.Document", return_value=mock_doc):
            result = json.loads(await tool.execute(action="get_content"))

        assert result["truncated"] is True
        assert result["length"] <= 50


class TestScreenshot:
    async def test_screenshot_saves_file(self, tmp_path):
        page = _mock_page()
        tool = _make_tool(tmp_path, page=page)

        result = json.loads(await tool.execute(action="screenshot"))

        assert result["ok"] is True
        assert "path" in result
        assert str(tmp_path / "screenshots") in result["path"]
        page.screenshot.assert_awaited_once()

    async def test_screenshot_no_page(self, tmp_path):
        tool = _make_tool(tmp_path)
        result = json.loads(await tool.execute(action="screenshot"))
        assert "No page loaded" in result["error"]


class TestExecuteJs:
    async def test_execute_js_success(self, tmp_path):
        page = _mock_page()
        tool = _make_tool(tmp_path, page=page)

        result = json.loads(await tool.execute(action="execute_js", script="1 + 1"))

        assert result["ok"] is True
        assert result["result"] == 42
        page.evaluate.assert_awaited_once_with("1 + 1")

    async def test_execute_js_no_script(self, tmp_path):
        page = _mock_page()
        tool = _make_tool(tmp_path, page=page)
        result = json.loads(await tool.execute(action="execute_js"))
        assert "script is required" in result["error"]


class TestCloseAndMisc:
    async def test_close_action(self, tmp_path):
        page = _mock_page()
        tool = _make_tool(tmp_path, page=page)
        camoufox_mock = tool._camoufox  # save ref before close() clears it

        result = json.loads(await tool.execute(action="close"))

        assert result["ok"] is True
        assert result["message"] == "Browser closed"
        camoufox_mock.__aexit__.assert_awaited_once()

    async def test_unknown_action(self, tmp_path):
        tool = _make_tool(tmp_path)
        result = json.loads(await tool.execute(action="fly"))
        assert result["error"] == "Unknown action: fly"

    async def test_error_returns_json(self, tmp_path):
        page = _mock_page()
        page.goto = AsyncMock(side_effect=Exception("timeout"))
        tool = _make_tool(tmp_path, page=page)

        with patch.object(tool, "_ensure_browser", new_callable=AsyncMock):
            result = json.loads(await tool.execute(action="navigate", url="https://example.com"))

        assert result["error"] == "timeout"


# ===========================================================================
# Group 3: Config Schema (sync)
# ===========================================================================

class TestConfigSchema:
    def test_browse_config_defaults(self):
        cfg = WebBrowseToolConfig()
        assert cfg.enabled is False
        assert cfg.max_chars == 50000
        assert cfg.screenshot_dir == "screenshots"

    def test_browse_config_camel_case(self):
        cfg = WebBrowseToolConfig(**{"maxChars": 1000, "screenshotDir": "snaps"})
        assert cfg.max_chars == 1000
        assert cfg.screenshot_dir == "snaps"

    def test_web_tools_config_has_browse(self):
        cfg = WebToolsConfig()
        assert hasattr(cfg, "browse")
        assert isinstance(cfg.browse, WebBrowseToolConfig)


# ===========================================================================
# Group 4: Registration Guard (async)
# ===========================================================================

class TestRegistrationGuard:
    async def test_browse_not_registered_when_disabled(self, tmp_path):
        from nanobot.agent.loop import AgentLoop

        mock_bus = MagicMock()
        mock_provider = MagicMock()
        mock_provider.get_default_model.return_value = "test/model"

        loop = AgentLoop(
            bus=mock_bus,
            provider=mock_provider,
            workspace=tmp_path,
            browse_config=WebBrowseToolConfig(enabled=False),
        )
        assert loop.tools.get("web_browse") is None

    async def test_browse_import_error_graceful(self, tmp_path):
        from nanobot.agent.loop import AgentLoop

        mock_bus = MagicMock()
        mock_provider = MagicMock()
        mock_provider.get_default_model.return_value = "test/model"

        with patch(
            "nanobot.agent.tools.browse.WebBrowseTool",
            side_effect=ImportError("no camoufox"),
        ):
            loop = AgentLoop(
                bus=mock_bus,
                provider=mock_provider,
                workspace=tmp_path,
                browse_config=WebBrowseToolConfig(enabled=True),
            )
        assert loop.tools.get("web_browse") is None
