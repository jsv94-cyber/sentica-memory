"""Tenant stamps on deriver queue rows. The stamp is an id, not content."""

from __future__ import annotations

import os
from typing import Any, cast

from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Insert

from .refusals import record_refusal
from .runtime import production_enabled, require_instance

_stamp_installed = False


class SenticaTenantRefused(RuntimeError):
    """A queue task is not for this instance. The message has no content."""

    def __init__(self, reason: str) -> None:
        record_refusal(reason)
        super().__init__("deriver task refused")


def stamp_queue_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add this instance's tenant id to each queue payload in production.

    Args:
        records: Core insert rows. Mutated in place.

    Returns:
        The same list.
    """
    if not production_enabled():
        return records
    tenant_id = require_instance().tenant_id
    for record in records:
        payload = dict(record.get("payload") or {})
        payload["tenant_id"] = tenant_id
        record["payload"] = payload
    return records


def install_queue_stamp(queue_item_cls: type[Any]) -> None:
    """Stamp every QueueItem insert in production, including ORM inserts."""
    global _stamp_installed
    if _stamp_installed:
        return
    event.listen(queue_item_cls, "before_insert", _stamp_orm_item)
    event.listen(Session, "do_orm_execute", _stamp_core_insert)
    _stamp_installed = True


def _stamp_orm_item(_mapper: Any, _connection: Any, target: Any) -> None:
    if os.environ.get("SENTICA_MODE", "").strip() != "production":
        return
    payload = dict(target.payload or {})
    payload["tenant_id"] = require_instance().tenant_id
    target.payload = payload


def _stamp_core_insert(orm_execute_state: Any) -> None:
    if os.environ.get("SENTICA_MODE", "").strip() != "production":
        return
    statement = orm_execute_state.statement
    if not isinstance(statement, Insert):
        return
    table = getattr(statement, "table", None)
    if table is None or table.name != "queue":
        return
    tenant_id = require_instance().tenant_id
    params: object = orm_execute_state.parameters
    rows: list[dict[str, Any]] = []
    if isinstance(params, dict):
        rows = [cast(dict[str, Any], params)]
    elif isinstance(params, list | tuple):
        for item in cast(tuple[object, ...], params):
            if isinstance(item, dict):
                rows.append(cast(dict[str, Any], item))
    else:
        return
    for row in rows:
        if "payload" not in row:
            continue
        raw_payload: object = row.get("payload")
        payload: dict[str, Any] = (
            cast(dict[str, Any], raw_payload) if isinstance(raw_payload, dict) else {}
        )
        payload["tenant_id"] = tenant_id
        row["payload"] = payload


def pop_and_check_tenant(payload: dict[str, Any]) -> None:
    """Remove ``tenant_id`` from a payload and refuse a mismatch.

    The key is removed so upstream payload models, which forbid extras,
    still validate. In upstream mode the key is ignored when absent.

    Args:
        payload: Queue payload dict, mutated in place.

    Raises:
        SenticaTenantRefused: Production mode and the tenant is missing
            or not this instance.
    """
    tenant = payload.pop("tenant_id", None)
    if not production_enabled():
        return
    expected = require_instance().tenant_id
    if not isinstance(tenant, str) or not tenant.strip():
        raise SenticaTenantRefused("missing_tenant")
    if tenant != expected:
        raise SenticaTenantRefused("wrong_tenant")
