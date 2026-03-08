# Nanobot Fork — Integration Manifest

## Fork Relationship

- **Origin**: `jacktreg/nanobot` (this repo)
- **Upstream**: `HKUDS/nanobot`
- **Merge strategy**: Regularly merge `upstream/main` into `main`
- **Remotes**: `origin` = fork, `upstream` = HKUDS/nanobot

## Fork-Only Feature: `web_browse` Headless Browser Tool

This fork adds a Camoufox-based headless browser tool (`web_browse`) that will **never** be merged upstream. After every upstream sync, verify these integration points are intact.

### Fork-Only Files (safe from upstream conflicts)

| File | Purpose |
|------|---------|
| `nanobot/agent/tools/browse.py` | `WebBrowseTool` implementation |
| `tests/test_browse_tool.py` | Browse tool tests |
| `nanobot/providers/routing_provider.py` | Routing provider (fork-only) |

### Integration Points in Upstream Files

These upstream files contain fork modifications that may conflict on merge:

| File | What we added | Lines to watch |
|------|---------------|----------------|
| `nanobot/config/schema.py` | `WebBrowseToolConfig` class + `browse` field on `WebToolsConfig` | Search for `WebBrowseToolConfig` and `browse:` |
| `nanobot/cli/commands.py` | `browse_config=config.tools.web.browse` kwarg (3 call sites) | Search for `browse_config=` |
| `nanobot/agent/loop.py` | `browse_config` param in `AgentLoop.__init__`, tool registration block | Search for `browse_config` |
| `nanobot/agent/subagent.py` | `browse_config` param in `SubagentManager.__init__`, tool registration block | Search for `browse_config` |
| `config.example.json` | `browse` section under `tools.web` | Check `tools.web.browse` key |

### Critical Upstream API Dependencies

`browse.py` imports from `nanobot.agent.tools.web`:
- `_validate_url(url: str) -> tuple[bool, str | None]`
- `_strip_tags(html: str) -> str`
- `_normalize(text: str) -> str`

If upstream renames/removes these, `browse.py` breaks immediately.

## Quick Verification Commands

```bash
# Import check
python -c "from nanobot.agent.tools.browse import WebBrowseTool; print('OK')"

# Schema check
python -c "from nanobot.config.schema import WebBrowseToolConfig, WebToolsConfig; assert hasattr(WebToolsConfig(), 'browse'); print('OK')"

# Constructor check
python -c "import inspect; from nanobot.agent.loop import AgentLoop; assert 'browse_config' in inspect.signature(AgentLoop.__init__).parameters; print('OK')"

# API dep check
python -c "from nanobot.agent.tools.web import _validate_url, _strip_tags, _normalize; print('OK')"

# CLI call sites
python -c "import ast, sys; tree=ast.parse(open('nanobot/cli/commands.py').read()); count=sum(1 for node in ast.walk(tree) if isinstance(node, ast.keyword) and node.arg=='browse_config'); print(f'{count} call sites'); sys.exit(0 if count>=3 else 1)"

# Tests
uv run pytest tests/test_browse_tool.py -v
```

## Sync Command

Run `/sync-upstream` to fetch, merge, and verify compatibility with upstream.
