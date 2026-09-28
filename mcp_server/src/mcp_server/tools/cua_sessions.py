from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from mcp_server.common.config import cua_config


DEFAULT_SESSION_ID = "default"
LOG = logging.getLogger(__name__)


class CuaSessionNotFoundError(KeyError):
    error_code = "session_not_found"

    def __init__(self, session_id: str) -> None:
        super().__init__(f"{self.error_code}: CUA session not found: {session_id}")

    def __str__(self) -> str:
        return str(self.args[0])


class CuaSessionExplicitDestroyRequiredError(RuntimeError):
    error_code = "explicit_destroy_required"

    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"{self.error_code}: CUA session {session_id!r} is context-backed; "
            "close it explicitly with destroy=true before replacing it"
        )


def _configured_default_session_id() -> str:
    value = cua_config.get("default_session", DEFAULT_SESSION_ID)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("[cua].default_session must be a non-empty string")
    return value.strip()


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
            if not destroy:
                # Sandbox.ephemeral() destroys its resource from __aexit__.
                # Entering it from a disconnect or replace path would turn a
                # nominally non-destructive operation into an implicit delete.
                raise CuaSessionExplicitDestroyRequiredError(self.session_id)
            await self.context.__aexit__(None, None, None)
            return

        if destroy:
            destroy_instance = getattr(self.instance, "destroy", None)
            if not callable(destroy_instance):
                raise RuntimeError(
                    f"CUA session {self.session_id!r} does not support explicit destroy"
                )
            await destroy_instance()
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
    def __init__(self, *, default_session_id: Optional[str] = None) -> None:
        self._sessions: dict[str, CuaSession] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self.default_session_id = default_session_id or _configured_default_session_id()

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    @staticmethod
    def _resolve_session_id(
        *,
        mode: str,
        session_id: Optional[str],
        name: Optional[str],
        default_session_id: str,
    ) -> str:
        if session_id:
            return session_id
        if mode == "localhost":
            return default_session_id
        if name:
            return name
        return str(uuid.uuid4())

    async def _create_session(
        self,
        *,
        mode: str,
        resolved_id: str,
        name: Optional[str],
        local: bool,
        os_type: str,
        distro: Optional[str],
        version: Optional[str],
        image_kind: Optional[str],
        registry_ref: Optional[str],
        image_path: Optional[str],
        agent_type: Optional[str],
        api_key: Optional[str],
        ws_url: Optional[str],
        http_url: Optional[str],
        container_name: Optional[str],
        cpu: Optional[int],
        memory_mb: Optional[int],
        disk_gb: Optional[int],
        region: str,
        request_timeout: Optional[float],
        time_to_start: Optional[float],
        telemetry_enabled: bool,
    ) -> CuaSession:
        Image, Localhost, Sandbox = _load_cua_sdk()
        del Image

        context = None
        persistent = False
        target = "localhost"

        if mode == "localhost":
            instance = await Localhost.connect()
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
        else:
            raise ValueError("kind must be one of: localhost, connect, create, ephemeral")

        now = time.time()
        return CuaSession(
            session_id=resolved_id,
            kind=mode,
            target=target,
            instance=instance,
            context=context,
            persistent=persistent,
            created_at=now,
            last_used_at=now,
        )

    async def _discard_candidate(self, session: CuaSession) -> None:
        try:
            # A candidate never became externally visible. Roll back a newly
            # created ephemeral context explicitly so failed verification or
            # registration cannot leak a sandbox.
            await asyncio.shield(session.close(destroy=session.context is not None))
        except Exception:
            LOG.exception("Failed to clean up unregistered CUA session %s", session.session_id)

    @staticmethod
    async def _verify_candidate(session: CuaSession) -> None:
        environment = await session.instance.get_environment()
        if environment is None:
            raise RuntimeError(f"CUA session {session.session_id!r} returned no environment")
        dimensions = await session.instance.get_dimensions()
        if (
            not isinstance(dimensions, (tuple, list))
            or len(dimensions) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in dimensions)
        ):
            raise RuntimeError(
                f"CUA session {session.session_id!r} returned invalid dimensions: {dimensions!r}"
            )

    async def _replace_locked(self, candidate: CuaSession, *, replace: bool) -> CuaSession:
        existing = self._sessions.get(candidate.session_id)
        if existing is not None and not replace:
            await self._discard_candidate(candidate)
            return existing

        if existing is not None:
            try:
                await existing.close(destroy=False)
            except BaseException:
                await self._discard_candidate(candidate)
                raise

        self._sessions[candidate.session_id] = candidate
        return candidate

    async def register_session(self, session: CuaSession, *, replace: bool = False) -> CuaSession:
        """Register an already connected session using the same transactional replacement rules."""
        async with self._lock_for(session.session_id):
            return await self._replace_locked(session, replace=replace)

    async def register_instance(
        self,
        *,
        session_id: str,
        kind: str,
        target: str,
        instance: Any,
        persistent: bool,
        context: Any = None,
        replace: bool = False,
    ) -> CuaSession:
        now = time.time()
        return await self.register_session(
            CuaSession(
                session_id=session_id,
                kind=kind,
                target=target,
                instance=instance,
                context=context,
                persistent=persistent,
                created_at=now,
                last_used_at=now,
            ),
            replace=replace,
        )

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
        mode = (kind or "localhost").lower()
        if mode not in ("localhost", "connect", "create", "ephemeral"):
            raise ValueError("kind must be one of: localhost, connect, create, ephemeral")
        resolved_id = self._resolve_session_id(
            mode=mode,
            session_id=session_id,
            name=name,
            default_session_id=self.default_session_id,
        )

        async with self._lock_for(resolved_id):
            existing = self._sessions.get(resolved_id)
            if existing is not None and not replace:
                return existing
            if existing is not None and existing.context is not None:
                # Reject before connecting a replacement. A context-backed
                # session can only be released through its destructive
                # __aexit__, which replace=true is not authorized to invoke.
                raise CuaSessionExplicitDestroyRequiredError(resolved_id)

            candidate = await self._create_session(
                mode=mode,
                resolved_id=resolved_id,
                name=name,
                local=local,
                os_type=os_type,
                distro=distro,
                version=version,
                image_kind=image_kind,
                registry_ref=registry_ref,
                image_path=image_path,
                agent_type=agent_type,
                api_key=api_key,
                ws_url=ws_url,
                http_url=http_url,
                container_name=container_name,
                cpu=cpu,
                memory_mb=memory_mb,
                disk_gb=disk_gb,
                region=region,
                request_timeout=request_timeout,
                time_to_start=time_to_start,
                telemetry_enabled=telemetry_enabled,
            )
            try:
                await self._verify_candidate(candidate)
            except BaseException:
                await self._discard_candidate(candidate)
                raise
            return await self._replace_locked(candidate, replace=replace)

    async def resume_sandbox(
        self,
        *,
        name: str,
        session_id: Optional[str] = None,
        local: bool = True,
        api_key: Optional[str] = None,
    ) -> CuaSession:
        resolved_id = session_id or name
        if not resolved_id:
            raise ValueError("cua_resume_sandbox requires a non-empty name or session_id")

        observed_session = self._sessions.get(resolved_id)
        async with self._lock_for(resolved_id):
            current_session = self._sessions.get(resolved_id)
            if current_session is not observed_session and current_session is not None:
                return current_session
            if current_session is not None and current_session.context is not None:
                raise CuaSessionExplicitDestroyRequiredError(resolved_id)
            _, _, Sandbox = _load_cua_sdk()
            instance = await Sandbox.resume(name, local=local, api_key=api_key)
            now = time.time()
            candidate = CuaSession(
                session_id=resolved_id,
                kind="connect",
                target=name,
                instance=instance,
                persistent=True,
                created_at=now,
                last_used_at=now,
            )
            try:
                await self._verify_candidate(candidate)
            except BaseException:
                await self._discard_candidate(candidate)
                raise
            return await self._replace_locked(candidate, replace=True)

    async def get_session(self, session_id: Optional[str] = None) -> CuaSession:
        resolved_id = session_id or self.default_session_id
        async with self._lock_for(resolved_id):
            session = self._sessions.get(resolved_id)
            if session is not None:
                session.last_used_at = time.time()
                return session

        if resolved_id == DEFAULT_SESSION_ID and self.default_session_id == DEFAULT_SESSION_ID:
            return await self.open_session(kind="localhost", session_id=DEFAULT_SESSION_ID)
        raise CuaSessionNotFoundError(resolved_id)

    async def close_session(self, session_id: str, *, destroy: bool = False) -> dict[str, Any]:
        async with self._lock_for(session_id):
            session = self._sessions.get(session_id)
            if session is None:
                raise CuaSessionNotFoundError(session_id)
            await session.close(destroy=destroy)
            if self._sessions.get(session_id) is session:
                del self._sessions[session_id]
            return {"session_id": session_id, "closed": True, "destroyed": destroy}

    async def list_sessions(self) -> list[dict[str, Any]]:
        sessions = list(self._sessions.values())
        return [await session.info() for session in sessions]

    def active_session_ids(self) -> list[str]:
        return list(self._sessions)

    async def close_all(self) -> None:
        session_ids = list(self._sessions)
        results = await asyncio.gather(
            *(self.close_session(session_id, destroy=False) for session_id in session_ids),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise BaseExceptionGroup("Failed to close one or more CUA sessions", errors)


_SESSION_MANAGER = CuaSessionManager()


def get_cua_manager() -> CuaSessionManager:
    return _SESSION_MANAGER
