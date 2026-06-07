# CUA Computer Use MCP Server

This package exposes computer-use tools to MCP clients. It talks directly to
the current `trycua/cua` SDK through `cua-sandbox`, and optionally wraps
`cua-driver` for background app/window automation.

## Tool Groups

Legacy-compatible tools:

- `move_mouse`
- `click_mouse`
- `drag_mouse`
- `scroll`
- `press_key`
- `type_text`
- `get_cursor_position`
- `screenshot`

CUA SDK tools:

- `cua_open_session`, `cua_close_session`, `cua_list_sessions`,
  `cua_session_info`
- `cua_list_sandboxes`, `cua_resume_sandbox`, `cua_suspend_sandbox`,
  `cua_delete_sandbox`
- `cua_screenshot`, `cua_get_screen_size`, `cua_get_environment`
- `cua_move`, `cua_click`, `cua_double_click`, `cua_drag`, `cua_scroll`
- `cua_type_text`, `cua_keypress`, `cua_key_down`, `cua_key_up`
- `cua_clipboard_get`, `cua_clipboard_set`, `cua_open_url`, `cua_shell`
- `cua_terminal_create`, `cua_terminal_send`, `cua_terminal_info`,
  `cua_terminal_close`
- `cua_display_url`, `cua_snapshot`
- `cua_file_list`, `cua_file_read_text`, `cua_file_write_text`
- `cua_mobile_tap`, `cua_mobile_swipe`, `cua_mobile_key`

CUA driver tools:

- `cua_driver_status`, `cua_driver_doctor`,
  `cua_driver_check_permissions`
- `cua_driver_list_tools`, `cua_driver_describe_tool`,
  `cua_driver_call`
- `cua_driver_list_apps`, `cua_driver_launch_app`,
  `cua_driver_kill_app`, `cua_driver_list_windows`
- `cua_driver_get_window_state`, `cua_driver_screenshot`,
  `cua_driver_click`, `cua_driver_double_click`, `cua_driver_type_text`,
  `cua_driver_press_key`, `cua_driver_hotkey`, `cua_driver_set_value`,
  `cua_driver_scroll`, `cua_driver_zoom`, `cua_driver_bring_to_front`
- `cua_driver_set_agent_cursor_enabled`,
  `cua_driver_get_agent_cursor_state`

`cua_driver_call` is intentionally generic so newly added upstream driver
tools can be used before this MCP adds a dedicated wrapper.

## Install

```powershell
uv sync
```

Optional agent task support:

```powershell
uv sync --extra agent
```

## Run

```powershell
uv run mcp-server -t stdio
```

SSE mode is still available:

```powershell
uv run mcp-server -t sse
```

## Configuration

Copy the example when local overrides are needed:

```powershell
Copy-Item settings.example.toml settings.toml
```

Relevant settings:

```toml
[cua]
default_session = "default"

[cua_driver]
command = "cua-driver"
```

`CUA_DRIVER_COMMAND` overrides `[cua_driver].command`.

## Desktop URL Launch

`cua_open_url` opens a URL through the visible desktop UI without invoking a
shell command. It uses the session clipboard plus the platform launcher
(`Win+R` on Windows, Spotlight on macOS, app launcher on Linux), then restores
the previous clipboard text by default.

## Coordinate Space

Legacy tools and CUA SDK mouse tools accept coordinates in the pixel space of
the latest screenshot returned by `screenshot` or `cua_screenshot`. On Windows
localhost sessions, the MCP server starts as per-monitor DPI aware and maps
those screenshot pixels to the native virtual desktop input space before
calling `cua_auto` mouse APIs. This keeps screenshot, cursor, click, drag, and
scroll coordinates aligned on scaled displays.

Screenshot responses include coordinate diagnostics such as `coordinate_space`,
`input_width`, `input_height`, `scale_x`, and `scale_y`. `scale_x`/`scale_y`
should normally be `1.0` after the Windows `cua_auto` screenshot fix; non-1.0
values mean the MCP wrapper is compensating for a backend that still returns a
resized screenshot.

## Background Automation

Install `cua-driver` separately if you want background app/window control.

Windows:

```powershell
irm https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.ps1 | iex
```

The driver wrappers default to background dispatch. When an app cannot accept a
background message, the driver returns a structured diagnostic such as
`background_unavailable`; the agent can then use an accessibility element path
or explicitly call `cua_driver_bring_to_front`.

## Notes

- The default legacy tools lazily open a CUA `Localhost` session.
- Stdio mode watches its parent process and closes open CUA sessions on server
  shutdown, so interrupted MCP clients do not leave long-lived server workers.
- `cua_open_session(kind="ephemeral")` keeps the SDK context open until
  `cua_close_session`.
- `tool_server_client` remains in dependencies for older integration code, but
  the current MCP tool path does not depend on the HTTP tool server.
