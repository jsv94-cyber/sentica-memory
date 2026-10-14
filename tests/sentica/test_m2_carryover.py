"""M1 carry-over checks that must keep failing if the behavior is removed."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

# Current Node 24 majors confirmed on 2026-10-13:
# actions/checkout v7.0.1, actions/setup-python v7.0.0, astral-sh/setup-uv v10.3.0.
PINNED_MAJORS = {
    "actions/checkout": 7,
    "actions/setup-python": 7,
    "astral-sh/setup-uv": 10,
}
_USES = re.compile(
    r"uses:\s*(actions/checkout|actions/setup-python|astral-sh/setup-uv)@v(\d+)"
)
# The unified runner asks a previous job for its label. Everything else is pinned.
REVIEWED_DYNAMIC_RUNNERS = {
    "unified-tests.yml": "${{ fromJSON(format('[{0}]', needs.start-runner.outputs.runner-labels)) }}",
}


def test_workflows_pin_ubuntu_2404_and_node24_actions() -> None:
    files = sorted(WORKFLOWS.glob("*.yml"))
    assert files
    seen = {name: False for name in PINNED_MAJORS}
    for path in files:
        text = path.read_text()
        assert "ubuntu-latest" not in text, path.name
        for label in re.findall(r"(?m)^[ ]*runs-on:\s*(.+)$", text):
            cleaned = label.strip()
            if cleaned == "ubuntu-24.04":
                continue
            allowed = REVIEWED_DYNAMIC_RUNNERS.get(path.name)
            assert cleaned == allowed, path.name
        for match in _USES.finditer(text):
            action = match.group(1)
            major = int(match.group(2))
            assert major >= PINNED_MAJORS[action], path.name
            seen[action] = True
    assert all(seen.values())


_BOUNDED_METHODS = (
    "rollback",
    "close",
    "delete",
    "refresh",
    "get",
    "merge",
    "connection",
    "run_sync",
)


@pytest.mark.parametrize("method_name", _BOUNDED_METHODS)
def test_bounded_session_method_times_out(method_name: str) -> None:
    """Each bounded session method times out on its own."""
    import asyncio
    import time

    from sqlalchemy.ext.asyncio import AsyncSession

    from src.db import BoundedSession, db_call_timeout

    async def stall(self: object, *_args: object, **_kwargs: object) -> None:
        del self
        await asyncio.sleep(30)

    async def _check() -> None:
        session = BoundedSession()
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            if method_name == "rollback":
                await session.rollback()
            elif method_name == "close":
                await session.close()
            elif method_name == "delete":
                await session.delete(object())
            elif method_name == "refresh":
                await session.refresh(object())
            elif method_name == "get":
                await session.get(object, 1)
            elif method_name == "merge":
                await session.merge(object())
            elif method_name == "connection":
                await session.connection()
            else:

                def _sync_fn(_sync: object) -> None:
                    return None

                await session.run_sync(_sync_fn)
        assert time.monotonic() - started < 2

    async def _run() -> None:
        token = db_call_timeout.set(0.25)
        original = getattr(AsyncSession, method_name)
        setattr(AsyncSession, method_name, stall)
        try:
            await _check()
        finally:
            setattr(AsyncSession, method_name, original)
            db_call_timeout.reset(token)

    asyncio.run(_run())


def test_cancelled_caller_stops_the_session_connection() -> None:
    import asyncio
    import time
    from typing import Any

    from src import db as db_module

    stopped: list[object] = []
    original_stop = db_module._stop_session_connection  # pyright: ignore[reportPrivateUsage]

    def _spy(session: Any) -> None:
        stopped.append(session)
        original_stop(session)

    async def _never() -> None:
        gate = asyncio.Event()
        await gate.wait()

    async def _outer(session: Any) -> None:
        await session._bounded(_never())

    async def _run() -> None:
        created: list[asyncio.Task[Any]] = []
        original_create = asyncio.create_task

        def _wrap(coro: Any, *args: Any, **kwargs: Any) -> asyncio.Task[Any]:
            task: asyncio.Task[Any] = original_create(coro, *args, **kwargs)
            created.append(task)
            return task

        db_module._stop_session_connection = _spy  # pyright: ignore[reportPrivateUsage]
        asyncio.create_task = _wrap  # type: ignore[method-assign, assignment]
        try:
            session = db_module.BoundedSession()
            outer = asyncio.create_task(_outer(session))
            await asyncio.sleep(0.05)
            inner = next(task for task in created if task is not outer)
            outer.cancel()
            started = time.monotonic()
            with pytest.raises(asyncio.CancelledError):
                await outer
            assert time.monotonic() - started < 0.5
            assert inner.done()
            assert stopped == [session]
        finally:
            asyncio.create_task = original_create  # type: ignore[method-assign]
            db_module._stop_session_connection = (  # pyright: ignore[reportPrivateUsage]
                original_stop
            )

    asyncio.run(_run())


def test_database_receives_scram_verifiers_and_tmpfs() -> None:
    import yaml

    from tests.sentica.scram import scram_verifier

    password = "tenant-secret-value"
    verifier = scram_verifier(password)
    assert verifier.startswith("SCRAM-SHA-256$")
    assert password not in verifier
    document = yaml.safe_load(Path("docker-compose.sentica.yml").read_text())
    services = document["services"]
    database_tmpfs = services["database"]["tmpfs"]
    assert "/var/lib/postgresql/secrets" in database_tmpfs
    assert "/var/lib/postgresql/certs" in database_tmpfs
    assert services["pgbouncer-a"]["tmpfs"] == ["/tmp"]
    assert services["pgbouncer-b"]["tmpfs"] == ["/tmp"]
    fixture = Path("tests/sentica/test_m1_stack.py").read_text(encoding="utf-8")
    assert "scram_verifier(password_a)" in fixture
    assert "scram_verifier(password_b)" in fixture


def test_bounded_wait_matches_psycopg() -> None:
    import inspect

    import psycopg.waiting as waiting
    from psycopg._enums import Ready

    from src.db import _DRIVER_READY_RW  # pyright: ignore[reportPrivateUsage]

    expected = {
        "wait_async": (
            ("gen", "fileno", "interval"),
            ("wakeup", "fileno", "ev", "ready"),
        ),
        "wait_conn_async": (("gen", "interval"), ("wakeup", "fileno", "ev", "ready")),
    }
    for name, (parameters, locals_) in expected.items():
        function = getattr(waiting, name)
        assert inspect.iscoroutinefunction(function)
        assert tuple(inspect.signature(function).parameters) == parameters
        names = function.__code__.co_varnames + function.__code__.co_cellvars
        for local in locals_:
            assert local in names
    assert int(Ready.RW) == _DRIVER_READY_RW
    source = Path("src/db.py").read_text(encoding="utf-8")
    assert 'in {"wait_async", "wait_conn_async"}' in source


def test_production_log_strips_messages_except_the_allow_list() -> None:
    from src.sentica.log_policy import ALLOW_LIST, ProductionLogFilter

    redactor = ProductionLogFilter()
    workspace, observer, observed = "ws-canary", "observer-canary", "observed-canary"
    interpolated = logging.LogRecord(
        "src.deriver.deriver",
        logging.INFO,
        __file__,
        12,
        f"for {workspace}/{observer}/{observed}",
        (),
        None,
        func="process_batch",
    )
    assert redactor.filter(interpolated)
    rendered = interpolated.getMessage()
    assert workspace not in rendered
    assert observer not in rendered
    assert rendered == "src.deriver.deriver process_batch 12 INFO"

    formatted = logging.LogRecord(
        "src.dreamer.orchestrator",
        logging.INFO,
        __file__,
        40,
        "observed=%s",
        (observed,),
        None,
        func="run_dream",
    )
    assert redactor.filter(formatted)
    assert observed not in formatted.getMessage()
    assert "observed=" not in formatted.getMessage()

    logger_name = "src.sentica.db_connect"
    template = "proxy tenant=%s connections=%s"
    assert (logger_name, template) in ALLOW_LIST
    allowed = logging.LogRecord(
        logger_name,
        logging.INFO,
        __file__,
        8,
        template,
        ("tenant-a", 2),
        None,
        func="apply_production_engine_args",
    )
    assert redactor.filter(allowed)
    assert allowed.getMessage() == "proxy tenant=tenant-a connections=2"

    same_text = logging.LogRecord(
        "src.other",
        logging.INFO,
        __file__,
        8,
        template,
        ("tenant-a", 2),
        None,
        func="apply_production_engine_args",
    )
    assert redactor.filter(same_text)
    assert "tenant-a" not in same_text.getMessage()
    assert "proxy tenant" not in same_text.getMessage()
