"""Subject freshness and hard deletion."""

import datetime

from fastapi import APIRouter, Body, Depends, Path
from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src import models
from src.crud.peer import reject_scope_peers
from src.dependencies import db
from src.exceptions import ValidationException
from src.security import require_auth
from src.sentica.freshness import forget, mark_subject_changed

router = APIRouter(
    prefix="/workspaces/{workspace_id}",
    tags=["subjects"],
    dependencies=[Depends(require_auth(workspace_name="workspace_id"))],
)


class SubjectChanged(BaseModel):
    subject_ref: str
    stale_count: int


class DeletionCreate(BaseModel):
    peer_id: str | None = None
    subject_ref: str | None = None

    @model_validator(mode="after")
    def one_target(self) -> "DeletionCreate":
        if (self.peer_id is None) == (self.subject_ref is None):
            raise ValueError("set peer_id or subject_ref, not both")
        return self


class DigestRead(BaseModel):
    id: str
    kind: str
    period: str
    content: str
    external: bool
    created_at: datetime.datetime


class DeletionResult(BaseModel):
    id: str
    peer_name: str | None = None
    subject_ref: str | None = None
    document_count: int
    embedding_count: int
    summary_count: int


@router.post(
    "/subjects/{subject_ref}/changed",
    response_model=SubjectChanged,
)
async def subject_changed(
    workspace_id: str = Path(...),
    subject_ref: str = Path(...),
    db: AsyncSession = db,
) -> SubjectChanged:
    """Mark every live conclusion for this subject stale."""
    if subject_ref.strip() == "":
        raise ValidationException("subject_ref is empty")
    count = await mark_subject_changed(db, workspace_id, subject_ref)
    return SubjectChanged(subject_ref=subject_ref, stale_count=count)


@router.post("/deletions", response_model=DeletionResult, status_code=201)
async def create_deletion(
    workspace_id: str = Path(...),
    body: DeletionCreate = Body(...),
    db: AsyncSession = db,
) -> DeletionResult:
    """Hard-delete a peer or a subject. The record has counts and no text."""
    if body.peer_id is not None:
        await reject_scope_peers(
            db,
            workspace_id,
            [body.peer_id],
            action="Delete a peer by its own name, not a scope.",
        )
    try:
        record = await forget(
            db,
            workspace_id,
            peer_name=body.peer_id,
            subject_ref=body.subject_ref,
        )
    except ValueError as exc:
        raise ValidationException(str(exc)) from exc
    return DeletionResult(
        id=record.id,
        peer_name=record.peer_name,
        subject_ref=record.subject_ref,
        document_count=record.document_count,
        embedding_count=record.embedding_count,
        summary_count=record.summary_count,
    )


@router.get("/digests", response_model=list[DigestRead])
async def list_digests(
    workspace_id: str = Path(...),
    db: AsyncSession = db,
) -> list[DigestRead]:
    """Read this workspace's digests. Another workspace's rows are not visible."""
    rows = (
        await db.scalars(
            select(models.DreamDigest)
            .where(models.DreamDigest.workspace_name == workspace_id)
            .order_by(models.DreamDigest.created_at.asc(), models.DreamDigest.id.asc())
        )
    ).all()
    return [
        DigestRead(
            id=row.id,
            kind=row.kind,
            period=row.period,
            content=row.content,
            external=row.external,
            created_at=row.created_at,
        )
        for row in rows
    ]
