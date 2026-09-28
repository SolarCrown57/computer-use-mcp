# Copyright (c) 2025 Bytedance Ltd. and/or its affiliates
# Licensed under the 【火山方舟】原型应用软件自用许可协议
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at 
#     https://www.volcengine.com/docs/82379/1433703
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from contextlib import asynccontextmanager
from typing import AsyncIterator

import anyio
from mcp.server.fastmcp import FastMCP

from mcp_server.common.logs import LOG


@asynccontextmanager
async def _lifespan(_server: FastMCP) -> AsyncIterator[dict[str, object]]:
    try:
        yield {}
    finally:
        from mcp_server.tools.cua_sessions import get_cua_manager

        # The stdio parent watchdog owns the single shutdown deadline. Keeping
        # cleanup shielded here prevents an outer cancellation from turning a
        # timed-out close into an apparently clean server exit.
        with anyio.CancelScope(shield=True):
            try:
                await get_cua_manager().close_all()
            except BaseExceptionGroup as exc:
                LOG.error("CUA session cleanup completed with errors: %s", exc)


MCP = FastMCP(name="computer_use", lifespan=_lifespan)
