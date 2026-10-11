"""Tenant stamps on deriver queue rows. The stamp is an id, not content."""

from __future__ import annotations

from typing import Any

from .refusals import record_refusal
from .runtime import production_enabled, require_instance


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
