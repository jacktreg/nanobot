# Sync Upstream

Fetch and merge upstream/main, then verify all fork integration points are intact.

## Steps

### 1. Fetch upstream and show new commits

Run `git fetch upstream` then `git log HEAD..upstream/main --oneline` to see incoming changes.

If there are no new commits, report "Already up to date with upstream/main" and stop.

### 2. Pre-merge risk analysis

Run `git diff HEAD...upstream/main -- nanobot/cli/commands.py nanobot/config/schema.py nanobot/agent/loop.py nanobot/agent/subagent.py nanobot/agent/tools/web.py` to check if upstream changed any files that contain our fork integration points.

Report which files have upstream changes and flag any that touch lines near our `browse_config` additions.

### 3. Merge upstream/main

Run `git merge upstream/main`. If there are conflicts:
- For files in the integration points table (see CLAUDE.md), resolve by preserving BOTH upstream changes AND our fork additions (browse_config params, WebBrowseToolConfig, etc.)
- For fork-only files, keep ours
- For all other files, keep upstream

### 4. Run compatibility checks

Run each of these Python one-liners and report pass/fail:

```bash
# Import check
python -c "from nanobot.agent.tools.browse import WebBrowseTool; print('PASS: browse import')"

# Schema check
python -c "from nanobot.config.schema import WebBrowseToolConfig, WebToolsConfig; assert hasattr(WebToolsConfig(), 'browse'); print('PASS: schema')"

# Constructor check — AgentLoop
python -c "import inspect; from nanobot.agent.loop import AgentLoop; assert 'browse_config' in inspect.signature(AgentLoop.__init__).parameters; print('PASS: AgentLoop constructor')"

# Constructor check — SubagentManager
python -c "import inspect; from nanobot.agent.subagent import SubagentManager; assert 'browse_config' in inspect.signature(SubagentManager.__init__).parameters; print('PASS: SubagentManager constructor')"

# API dependency check
python -c "from nanobot.agent.tools.web import _validate_url, _strip_tags, _normalize; print('PASS: API deps')"

# CLI call sites (expect >= 3 browse_config= keyword args)
python -c "
import ast, sys
tree = ast.parse(open('nanobot/cli/commands.py').read())
count = sum(1 for node in ast.walk(tree) if isinstance(node, ast.keyword) and node.arg == 'browse_config')
print(f'PASS: {count} CLI call sites' if count >= 3 else f'FAIL: only {count} CLI call sites (expected >= 3)')
sys.exit(0 if count >= 3 else 1)
"
```

### 5. Run tests

Run `uv run pytest tests/test_browse_tool.py -v`.

### 6. Fix issues

If any check failed:
1. Read the relevant file and the CLAUDE.md integration points table
2. Fix the issue (re-add missing params, update imports, etc.)
3. Re-run the failing check to confirm the fix
4. Stage and commit the fix

### 7. Summary

Report a table of all checks with PASS/FAIL status, plus any fixes applied.
