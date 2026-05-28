@echo off
echo ========================================
echo   CUA Computer Use MCP
echo ========================================
echo.

set BASE_DIR=%~dp0

echo Starting MCP Server (stdio)...
start "MCP Server" cmd /k "cd /d %BASE_DIR%mcp_server && uv run mcp-server -t stdio"

echo.
echo ========================================
echo   MCP stack started.
echo   MCP Server (stdio): ready for clients.
echo   CUA SDK tools run directly in mcp_server.
echo   cua-driver tools require cua-driver on PATH.
echo ========================================
echo.
pause

