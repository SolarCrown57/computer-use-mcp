# CLAUDE.md

This file gives repository-specific guidance to Claude Code and similar coding
agents.

## Repository Scope

This workspace contains a computer-use stack centered on two core services:

- `mcp_server/`: exposes desktop control tools through MCP
- `tool_server/`: performs the actual local desktop automation

The following directories are optional surrounding services and are not needed
for the MCP itself:

- `planner/`
- `frontend/`
- `ecs_manager/`

## Recommended Working Focus

When the task is specifically about the computer-use MCP used by an agent,
prefer working in:

- `mcp_server/`
- `tool_server/`
- root docs and startup scripts

Only touch `planner/`, `frontend/`, or `ecs_manager/` when the task clearly
depends on them.

## Local Startup

### MCP only

```powershell
.\start_mcp_only.bat
```

### Full workspace

```powershell
.\start_all.bat
```

## Config Files

Tracked examples:

- `mcp_server/settings.example.toml`
- `tool_server/config.example.toml`
- `planner/config.example.toml`
- `ecs_manager/config.example.toml`

Local machine configs are intentionally ignored:

- `mcp_server/settings.toml`
- `tool_server/config.toml`
- `planner/config.toml`
- `ecs_manager/config.toml`

## Git Hygiene

- Do not commit `.venv`, `node_modules`, `.next`, logs, or `__pycache__`.
- Keep lockfiles unless there is a deliberate dependency update.
- Preserve upstream copyright and license headers in source files.
