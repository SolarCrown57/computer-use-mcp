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
uv sync --locked
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

后台窗口/应用操作只支持仓库锁定的 `cua-driver` fork。版本、协议、发布资产和
校验值记录在 [`cua-driver.lock.json`](cua-driver.lock.json)；运行时必须同时匹配
`0.7.1-sc.1` 和 `sc.background.v1`，否则诊断工具仍可用，但启动和输入工具会
以 `driver_incompatible` 失败关闭。

Windows x86_64 本地安装命令：

```powershell
.\scripts\install-cua-driver.ps1
```

安装器只从锁定的 `SolarCrown57/cua` release 下载，把驱动安装到
`.tools/cua-driver/<version>`，并在展开前验证 SHA256。当前锁文件的
`published=false` 表示目标 fork release 尚未发布，安装器会明确拒绝下载；发布
流程必须在 fork 的 Windows E2E 通过后填入真实 SHA256 并改为 `true`，不能使用
占位校验值或自动退回 upstream/PATH 中的不兼容版本。

锁文件只能在 fork E2E 和本仓库的 MCP 交互式 E2E 都通过后更新。回滚时恢复上一
个已验收 release 的完整锁记录，不得只替换下载 URL 或跳过校验。

发布并安装后，把安装器返回的 `command` 路径写入 `CUA_DRIVER_COMMAND` 或
`mcp_server/settings.toml`，重启 MCP，然后运行：

```text
cua_driver_status
cua_driver_doctor
cua_driver_list_tools
```

### 严格后台契约

- 所有 driver 结果保留 `ok`、`available`、`stdout`、`stderr`，并返回
  `verified`、`error_code`、`message`、`details`、`target`、`foreground`。
- `verified=true` 只表示目标身份、投递路径和前台保持已经确认；有可靠读回能力时
  还必须验证实际效果。投递成功但无法确认效果不能当作成功。
- `cua_driver_launch_app` 必须且只能给出一种应用标识，名称采用大小写无关的精确
  匹配；`instance_policy` 默认为 `reuse`，`preserve_foreground` 默认为 `true`。
- 后台失败不会自动切换到前台。只有调用方显式选择 `dispatch="foreground"` 或
  调用 `cua_driver_bring_to_front` 才允许改变焦点。

| Windows 目标 | 后台点击/赋值 | 按键/文本 | 无激活恢复/截图 |
| --- | --- | --- | --- |
| Win32 / 原生控件 | 可取消的 PostMessage/注入路径经验证时支持；`set_value` 暂不支持 | PostMessage 经验证时支持；显式前台使用可取消 SendInput | 原生能力确认时支持 |
| XAML / WinUI / UWP | 不可取消的 UIA 变更路径拒绝为 `background_unavailable` 或 `tool_unsupported` | 后台拒绝；调用方可显式选择前台 | 仅原生能力确认时支持 |
| WebView2 / Electron | UIA/CDP 后台变更路径拒绝；不会回退前台 | 后台拒绝不可靠路径；调用方可显式选择前台 | 截图经身份校验后支持 |
| 无稳定 PID/HWND 的目标 | 拒绝 | 拒绝 | 拒绝 |

Windows `page.execute_javascript`、`page.click_element` 和 `set_value` 在可终止的
helper-process 边界完成前保持失败关闭；只读 `page.get_text` / `page.query_dom` 仍可用。

常见严格错误码包括 `tool_unsupported`、`target_ambiguous`、`target_mismatch`、
`background_unavailable`、`background_no_effect`、`foreground_changed`、
`operation_timeout` 和 `operation_cancelled`。不要把这些错误无条件重试；长文本失败时
应根据返回的已确认写入量决定后续动作。

## CUA Agent 任务工具

如需启用 `cua_run_task`：

```powershell
cd mcp_server
uv sync --locked --extra agent
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
- `cua_open_session(replace=true)` 不会销毁旧 sandbox；资源销毁只由显式
  `cua_close_session(destroy=true)` 或 `cua_delete_sandbox` 执行。

## License And Attribution

见 [NOTICE.md](NOTICE.md)。仓库内源文件保留其上游 copyright 和 license
声明。
