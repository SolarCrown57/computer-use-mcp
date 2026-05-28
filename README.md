# Computer Use MCP

这是一个基于新版 [`trycua/cua`](https://github.com/trycua/cua) SDK 重构的
Computer Use MCP 服务。当前主路径直接使用 `cua-sandbox`，并可选接入
`cua-driver` 来实现后台窗口级自动化。

## 架构

```text
MCP Client
  -> mcp_server
  -> cua-sandbox Localhost / Sandbox
  -> 可选 cua-driver 后台应用/窗口控制
```

`tool_server/` 仍然保留，用于 HTTP 兼容和旧 demo/planner 栈；但正常 MCP
使用已经不再需要通过 `tool_server` 转发桌面操作。

## 能力

保留旧 MCP 工具名：

- `move_mouse`
- `click_mouse`
- `drag_mouse`
- `scroll`
- `press_key`
- `type_text`
- `get_cursor_position`
- `screenshot`

新增 `cua_*` SDK 工具：

- Session 生命周期：`cua_open_session`、`cua_list_sessions`、
  `cua_close_session`、`cua_session_info`
- 本机/沙箱控制：截图、鼠标、键盘、剪贴板、shell、PTY terminal
- 沙箱管理：list、resume、suspend、delete、snapshot、display URL
- 文件操作：list/read/write text
- Android 操作：tap、swipe、hardware key

新增 `cua_driver_*` 后台自动化工具：

- 诊断：`cua_driver_status`、`cua_driver_doctor`、
  `cua_driver_check_permissions`
- 发现：list/describe/call driver tools、list apps、list windows
- 后台操作：launch app、window state、screenshot、click、type、hotkey、
  set value、scroll、zoom、agent cursor
- 通用入口：`cua_driver_call`，用于调用上游新加但本仓库还没单独封装的
  driver 工具

`cua_run_task` 也保留了，但它依赖 `cua-agent`，默认不安装，避免基础 MCP
服务被大依赖拖慢。

## 快速开始

安装依赖：

```powershell
cd mcp_server
uv sync
```

启动 MCP：

```powershell
uv run mcp-server -t stdio
```

Windows 也可以直接运行：

```powershell
.\start_mcp_only.bat
```

## MCP 客户端配置

示例 stdio 配置：

```json
{
  "mcpServers": {
    "computer_use": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "C:/work/20260526/computer-use-mcp/mcp_server",
        "mcp-server",
        "-t",
        "stdio"
      ]
    }
  }
}
```

## 后台操作

后台窗口/应用操作依赖 `cua-driver`，这是 CUA 的独立二进制。MCP 服务会检测
`PATH` 中是否存在 `cua-driver`；如果没有安装，`cua_driver_status` 会返回安装
提示，而不会导致整个 MCP 服务启动失败。

Windows 安装命令：

```powershell
irm https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.ps1 | iex
```

安装后重启 MCP，并先运行：

```text
cua_driver_status
cua_driver_doctor
cua_driver_list_tools
```

driver 工具默认使用 background dispatch。部分 Windows 目标窗口可能返回
`background_unavailable`，这时返回值会说明应该改用 accessibility element 路径，
还是显式调用 `cua_driver_bring_to_front` 后再执行前台 dispatch。

## CUA Agent 任务工具

如需启用 `cua_run_task`：

```powershell
cd mcp_server
uv sync --extra agent
```

然后按所选 `cua-agent` 模型配置对应 provider 的 API key。

## 旧 Demo 栈

如果还需要旧的 HTTP tool server、planner 和 web UI：

```powershell
.\start_all.bat
```

它会启动：

- `tool_server`
- `mcp_server`
- `planner`
- `frontend`

## 配置说明

`mcp_server/settings.toml` 不是必须的。需要本地覆盖时复制示例：

```powershell
Copy-Item mcp_server\settings.example.toml mcp_server\settings.toml
```

`tool_server/config.toml` 仍支持：

```toml
computer_backend = "cua"
```

该配置会让旧 HTTP tool server 也走 `cua-sandbox` 的 `Localhost` 后端。

## 备注

- lockfile 已保留，方便复现依赖。
- `tool_server_client` 仍在 MCP 依赖中保留，用于兼容旧集成代码。
- `cua-driver` 是可选能力；没安装时只有 `cua_driver_*` 后台工具不可用。

## License And Attribution

见 [NOTICE.md](NOTICE.md)。仓库内源文件保留其上游 copyright 和 license
声明。
