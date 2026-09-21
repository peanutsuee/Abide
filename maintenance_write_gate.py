"""Process-local write quiescence for maintenance capture operations."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
import secrets
import threading
import time
from typing import AsyncIterator, Iterator


class MaintenanceWriteError(RuntimeError):
    """Stable, redacted maintenance write boundary failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class MaintenanceStatus:
    state: str
    active_writers: int
    generation: int
    freeze_started_at: str | None
    freeze_deadline: str | None
    freeze_reason: str | None


class FreezeLease:
    """Opaque lease returned only by a coordinator freeze operation."""

    __slots__ = (
        "lease_id",
        "generation",
        "started_at",
        "deadline",
        "started_time",
        "deadline_time",
        "reason",
        "_coordinator_identity",
        "_capability",
        "_released",
    )

    def __init__(
        self,
        *,
        lease_id: str,
        generation: int,
        started_at: float,
        deadline: float,
        reason: str,
        coordinator_identity: object,
        capability: object,
    ) -> None:
        self.lease_id = lease_id
        self.generation = generation
        self.started_at = started_at
        self.deadline = deadline
        now = datetime.now(timezone.utc)
        self.started_time = now.isoformat(timespec="seconds")
        self.deadline_time = (
            now + timedelta(seconds=deadline - started_at)
        ).isoformat(timespec="seconds")
        self.reason = reason
        self._coordinator_identity = coordinator_identity
        self._capability = capability
        self._released = False

    def __repr__(self) -> str:
        return "FreezeLease(active={!r}, generation={!r})".format(
            not self._released,
            self.generation,
        )


class MaintenanceWriteCoordinator:
    """Coordinate writers and one process-local maintenance freeze."""

    def __init__(self, *, monotonic=time.monotonic) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._state = "open"
        self._active_writers = 0
        self._generation = 0
        self._identity = object()
        self._capability = object()
        self._lease: FreezeLease | None = None
        self._monotonic = monotonic
        self._writer_depth: ContextVar[tuple[object, int] | None] = ContextVar(
            "maintenance_writer_depth_{}".format(id(self)),
            default=None,
        )

    @contextmanager
    def writer_scope(self, operation: str = "persistent_mutation") -> Iterator[None]:
        del operation
        execution = _execution_identity()
        current = self._writer_depth.get()
        depth = current[1] if current is not None and current[0] == execution else 0
        token = self._writer_depth.set((execution, depth + 1))
        entered = False
        if depth == 0:
            with self._condition:
                if self._state != "open":
                    self._writer_depth.reset(token)
                    raise MaintenanceWriteError("maintenance_in_progress")
                self._active_writers += 1
                entered = True
        try:
            yield
        finally:
            self._writer_depth.reset(token)
            if entered:
                with self._condition:
                    self._active_writers -= 1
                    self._generation += 1
                    self._condition.notify_all()

    @contextmanager
    def optional_writer_scope(
        self,
        operation: str = "incidental_persistence",
    ) -> Iterator[bool]:
        """Enter incidental persistence, or yield False during maintenance."""
        del operation
        execution = _execution_identity()
        current = self._writer_depth.get()
        depth = current[1] if current is not None and current[0] == execution else 0
        token = self._writer_depth.set((execution, depth + 1))
        entered = False
        rejected = False
        if depth == 0:
            with self._condition:
                if self._state != "open":
                    rejected = True
                else:
                    self._active_writers += 1
                    entered = True
        if rejected:
            self._writer_depth.reset(token)
            yield False
            return
        try:
            yield True
        finally:
            self._writer_depth.reset(token)
            if entered:
                with self._condition:
                    self._active_writers -= 1
                    self._generation += 1
                    self._condition.notify_all()

    @asynccontextmanager
    async def async_writer_scope(
        self,
        operation: str = "persistent_mutation",
    ) -> AsyncIterator[None]:
        with self.writer_scope(operation):
            yield

    @asynccontextmanager
    async def optional_async_writer_scope(
        self,
        operation: str = "incidental_persistence",
    ) -> AsyncIterator[bool]:
        with self.optional_writer_scope(operation) as entered:
            yield entered

    @asynccontextmanager
    async def freeze(
        self,
        *,
        reason: str,
        drain_timeout_seconds: float,
        max_freeze_seconds: float,
    ) -> AsyncIterator[FreezeLease]:
        if (
            not isinstance(reason, str)
            or not reason
            or len(reason) > 64
            or not reason.replace("_", "").isalnum()
            or drain_timeout_seconds <= 0
            or max_freeze_seconds <= 0
        ):
            raise MaintenanceWriteError("freeze_invalid")
        with self._condition:
            if self._state != "open" or self._lease is not None:
                raise MaintenanceWriteError("freeze_unavailable")
            self._state = "draining"
        try:
            drained = await asyncio.to_thread(
                self._wait_for_writers,
                drain_timeout_seconds,
            )
            if not drained:
                raise MaintenanceWriteError("freeze_drain_timeout")
            now = self._monotonic()
            with self._condition:
                if self._state != "draining" or self._active_writers != 0:
                    raise MaintenanceWriteError("freeze_unavailable")
                lease = FreezeLease(
                    lease_id=secrets.token_hex(16),
                    generation=self._generation,
                    started_at=now,
                    deadline=now + max_freeze_seconds,
                    reason=reason,
                    coordinator_identity=self._identity,
                    capability=self._capability,
                )
                self._lease = lease
                self._state = "frozen"
            try:
                yield lease
            finally:
                self._release_lease(lease)
        except BaseException:
            with self._condition:
                if self._state == "draining":
                    self._state = "open"
                    self._condition.notify_all()
            raise

    def validate_lease(self, lease: FreezeLease) -> None:
        with self._condition:
            if (
                not isinstance(lease, FreezeLease)
                or lease._coordinator_identity is not self._identity
                or lease._capability is not self._capability
                or lease._released
                or self._lease is not lease
                or self._state != "frozen"
                or self._generation != lease.generation
            ):
                raise MaintenanceWriteError("freeze_lease_invalid")
            if self._monotonic() >= lease.deadline:
                raise MaintenanceWriteError("freeze_lease_expired")

    def status(self) -> MaintenanceStatus:
        with self._condition:
            lease = self._lease
            return MaintenanceStatus(
                state=self._state,
                active_writers=self._active_writers,
                generation=self._generation,
                freeze_started_at=(
                    lease.started_time if lease is not None else None
                ),
                freeze_deadline=(
                    lease.deadline_time if lease is not None else None
                ),
                freeze_reason=lease.reason if lease is not None else None,
            )

    def monotonic(self) -> float:
        """Return the coordinator clock used by lease deadlines."""
        return self._monotonic()

    def _wait_for_writers(self, timeout_seconds: float) -> bool:
        deadline = self._monotonic() + timeout_seconds
        with self._condition:
            while self._active_writers:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def _release_lease(self, lease: FreezeLease) -> None:
        with self._condition:
            if self._lease is lease:
                lease._released = True
                self._lease = None
                self._state = "open"
                self._condition.notify_all()


def _execution_identity() -> object:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return ("task", id(task)) if task is not None else ("thread", threading.get_ident())


DEFAULT_WRITE_COORDINATOR = MaintenanceWriteCoordinator()


def writer_scope(operation: str = "persistent_mutation"):
    return DEFAULT_WRITE_COORDINATOR.writer_scope(operation)


def async_writer_scope(operation: str = "persistent_mutation"):
    return DEFAULT_WRITE_COORDINATOR.async_writer_scope(operation)


def optional_writer_scope(operation: str = "incidental_persistence"):
    return DEFAULT_WRITE_COORDINATOR.optional_writer_scope(operation)


def optional_async_writer_scope(operation: str = "incidental_persistence"):
    return DEFAULT_WRITE_COORDINATOR.optional_async_writer_scope(operation)


def guarded_mutation(operation: str):
    """Guard one synchronous production persistence boundary."""

    def decorate(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            coordinator = getattr(
                args[0],
                "write_coordinator",
                DEFAULT_WRITE_COORDINATOR,
            ) if args else DEFAULT_WRITE_COORDINATOR
            with coordinator.writer_scope(operation):
                return function(*args, **kwargs)

        guarded.__maintenance_guarded__ = operation
        return guarded

    return decorate


def guarded_async_mutation(operation: str):
    """Guard one asynchronous production persistence boundary."""

    def decorate(function):
        @wraps(function)
        async def guarded(*args, **kwargs):
            coordinator = getattr(
                args[0],
                "write_coordinator",
                DEFAULT_WRITE_COORDINATOR,
            ) if args else DEFAULT_WRITE_COORDINATOR
            async with coordinator.async_writer_scope(operation):
                return await function(*args, **kwargs)

        guarded.__maintenance_guarded__ = operation
        return guarded

    return decorate


def guarded_optional_mutation(operation: str):
    """Run incidental synchronous persistence only while writes are open."""

    def decorate(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            coordinator = getattr(
                args[0],
                "write_coordinator",
                DEFAULT_WRITE_COORDINATOR,
            ) if args else DEFAULT_WRITE_COORDINATOR
            with coordinator.optional_writer_scope(operation) as entered:
                if not entered:
                    return None
                return function(*args, **kwargs)

        guarded.__maintenance_guarded__ = operation
        guarded.__maintenance_optional__ = True
        return guarded

    return decorate


def guarded_optional_async_mutation(operation: str):
    """Run incidental asynchronous persistence only while writes are open."""

    def decorate(function):
        @wraps(function)
        async def guarded(*args, **kwargs):
            coordinator = getattr(
                args[0],
                "write_coordinator",
                DEFAULT_WRITE_COORDINATOR,
            ) if args else DEFAULT_WRITE_COORDINATOR
            async with coordinator.optional_async_writer_scope(operation) as entered:
                if not entered:
                    return None
                return await function(*args, **kwargs)

        guarded.__maintenance_guarded__ = operation
        guarded.__maintenance_optional__ = True
        return guarded

    return decorate


def guarded_http_mutation(operation: str, *, methods: tuple[str, ...] | None = None):
    """Guard an ASGI mutation and map maintenance rejection to stable 503."""

    def decorate(function):
        @wraps(function)
        async def guarded(*args, **kwargs):
            request = args[0] if args else None
            if methods is not None and str(getattr(request, "method", "")).upper() not in methods:
                return await function(*args, **kwargs)
            try:
                async with DEFAULT_WRITE_COORDINATOR.async_writer_scope(operation):
                    return await function(*args, **kwargs)
            except MaintenanceWriteError:
                from starlette.responses import JSONResponse

                return JSONResponse(
                    {"error": "maintenance_in_progress"},
                    status_code=503,
                    headers={"Cache-Control": "no-store"},
                )

        guarded.__maintenance_guarded__ = operation
        return guarded

    return decorate
