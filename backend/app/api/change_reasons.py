from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.db_errors import translate_db_errors
from app.api.deps import DbSession, get_or_404
from app.models import ChangeReason
from app.schemas.catalog import ChangeReasonIn, ChangeReasonOut

router = APIRouter(prefix="/change-reasons", tags=["change-reasons"])


@router.get("", response_model=list[ChangeReasonOut])
def list_change_reasons(db: DbSession) -> list[ChangeReason]:
    return list(db.scalars(select(ChangeReason).order_by(ChangeReason.label)))


@router.get("/{reason_id}", response_model=ChangeReasonOut)
def get_change_reason(reason_id: int, db: DbSession) -> ChangeReason:
    return get_or_404(db, ChangeReason, reason_id, "change reason")


@router.post("", response_model=ChangeReasonOut, status_code=status.HTTP_201_CREATED)
def create_change_reason(body: ChangeReasonIn, db: DbSession) -> ChangeReason:
    """Add a reason to the shared list; usable with every compensation type at once."""
    reason = ChangeReason(**body.model_dump())
    with translate_db_errors(db):
        db.add(reason)
        db.commit()
    return reason
