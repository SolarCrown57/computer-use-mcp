from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional


DEFAULT_SESSION_ID = "default"


def _load_cua_sdk():
    try:
        from cua import Image, Localhost, Sandbox
    except ImportError:
        from cua_sandbox import Image, Localhost, Sandbox
    return Image, Localhost, Sandbox


def make_image(
    os_type: str = "linux",
    *,
    distro: Optional[str] = None,
    version: Optional[str] = None,
    kind: Optional[str] = None,
    registry_ref: Optional[str] = None,
    image_path: Optional[str] = None,
    agent_type: Optional[str] = None,
) -> Any:
    Image, _, _ = _load_cua_sdk()

    if registry_ref:
        return Image.from_registry(registry_ref)
    if image_path:
        return Image.from_file(
            image_path,
            os_type=os_type,
            kind=kind or "vm",
            agent_type=agent_type,
        )

    os_name = (os_type or "linux").lower()
    if os_name == "linux":
        return Image.linux(distro=distro or "ubuntu", version=version or "24.04", kind=kind or "vm")
    if os_name in ("mac", "macos"):
        return Image.macos(version=version or "26", kind=kind or "vm")
    if os_name in ("win", "windows"):
        return Image.windows(version=version or "11", kind=kind or "vm")
    if os_name == "android":
        return Image.android(version=version or "14", kind=kind or "vm")
    raise ValueError(f"Unsupported CUA image os_type: {os_type}")


@dataclass
class CuaSession:
    session_id: str
    kind: str
    target: str
    instance: Any
    created_at: float
    last_used_at: float
    context: Any = None
    persistent: bool = False

    async def close(self, *, destroy: bool = False) -> None:
        if self.context is not None:
            await self.context.__aexit__(None, None, None)
            return

        if destroy and hasattr(self.instance, "destroy"):
            await self.instance.destroy()
            return

        disconnect = getattr(self.instance, "disconnect", None)
        if callable(disconnect):
            await disconnect()

    async def info(self) -> dict[str, Any]:
        self.last_used_at = time.time()
        data: dict[str, Any] = {
            "session_id": self.session_id,
            "kind": self.kind,
            "target": self.target,
            "persistent": self.persistent,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }
        name = getattr(self.instance, "name", None)
        if name:
            data["name"] = name
        try:
            data["environment"] = await self.instance.get_environment()
        except Exception:
            pass
        try:
            width, height = await self.instance.get_dimensions()
            data["width"] = width
            data["height"] = height
        except Exception:
            pass
        return data


class CuaSessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, CuaSession] = {}

    async def open_session(
        self,
        *,
        kind: str = "localhost",
        session_id: Optional[str] = None,
        replace: bool = False,
        name: Optional[str] = None,
        local: bool = True,
        os_type: str = "linux",
        distro: Optional[str] = None,
        version: Optional[str] = None,
        image_kind: Optional[str] = None,
        registry_ref: Optional[str] = None,
        image_path: Optional[str] = None,
        agent_type: Optional[str] = None,
        api_key: Optional[str] = None,
        ws_url: Optional[str] = None,
        http_url: Optional[str] = None,
        container_name: Optional[str] = None,
        cpu: Optional[int] = None,
        memory_mb: Optional[int] = None,
        disk_gb: Optional[int] = None,
        region: str = "us-east-1",
        request_timeout: Optional[float] = None,
        time_to_start: Optional[float] = None,
        telemetry_enabled: bool = True,
    ) -> CuaSession:
        Image, Localhost, Sandbox = _load_cua_sdk()
        del Image

        mode = (kind or "localhost").lower()
        requested_id = session_id or (DEFAULT_SESSION_ID if mode == "localhost" else None)
        if requested_id and requested_id in self._sessions:
            if not replace:
                return self._sessions[requested_id]
            await self.close_session(requested_id, destroy=True)

        context = None
        persistent = False
        target = "localhost"

        if mode == "localhost":
            instance = await Localhost.connect()
            target = "localhost"
            resolved_id = requested_id or DEFAULT_SESSION_ID
        elif mode == "connect":
            if not name and not any([ws_url, http_url]):
                raise ValueError("cua_open_session(kind='connect') requires name, ws_url, or http_url")
            instance = await Sandbox.connect(
                name or "",
                local=local,
                api_key=api_key,
                ws_url=ws_url,
                http_url=http_url,
                container_name=container_name,
                cpu=cpu,
                memory_mb=memory_mb,
                disk_gb=disk_gb,
                region=region,
                telemetry_enabled=telemetry_enabled,
            )
            target = name or ws_url or http_url or "sandbox"
            persistent = True
            resolved_id = requested_id or name or str(uuid.uuid4())
        elif mode in ("create", "ephemeral"):
            image = make_image(
                os_type,
                distro=distro,
                version=version,
                kind=image_kind,
                registry_ref=registry_ref,
                image_path=image_path,
                agent_type=agent_type,
            )
            if mode == "ephemeral":
                context = Sandbox.ephemeral(
                    image,
                    name=name,
                    local=local,
                    api_key=api_key,
                    cpu=cpu,
                    memory_mb=memory_mb,
                    disk_gb=disk_gb,
                    region=region,
                    request_timeout=request_timeout,
                    time_to_start=time_to_start,
                    telemetry_enabled=telemetry_enabled,
                )
                instance = await context.__aenter__()
            else:
                instance = await Sandbox.create(
                    image,
                    name=name,
                    local=local,
                    api_key=api_key,
                    cpu=cpu,
                    memory_mb=memory_mb,
                    disk_gb=disk_gb,
                    region=region,
                    request_timeout=request_timeout,
                    time_to_start=time_to_start,
                    telemetry_enabled=telemetry_enabled,
                )
                persistent = True
            target = name or getattr(instance, "name", None) or f"{os_type}:{version or 'default'}"
            resolved_id = requested_id or getattr(instance, "name", None) or str(uuid.uuid4())
        else:
            raise ValueError("kind must be one of: localhost, connect, create, ephemeral")

        now = time.time()
        session = CuaSession(
            session_id=resolved_id,
            kind=mode,
            target=target,
            instance=instance,
            context=context,
            persistent=persistent,
            created_at=now,
            last_used_at=now,
        )
        self._sessions[resolved_id] = session
        return session

    async def get_session(self, session_id: Optional[str] = None) -> CuaSession:
        resolved_id = session_id or DEFAULT_SESSION_ID
        if resolved_id not in self._sessions:
            if resolved_id == DEFAULT_SESSION_ID:
                return await self.open_session(kind="localhost", session_id=DEFAULT_SESSION_ID)
            raise KeyError(f"CUA session not found: {resolved_id}")
        session = self._sessions[resolved_id]
        session.last_used_at = time.time()
        return session

    async def close_session(self, session_id: str, *, destroy: bool = False) -> dict[str, Any]:
        session = self._sessions.pop(session_id)
        await session.close(destroy=destroy)
        return {"session_id": session_id, "closed": True, "destroyed": destroy}

    async def list_sessions(self) -> list[dict[str, Any]]:
        return [await session.info() for session in self._sessions.values()]

    async def close_all(self) -> None:
        for session_id in list(self._sessions):
            await self.close_session(session_id, destroy=False)


_SESSION_MANAGER = CuaSessionManager()


def get_cua_manager() -> CuaSessionManager:
    return _SESSION_MANAGER
