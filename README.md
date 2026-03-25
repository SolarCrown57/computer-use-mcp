# Computer Use MCP Workspace

This repository is a cleaned, GitHub-ready workspace for the computer-use MCP
stack currently used by the local agent environment.

The core runnable pieces are:

- `mcp_server/`: MCP server exposed to MCP clients
- `tool_server/`: desktop automation backend used by the MCP server

Optional demo components are also kept in this workspace:

- `planner/`: agent planner service
- `frontend/`: web UI
- `ecs_manager/`: sandbox manager for cloud-hosted machines

## Architecture

```text
MCP client
  -> mcp_server
  -> tool_server
  -> local desktop
```

For the MCP itself, `planner`, `frontend`, and `ecs_manager` are not required.

## What Was Cleaned Up

- Added repository-level ignore rules for `.venv`, `node_modules`, `.next`,
  `__pycache__`, logs, and other generated files.
- Split local machine config from shareable config by adding example TOML files.
- Added a dedicated `start_mcp_only.bat` launcher for the MCP stack.
- Replaced the top-level README with a repository-oriented overview.

## Quick Start

### 1. Prepare configs

Copy the example configs to live configs:

```powershell
Copy-Item mcp_server\settings.example.toml mcp_server\settings.toml
Copy-Item tool_server\config.example.toml tool_server\config.toml
```

Optional components have their own examples:

```powershell
Copy-Item planner\config.example.toml planner\config.toml
Copy-Item ecs_manager\config.example.toml ecs_manager\config.toml
```

### 2. Start the MCP stack

Windows:

```powershell
.\start_mcp_only.bat
```

Manual start:

```powershell
cd tool_server
uv run uvicorn main:app --host 0.0.0.0 --port 8102
```

In another terminal:

```powershell
cd mcp_server
uv run mcp-server -t stdio
```

## MCP Client Configuration

Example Claude/Codex-style stdio configuration:

```json
{
  "mcpServers": {
    "computer_use": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "C:/d/test-cua/computer_use/mcp_server",
        "mcp-server",
        "-t",
        "stdio"
      ]
    }
  }
}
```

The MCP server expects the tool server to be reachable at
`http://127.0.0.1:8102` by default.

## Full Workspace

If you also want the planner and web UI:

```powershell
.\start_all.bat
```

That starts:

- Tool Server on `8102`
- MCP Server on `8000`
- Planner on `8089`
- Frontend on `3000`

## Repository Notes

- Local config files are intentionally ignored by Git.
- Lockfiles are kept so dependency versions remain reproducible.
- Generated directories already present on disk are left untouched so the local
  environment keeps working.

## License And Attribution

See [NOTICE.md](NOTICE.md). Source files in this workspace retain upstream
copyright and license notices.
