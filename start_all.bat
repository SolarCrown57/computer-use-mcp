@echo off
echo ========================================
echo   Computer Use Agent - Local Mode
echo ========================================
echo.

set BASE_DIR=%~dp0

echo [1/4] Starting Tool Server (port 8102)...
start "Tool Server" cmd /k "cd /d %BASE_DIR%tool_server && uv run uvicorn main:app --host 0.0.0.0 --port 8102"
ping -n 4 127.0.0.1 >nul

echo [2/4] Starting MCP Server (port 8000)...
start "MCP Server" cmd /k "cd /d %BASE_DIR%mcp_server && uv run mcp-server"
ping -n 4 127.0.0.1 >nul

echo [3/4] Starting Planner (port 8089)...
start "Planner" cmd /k "cd /d %BASE_DIR%planner && uv run src/planner/main.py"
ping -n 4 127.0.0.1 >nul

echo [4/4] Starting Frontend (port 3000)...
start "Frontend" cmd /k "cd /d %BASE_DIR%frontend && npm install && npm run dev"

echo.
echo ========================================
echo   All services started! (Local Mode)
echo   Frontend: http://localhost:3000
echo.
echo   WARNING: AI will control your mouse
echo   and keyboard directly!
echo ========================================
echo.
pause
