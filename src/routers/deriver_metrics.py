"""Deriver work metrics as JSON, with the age of the measurement alongside them."""

from logging import getLogger

from fastapi import APIRouter, Depends, HTTPException

from src.backlog import DeriverMetricsPoller
from src.security import JWTParams, require_auth
from src.sentica.workspace_scope import token_allowed

logger = getLogger(__name__)

router = APIRouter(prefix="/deriver", tags=["deriver"])

_poller: DeriverMetricsPoller | None = None


def set_deriver_metrics_poller(poller: DeriverMetricsPoller | None) -> None:
    global _poller
    _poller = poller


# Deriver queue internals: an operational signal for scaling, not a public
# resource.
@router.get("/metrics", include_in_schema=False)
async def get_deriver_metrics_response(
    jwt_params: JWTParams = Depends(require_auth()),
) -> dict[str, float | int]:
    """Seconds of outstanding deriver work, plus the raw counts behind it."""
    snapshot = _poller.snapshot if _poller is not None else None
    if snapshot is None or snapshot.measured_at is None:
        raise HTTPException(
            status_code=503, detail="No deriver measurement available yet"
        )
    allowed = token_allowed(jwt_params)
    if allowed is not None:
        from src.backlog import outstanding_work_seconds
        from src.crud import metrics_for_workspaces
        from src.dependencies import tracked_db
        from src.dreamer.dream_due import list_due_dreams
        from src.sentica.workspace_scope import reset_workspace, set_workspace

        scoped = await metrics_for_workspaces(allowed)
        dreams = 0
        for name in allowed:
            token = set_workspace(name)
            try:
                async with tracked_db("deriver_metrics.dreams", read_only=True) as db:
                    dreams += len(await list_due_dreams(db))
            finally:
                reset_workspace(token)
        return {
            "outstanding_work_seconds": outstanding_work_seconds(
                scoped, dreams_due=dreams
            ),
            "eligible_work_units": scoped.eligible_work_units,
            "claimed_work_units": scoped.claimed_work_units,
            "pending_items": scoped.pending_items,
            "oldest_pending_age_seconds": scoped.oldest_pending_age_seconds,
            "embeddings_pending": scoped.embeddings_pending,
            "embeddings_pending_due": scoped.embeddings_pending_due,
            "dreams_due": dreams,
            "measured_at": snapshot.measured_at,
            "measurement_age_seconds": snapshot.age_seconds or 0.0,
        }

    return {
        "outstanding_work_seconds": snapshot.signal_seconds,
        "eligible_work_units": snapshot.stats.eligible_work_units,
        "claimed_work_units": snapshot.stats.claimed_work_units,
        "pending_items": snapshot.stats.pending_items,
        "oldest_pending_age_seconds": snapshot.stats.oldest_pending_age_seconds,
        "embeddings_pending": snapshot.stats.embeddings_pending,
        "embeddings_pending_due": snapshot.stats.embeddings_pending_due,
        "dreams_due": snapshot.dreams_due,
        "measured_at": snapshot.measured_at,
        "measurement_age_seconds": snapshot.age_seconds or 0.0,
    }
