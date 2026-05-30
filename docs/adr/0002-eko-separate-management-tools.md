# Eko management tools: separate tools, not action-dispatch

Eko's three management operations (`create_group`, `create_topic`, `query_users`) are exposed as three separate Hermes tools (`eko_create_group`, `eko_create_topic`, `eko_query_users`) rather than a single `eko_management` tool with an `action` parameter. This differs from the Discord adapter, which uses a single `discord` tool with ~20 actions.

**Considered:** Single `eko_management(action=...)` tool matching the Discord pattern.

**Why not:**
- Eko has 3 management operations with no realistic path to 10+. The single-tool pattern exists to prevent tool-list flooding when you have 20+ actions — that problem doesn't apply here.
- Separate tools give each operation a focused schema. The model sees exactly the parameters it needs, not a merged schema where `create_group` params sit alongside `query_users` params.
- Tool names are self-documenting — `eko_create_group` in the tool list is immediately clear. With action-dispatch, the model has to read the description to discover capabilities.
- No dynamic schema filtering needed. Discord's single-tool design requires intent detection + config allowlist (~100 lines of plumbing) to hide irrelevant actions from the model. That complexity isn't justified for 3 operations.

Discord stays as-is — refactoring 20+ actions into separate tools would be a breaking change for no benefit. Each platform uses the shape that fits its API surface.

**Status:** Accepted (2026-05-30)
