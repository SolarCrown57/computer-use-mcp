@echo off
echo ========================================
echo   Computer Use MCP - Local Mode
echo ========================================
echo.

set BASE_DIR=%~dp0

echo [1/2] Starting Tool Server (port 8102)...
start "Tool Server" cmd /k "cd /d %BASE_DIR%tool_server && uv run uvicorn main:app --host 0.0.0.0 --port 8102"
ping -n 4 127.0.0.1 >nul

echo [2/2] Starting MCP Server (port 8000)...
start "MCP Server" cmd /k "cd /d %BASE_DIR%mcp_server && uv run mcp-server -t stdio"

echo.
echo ========================================
echo   MCP stack started.
echo   Tool Server: http://127.0.0.1:8102
echo   MCP Server (stdio): ready for clients
echo ========================================
echo.
pause

